"""Rules for building one patient's record document by document (patient accounts).
The record belongs to the account from the first upload. A document is never matched to the record by its Patient ID:
IDs, names, sex and date of birth printed in documents are FACTS about the document, compared with what is known.
Every decision is made here, from the documents themselves; the patient is never asked to vouch for a document.
  * Reject      a document that CONTRADICTS what the record already knows (name, sex, date of birth, age at the
                document date, or period data for a male record), and a document with a Patient ID the record has not
                seen unless the same name or date of birth shows it is the same person (labs issue their own IDs).
  * Accept      everything else: no Patient ID, a known Patient ID, the first document, old reports, partly
                overlapping reports.
  * Leave out   single readings that are impossible (glucose 9000 mg/dL, a reading dated in the future); the rest of
                the document is kept.
Pure functions only (no database, no Flask), so each rule can be tested on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .data import METRICS, PatientRecord, to_datetime

# Physically possible values, in the units HealthLens stores (wide on purpose: only clear errors are left out).
PLAUSIBLE = {
    'glucose': (20, 1000), 'hba1c': (2, 20), 'ldl': (5, 500), 'hdl': (5, 200), 'triglycerides': (10, 5000),
    'total_chol': (30, 1000), 'creatinine': (0.1, 25), 'egfr': (1, 200), 'alt': (1, 5000), 'tsh': (0.001, 200),
    'vitamin_d': (1, 300), 'ferritin': (1, 10000), 'hemoglobin': (2, 25),
    'sbp': (50, 300), 'dbp': (20, 200), 'rhr': (20, 250), 'weight': (2, 400), 'bmi': (8, 100), 'spo2': (50, 100),
    'steps': (0, 100000), 'sleep': (0, 24), 'active': (0, 1440), 'hrv': (1, 300), 'sedentary': (0, 24),
    'stress': (0, 10), 'cycle_length': (10, 120),
}
AGE_TOLERANCE = 1                    # years: a report's printed age may be rounded or taken a few months off


def _today():
    return pd.Timestamp(date.today())


def drop_implausible(sheets: dict[str, pd.DataFrame]) -> int:
    """Remove impossible readings and readings dated in the future, in place. Returns how many were left out."""
    dropped = 0
    future = _today() + pd.Timedelta(days=1)
    for key, (_label, sheet, col, unit, kind) in METRICS.items():
        frame = sheets.get(sheet)
        if frame is None or frame.empty:
            continue
        low, high = PLAUSIBLE.get(key, (-np.inf, np.inf))
        date_col = 'Start Date' if kind == 'cycle' else 'Date'
        if kind == 'lab':
            if not {'Test Name', 'Result'}.issubset(frame.columns):
                continue
            rows = frame['Test Name'].astype(str).str.strip().str.casefold() == col.casefold()
            if 'Unit' in frame.columns:            # only judge values given in the unit the range is written for
                given = frame['Unit'].astype(str).str.strip().str.casefold()
                rows &= frame['Unit'].isna() | (given == unit.casefold())
            values = pd.to_numeric(frame['Result'], errors='coerce')
            bad = rows & values.notna() & ((values < low) | (values > high))
            if date_col in frame.columns:
                bad |= rows & (to_datetime(frame[date_col]) > future)
            if bad.any():
                dropped += int(bad.sum())
                sheets[sheet] = frame = frame[~bad].reset_index(drop=True)
        else:
            if col not in frame.columns:
                continue
            values = pd.to_numeric(frame[col], errors='coerce')
            bad = values.notna() & ((values < low) | (values > high))
            if date_col in frame.columns:
                bad |= values.notna() & (to_datetime(frame[date_col]) > future)
            if bad.any():
                dropped += int(bad.sum())
                frame = frame.copy()
                frame.loc[bad, col] = np.nan
                sheets[sheet] = frame
    return dropped


def observations_of(sheets: dict[str, pd.DataFrame]) -> list[dict]:
    """One row per measurement found in a document: measure, value, unit, date measured, the lab's own range."""
    rec = PatientRecord(sheets)
    out = []
    for key, s in rec.series.items():
        for d, v in zip(s.dates, s.values):
            out.append({'measure': key, 'value': float(v), 'unit': s.unit, 'measured_at': d.strftime('%Y-%m-%d'),
                        'ref_low': s.ref_low, 'ref_high': s.ref_high})
    return out


@dataclass
class DocFacts:
    """What one document says about who it belongs to."""
    patient_id: str = ''
    name: str = ''
    sex: str = ''                    # 'Female' / 'Male' / ''
    birth_date: str = ''             # YYYY-MM-DD or ''
    age: int | None = None
    doc_date: pd.Timestamp | None = None
    has_cycles: bool = False


def _profile(sheets) -> dict[str, object]:
    prof = sheets.get('Patient Profile')
    if prof is None or not {'Field', 'Value'}.issubset(prof.columns):
        return {}
    return {str(f).strip().casefold(): v for f, v in zip(prof['Field'], prof['Value'])
            if not (isinstance(v, float) and v != v) and str(v).strip()}


