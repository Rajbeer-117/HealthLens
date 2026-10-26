"""Runs the whole analysis for one patient and returns a JSON-ready dict."""
from __future__ import annotations

import pandas as pd

from . import analysis as A
from .data import PatientRecord
from .evidence import EvidenceRegistry
from .scenarios import Ctx, run_scenarios
from .timeline import build_timeline

CHART_ORDER = ['glucose', 'hba1c', 'ldl', 'hdl', 'triglycerides', 'sbp', 'dbp', 'rhr', 'weight', 'bmi',
               'steps', 'sleep', 'active', 'hrv', 'stress', 'sedentary', 'spo2', 'cycle_length',
               'total_chol', 'tsh', 'vitamin_d', 'ferritin', 'hemoglobin', 'creatinine', 'egfr', 'alt']


def age_band(age) -> str:
    try:
        a = int(float(age))
    except (TypeError, ValueError):
        return 'not recorded'
    lo = a // 10 * 10
    return f'{lo}–{lo + 9}'


def analyze_book(book: dict[str, pd.DataFrame]) -> dict:
    rec = PatientRecord(book)
    start, end = rec.window
    ev = EvidenceRegistry(start)

    trends = [t for t in (A.trend(s, ev) for s in rec.series.values()) if t]
    lab_flags = [f for f in (A.lab_reference(s, ev) for s in rec.series.values()) if f]
    anomalies = []
    for s in rec.series.values():
        anomalies.extend(A.anomalies(s, ev))
    anomalies.sort(key=lambda a: abs(a['z']), reverse=True)
    co = A.co_occurring(anomalies, rec, ev)
    relationships = A.relationships(rec, ev)
    meds = A.medication_context(rec, ev)
    symptoms = A.recurring_symptoms(rec, ev)
    history = A.history_context(rec, ev)
    quality = A.data_quality(rec, ev)
    scenarios = run_scenarios(Ctx(rec, ev, trends, lab_flags, symptoms, history, meds))
    timeline = build_timeline(rec, anomalies, lab_flags)

    trend_by = {t['metric']: t for t in trends}
    charts = {}
    for key in CHART_ORDER:
        s = rec.series.get(key)
        if s is None:
            continue
        t = trend_by.get(key)
        charts[key] = {
            'label': s.label, 'unit': s.unit, 'kind': s.kind, 'group': s.sheet, 'points': s.points(),
            'trendline': t['trendline'] if t and t['sustained'] else [],
            'direction': t['direction'] if t else 'n/a',
            'anomalies': [p for a in anomalies if a['metric'] == key for p in a['points']],
            'ref': {'low': s.ref_low, 'high': s.ref_high, 'text': s.ref_text},
        }

    evidence = ev.export()
    patient = {
        'age': rec.profile.get('Age'), 'age_band': age_band(rec.profile.get('Age')), 'sex': rec.sex or 'not recorded',
        'family_history': str(rec.profile.get('Family History', '') or ''),
        'window': quality['window'],
    }
    return {
        'patient': patient,
        'evidence': evidence,
        'trends': sorted(trends, key=lambda t: (not t['sustained'], -abs(t['change_pct']))),
        'lab_flags': [f for f in lab_flags if f.get('evidence')],
        'anomalies': anomalies,
        'relationships': relationships,
        'co_occurrences': co,
        'medication_context': meds,
        'symptoms': list(symptoms.values()),
        'history': history,
        'scenarios': scenarios,
        'data_quality': quality,
        'timeline': timeline,
        'charts': charts,
        'counts': {
            'evidence': len(evidence),
            'sustained_trends': sum(t['sustained'] for t in trends),
            'anomaly_episodes': len(anomalies),
            'lab_flags': sum(1 for f in lab_flags if f.get('evidence')),
            'relationships': len(relationships),
            'co_occurrences': len(co),
            'timeline_events': len(timeline),
        },
    }
