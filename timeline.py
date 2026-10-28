"""One longitudinal timeline built from every sheet in the workbook."""
from __future__ import annotations

import pandas as pd

from .data import PatientRecord


def _d(value):
    return pd.Timestamp(value).strftime('%Y-%m-%d') if pd.notna(value) else None


def build_timeline(rec: PatientRecord, anomalies: list[dict], lab_flags: list[dict]) -> list[dict]:
    ev: list[dict] = []

    def add(date, category, title, detail='', evidence=None):
        d = _d(date)
        if d:
            ev.append({'date': d, 'category': category, 'title': title, 'detail': detail, 'evidence': evidence})

    for _, r in rec.sheet('Medical History').iterrows():
        add(r.get('Date'), 'history', str(r.get('Finding / Event', 'Medical history entry')),
            ' · '.join(str(r.get(c)) for c in ('Encounter / Type', 'Status', 'Notes') if pd.notna(r.get(c))))
    for _, r in rec.sheet('Medication History').iterrows():
        name = str(r.get('Medication', 'Medication'))
        if name.lower().startswith('no active'):
            continue
        status = str(r.get('Status', ''))
        detail = ' · '.join(str(r.get(c)) for c in ('Dose', 'Frequency', 'Indication', 'Change Rationale') if pd.notna(r.get(c)))
        add(r.get('Start Date'), 'medication', f'{name} started' if status.lower() != 'changed' else f'{name} started (later changed)', detail)
        if pd.notna(r.get('End Date')):
            add(r.get('End Date'), 'medication', f'{name} ended / changed', str(r.get('Change Rationale', '')))
    for _, r in rec.sheet('Symptoms Timeline').iterrows():
        add(r.get('Date'), 'symptom', str(r.get('Symptom', 'Symptom')),
            ' · '.join(str(r.get(c)) for c in ('Severity', 'Duration', 'Source') if pd.notna(r.get(c))))
    for _, r in rec.sheet('Encounters & Notes').iterrows():
        add(r.get('Date'), 'encounter', str(r.get('Encounter', 'Encounter')), str(r.get('Clinical Note', '')))
    labs = rec.sheet('Lab Results')
    if not labs.empty and 'Date' in labs.columns:
        flagged = {}
        for f in lab_flags:
            if f and f.get('evidence'):
                flagged.setdefault(f['date'], []).append(f['label'])
        for date, grp in labs.dropna(subset=['Date']).groupby('Date'):
            tests = ', '.join(grp['Test Name'].astype(str).head(8))
            more = f' (+{len(grp) - 8} more)' if len(grp) > 8 else ''
            note = f' · latest-value flags: {", ".join(flagged[_d(date)])}' if _d(date) in flagged else ''
            add(date, 'lab', f'Lab panel · {len(grp)} tests', tests + more + note)
    wh = rec.sheet('Women Health')
    if 'Start Date' in wh.columns:
        for _, r in wh.dropna(subset=['Start Date']).iterrows():
            add(r.get('Start Date'), 'cycle', f'Cycle {r.get("Cycle")} started',
                ' · '.join(f'{c}: {r.get(c)}' for c in ('Cycle Length', 'Bleeding Days', 'Symptoms', 'Severity') if pd.notna(r.get(c))))
    for a in anomalies:
        span = '' if a['days'] == 1 else f' ({a["days"]} days)'
        add(a['start'], 'anomaly', f'Unusual {a["label"].lower()}{span}',
            f'{a["value"]} {a["unit"]} vs typical {a["typical"]} {a["unit"]}', a['evidence'])
    ev.sort(key=lambda e: e['date'], reverse=True)
    return ev
