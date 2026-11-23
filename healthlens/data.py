"""Data preparation: turn a patient workbook into clean, chronologically sorted frames."""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Metrics the engine analyses. key -> (label, sheet, column or lab test name, unit, kind)
# kind: 'daily' (one reading per day), 'lab' (a few draws per year), 'cycle' (menstrual cycles)
METRICS: dict[str, tuple[str, str, str, str, str]] = {
    # Labs
    'glucose': ('Fasting glucose', 'Lab Results', 'Fasting glucose', 'mg/dL', 'lab'),
    'hba1c': ('HbA1c', 'Lab Results', 'HbA1c', '%', 'lab'),
    'ldl': ('LDL cholesterol', 'Lab Results', 'LDL cholesterol', 'mg/dL', 'lab'),
    'hdl': ('HDL cholesterol', 'Lab Results', 'HDL cholesterol', 'mg/dL', 'lab'),
    'triglycerides': ('Triglycerides', 'Lab Results', 'Triglycerides', 'mg/dL', 'lab'),
    'total_chol': ('Total cholesterol', 'Lab Results', 'Total cholesterol', 'mg/dL', 'lab'),
    'creatinine': ('Creatinine', 'Lab Results', 'Creatinine', 'mg/dL', 'lab'),
    'egfr': ('eGFR', 'Lab Results', 'eGFR', 'mL/min/1.73m²', 'lab'),
    'alt': ('ALT', 'Lab Results', 'ALT', 'U/L', 'lab'),
    'tsh': ('TSH', 'Lab Results', 'TSH', 'mIU/L', 'lab'),
    'vitamin_d': ('Vitamin D', 'Lab Results', 'Vitamin D', 'ng/mL', 'lab'),
    'ferritin': ('Ferritin', 'Lab Results', 'Ferritin', 'ng/mL', 'lab'),
    'hemoglobin': ('Hemoglobin', 'Lab Results', 'Hemoglobin', 'g/dL', 'lab'),
    # Vitals (daily)
    'sbp': ('Systolic BP', 'Vitals Daily', 'Systolic BP', 'mmHg', 'daily'),
    'dbp': ('Diastolic BP', 'Vitals Daily', 'Diastolic BP', 'mmHg', 'daily'),
    'rhr': ('Resting heart rate', 'Vitals Daily', 'Resting HR', 'bpm', 'daily'),
    'weight': ('Weight', 'Vitals Daily', 'Weight kg', 'kg', 'daily'),
    'bmi': ('BMI', 'Vitals Daily', 'BMI', 'kg/m²', 'daily'),
    'spo2': ('SpO2', 'Vitals Daily', 'SpO2 %', '%', 'daily'),
    # Wearables (daily)
    'steps': ('Steps', 'Wearable Daily', 'Steps', 'steps/day', 'daily'),
    'sleep': ('Sleep', 'Wearable Daily', 'Sleep Hours', 'hours', 'daily'),
    'active': ('Active minutes', 'Wearable Daily', 'Active Minutes', 'min/day', 'daily'),
    'hrv': ('Heart-rate variability', 'Wearable Daily', 'HRV ms', 'ms', 'daily'),
    'sedentary': ('Sedentary time', 'Wearable Daily', 'Sedentary Hours', 'hours/day', 'daily'),
    'stress': ('Stress score', 'Wearable Daily', 'Stress Score 1-10', '/10', 'daily'),
    # Women's health
    'cycle_length': ('Menstrual cycle length', 'Women Health', 'Cycle Length', 'days', 'cycle'),
}

DATE_COLUMNS = ('Date', 'Start Date', 'End Date', 'Week Start', 'Month')


def to_datetime(col: pd.Series) -> pd.Series:
    """Excel serial numbers and ordinary date strings -> Timestamps."""
    if pd.api.types.is_datetime64_any_dtype(col):
        return pd.to_datetime(col, errors='coerce')
    numeric = pd.to_numeric(col, errors='coerce')
    excel = pd.to_datetime(numeric, unit='D', origin='1899-12-30', errors='coerce')
    ordinary = pd.to_datetime(col.where(numeric.isna()), errors='coerce')
    return excel.where(numeric.notna(), ordinary)