def normalise_sex(value) -> str:
    v = str(value or '').strip().casefold()
    return 'Female' if v in ('f', 'female', 'woman') else 'Male' if v in ('m', 'male', 'man') else ''


def facts_of(sheets: dict[str, pd.DataFrame], patient_id: str = '') -> DocFacts:
    p = _profile(sheets)
    f = DocFacts(patient_id=patient_id or str(p.get('patient id', '') or '').strip(),
                 name=str(p.get('name', '') or p.get('patient name', '') or '').strip(), sex=normalise_sex(p.get('sex')))
    dob = pd.to_datetime(p.get('date of birth'), errors='coerce') if p.get('date of birth') is not None else pd.NaT
    f.birth_date = dob.strftime('%Y-%m-%d') if pd.notna(dob) else ''
    try:
        f.age = int(float(p['age'])) if 'age' in p else None
    except (TypeError, ValueError):
        f.age = None
    dates = []
    for sheet in ('Lab Results', 'Vitals Daily', 'Wearable Daily', 'Women Health'):
        frame = sheets.get(sheet)
        col = 'Start Date' if sheet == 'Women Health' else 'Date'
        if frame is not None and col in frame.columns:
            dates.append(to_datetime(frame[col]).dropna())
    alld = pd.concat(dates) if dates else pd.Series(dtype='datetime64[ns]')
    f.doc_date = alld.max() if len(alld) else None
    cyc = sheets.get('Women Health')
    f.has_cycles = bool(cyc is not None and 'Cycle Length' in cyc.columns
                        and pd.to_numeric(cyc['Cycle Length'], errors='coerce').gt(0).any())
    return f


def name_tokens(name: str) -> set[str]:
    """Words of a name, ignoring capitals, punctuation, order and initials: 'Doe, Jane A.' -> {'doe', 'jane'}."""
    return {w for w in re.findall(r"[a-z]+(?:'[a-z]+)?", str(name or '').casefold()) if len(w) > 1}


def same_name(a: str, b: str) -> bool:
    """True when two printed names are the same person's: equal words, or one adds a middle name to the other."""
    x, y = name_tokens(a), name_tokens(b)
    if not x or not y:
        return False
    small, big = (x, y) if len(x) <= len(y) else (y, x)
    return x == y or (len(small) >= 2 and small <= big)


def age_on(birth_date: str, when: pd.Timestamp | None) -> int | None:
    if not birth_date:
        return None
    b = pd.Timestamp(birth_date)
    w = when if when is not None else _today()
    return int(w.year - b.year - ((w.month, w.day) < (b.month, b.day)))


@dataclass
class Known:
    """A fact already on the record: its value and whether the patient confirmed it ('user') or a document said it."""
    value: str
    confirmed: bool
    source: str = ''                 # file name of the document it came from, for messages


@dataclass
class Verdict:
    action: str                      # 'accept' | 'reject'
    reason: str = ''


def judge(doc: DocFacts, known: dict[str, Known], identifiers: set[str], name: str) -> Verdict:
    """Decide what happens to one document, from what it says and what the record already knows.
    known: 'sex' and 'birth_date' -> Known. identifiers: Patient IDs already on the record."""
    def where(k: Known):
        return 'your record says' if k.confirmed else f'{k.source or "an earlier document"} says'
    def reject(text):
        return Verdict('reject', text + ' Not added.')
    person = known.get('name')
    if doc.name and person and not same_name(doc.name, person.value):
        return reject(f'{name} lists name {doc.name}, but {where(person)} {person.value}.')
    sex = known.get('sex')
    if doc.sex and sex and doc.sex != sex.value:
        return reject(f'{name} lists sex as {doc.sex}, but {where(sex)} {sex.value}.')
    if doc.has_cycles and sex and sex.value == 'Male' and not doc.sex:
        return reject(f'{name} contains period data, but {where(sex)} Male.')
    dob = known.get('birth_date')
    if doc.birth_date and dob and doc.birth_date != dob.value:
        return reject(f'{name} lists date of birth {doc.birth_date}, but {where(dob)} {dob.value}.')
    if doc.age is not None and not doc.birth_date and dob:
        expected = age_on(dob.value, doc.doc_date)
        if expected is not None and abs(doc.age - expected) > AGE_TOLERANCE:
            return reject(f'{name} lists age {doc.age}, but the date of birth {where(dob)} ({dob.value}) gives {expected}.')
    if doc.patient_id and identifiers and doc.patient_id not in identifiers:
        same_person = bool((doc.birth_date and dob and doc.birth_date == dob.value)
                           or (doc.name and person and same_name(doc.name, person.value)))
        if not same_person:              # a new ID is accepted only when the name or date of birth shows the same person
            return reject(f'{name} shows Patient ID {doc.patient_id}, but your record has {", ".join(sorted(identifiers))} '
                          'and the document does not show the same name or date of birth.')
    return Verdict('accept')