def clean_sheet(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy().dropna(how='all')
    for name in DATE_COLUMNS:
        if name in frame.columns:
            frame[name] = to_datetime(frame[name])
    key = next((c for c in ('Date', 'Start Date', 'Week Start', 'Month') if c in frame.columns), None)
    if key:
        frame = frame.sort_values(key, na_position='last')
    return frame.reset_index(drop=True)


def parse_range(text) -> tuple[float | None, float | None]:
    """'70–99' -> (70, 99); '>40' -> (40, None); '<100' -> (None, 100)."""
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return None, None
    s = str(text).replace('–', '-').replace('—', '-').replace(' ', '')
    m = re.fullmatch(r'(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)', s)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.fullmatch(r'[>≥](=?)(\d+(?:\.\d+)?)', s)
    if m:
        return float(m.group(2)), None
    m = re.fullmatch(r'[<≤](=?)(\d+(?:\.\d+)?)', s)
    if m:
        return None, float(m.group(2))
    return None, None


@dataclass
class Series:
    key: str
    label: str
    unit: str
    kind: str
    sheet: str
    dates: pd.DatetimeIndex
    values: np.ndarray
    ref_low: float | None = None
    ref_high: float | None = None
    ref_text: str = ''

    def __len__(self):
        return len(self.values)

    def points(self):
        return [{'date': d.strftime('%Y-%m-%d'), 'value': round(float(v), 3)}
                for d, v in zip(self.dates, self.values)]


class PatientRecord:
    """A cleaned patient workbook with convenient accessors."""

    def __init__(self, book: dict[str, pd.DataFrame]):
        self.raw = book
        self.sheets = {name: clean_sheet(frame) for name, frame in book.items()}
        prof = self.sheets.get('Patient Profile', pd.DataFrame())
        self.profile = {str(k): v for k, v in zip(prof.get('Field', []), prof.get('Value', []))}
        self.series: dict[str, Series] = {}
        for key in METRICS:
            s = self._build_series(key)
            if s is not None and len(s):
                self.series[key] = s

    def sheet(self, name) -> pd.DataFrame:
        return self.sheets.get(name, pd.DataFrame())

    @property
    def sex(self) -> str:
        """Sex from the profile. When no uploaded document states it (e.g. only a period-tracker export was
        uploaded), recorded menstrual cycles mean the record is female, so Women's health can still be assessed."""
        stated = self.profile.get('Sex', '')
        stated = '' if stated is None or (isinstance(stated, float) and stated != stated) else str(stated).strip()
        if stated:
            return stated
        cycles = self.series.get('cycle_length')
        return 'Female' if cycles is not None and len(cycles) else ''

    @property
    def sex_inferred(self) -> bool:
        """True when sex was not stated in any document and was inferred from cycle data."""
        stated = self.profile.get('Sex', '')
        return bool(self.sex) and not (isinstance(stated, str) and stated.strip())

    @property
    def window(self) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        dates = [s.dates for s in self.series.values() if len(s)]
        if not dates:
            return None, None
        alld = dates[0].append(dates[1:]) if len(dates) > 1 else dates[0]
        return alld.min(), alld.max()

    def _build_series(self, key):
        label, sheet, col, unit, kind = METRICS[key]
        frame = self.sheet(sheet)
        if frame.empty:
            return None
        date_col = 'Start Date' if kind == 'cycle' else 'Date'
        ref_low = ref_high = None
        ref_text = ''
        if kind == 'lab':
            if not {'Test Name', 'Result', 'Date'}.issubset(frame.columns):
                return None
            sub = frame[frame['Test Name'].astype(str).str.strip().str.casefold() == col.casefold()]
            if sub.empty:
                return None
            if 'Unit' in sub.columns and sub['Unit'].notna().any():
                unit = str(sub['Unit'].dropna().iloc[0])
            if 'Reference Range' in sub.columns and sub['Reference Range'].notna().any():
                ref_text = str(sub['Reference Range'].dropna().iloc[-1])
                ref_low, ref_high = parse_range(ref_text)
            values = pd.to_numeric(sub['Result'], errors='coerce')
            dates = sub['Date']
        else:
            if col not in frame.columns or date_col not in frame.columns:
                return None
            values = pd.to_numeric(frame[col], errors='coerce')
            dates = frame[date_col]
        ok = values.notna() & dates.notna()
        if not ok.any():
            return None
        d = pd.DatetimeIndex(dates[ok])
        v = values[ok].to_numpy(dtype=float)
        order = np.argsort(d.values, kind='stable')
        return Series(key, label, unit, kind, sheet, d[order], v[order], ref_low, ref_high, ref_text)


# ---------------------------------------------------------------- time span (e.g. latest 3 months)
# Measurement sheets are cut to the chosen span, using the date column shown here. Patient Profile,
# Medical History and Medication History are always kept in full: they are context, not measurements.
SPAN_SHEETS = {'Lab Results': 'Date', 'Vitals Daily': 'Date', 'Wearable Daily': 'Date', 'Habits Weekly': 'Week Start',
               'Symptoms Timeline': 'Date', 'Encounters & Notes': 'Date', 'Medication Adherence': 'Month',
               'Women Health': 'Start Date'}


def latest_date(book: dict[str, pd.DataFrame]):
    """Most recent date found in the measurement sheets (the end of the record)."""
    ends = []
    for name, col in SPAN_SHEETS.items():
        frame = book.get(name)
        if frame is not None and col in frame.columns:
            d = to_datetime(frame[col]).dropna()
            if not d.empty:
                ends.append(d.max())
    return max(ends) if ends else None


def trim_book(book: dict[str, pd.DataFrame], months: int):
    """Keep only the latest `months` of measurements. Returns (trimmed book, (start, end)) or (book, None)."""
    end = latest_date(book)
    if end is None:
        return book, None
    start = (end - pd.DateOffset(months=months)).normalize() + pd.Timedelta(days=1)
    out = {}
    for name, frame in book.items():
        col = SPAN_SHEETS.get(name)
        if col and col in frame.columns:
            keep = to_datetime(frame[col]) >= start
            out[name] = frame[keep.fillna(False).to_numpy(dtype=bool)].reset_index(drop=True)
        else:
            out[name] = frame
    return out, (start, end)


# ---------------------------------------------------------------- several uploads of one patient
# A patient may upload documents one at a time (this month's blood work, last month's vitals...).
# All uploads for the same Patient ID are combined into one record before the analysis.
# Rows describing the same reading are matched by these columns: the same date (and the same lab test)
# is one reading, so a newer upload replaces it instead of counting it twice.
MERGE_KEYS = {
    'Lab Results': ('Date', 'Test Name'),
    'Vitals Daily': ('Date',),
    'Wearable Daily': ('Date',),
    'Habits Weekly': ('Week Start',),
    'Women Health': ('Start Date',),
}
# Other sheets (history, medications, adherence, symptoms, notes) can hold several different rows for the same
# date, so only exact repeats are removed there.


def patient_id_of(book: dict[str, pd.DataFrame]) -> str:
    """The Patient ID written in the workbook's Patient Profile sheet ('' if there is none)."""
    prof = book.get('Patient Profile')
    if prof is None or not {'Field', 'Value'}.issubset(prof.columns):
        return ''
    for field, value in zip(prof['Field'], prof['Value']):
        if str(field).strip().casefold() == 'patient id' and not pd.isna(value):
            return str(value).strip()
    return ''


def _merge_profile(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Field/Value pairs from every upload; a newer upload's value replaces an older one."""
    values: dict[str, object] = {}
    for frame in frames:
        if not {'Field', 'Value'}.issubset(frame.columns):
            continue
        for field, value in zip(frame['Field'], frame['Value']):
            if pd.isna(field) or (not isinstance(value, str) and pd.isna(value)):
                continue
            values[str(field).strip()] = value
    return pd.DataFrame({'Field': list(values), 'Value': list(values.values())})


def merge_workbooks(books: list[dict[str, pd.DataFrame]]) -> dict[str, pd.DataFrame]:
    """Combine several uploads of one patient (oldest first) into one workbook.

    One upload is returned unchanged. Otherwise each sheet's rows are added together; a reading that several
    uploads contain (same key, see MERGE_KEYS, or the same row) is kept once, with the newest upload's values."""
    books = [b for b in books if b]
    if len(books) <= 1:
        return books[0] if books else {}
    names = list(dict.fromkeys(name for book in books for name in book))
    merged = {}
    for name in names:
        frames = [book[name].dropna(how='all') for book in books if name in book]
        frames = [f for f in frames if not f.empty] or [books[-1].get(name, pd.DataFrame())]
        if name == 'Patient Profile':
            merged[name] = _merge_profile(frames)
            continue
        keys = MERGE_KEYS.get(name, ())
        prepared = []
        for frame in frames:
            frame = frame.copy()
            for col in DATE_COLUMNS:                 # Excel serial numbers and real dates compare equal
                if col in frame.columns:
                    frame[col] = to_datetime(frame[col])
            cols = list(keys) if keys and set(keys) <= set(frame.columns) else list(frame.columns)
            text = frame[cols].map(lambda v: '' if not isinstance(v, str) and pd.isna(v) else str(v).strip().casefold())
            match = text.agg('|'.join, axis=1) if len(frame) else pd.Series([], dtype=object)
            # Rows repeated inside ONE upload are real repeats and are all kept; only a row that another upload
            # also contains is matched (the n-th copy in one upload pairs with the n-th copy in another).
            frame['_match'] = ('|'.join(cols) + '#') + match + '#' + match.groupby(match).cumcount().astype(str)
            prepared.append(frame)
        frame = pd.concat(prepared, ignore_index=True, sort=False)
        frame = frame.drop_duplicates('_match', keep='last').drop(columns='_match')
        merged[name] = clean_sheet(frame)
    return merged
