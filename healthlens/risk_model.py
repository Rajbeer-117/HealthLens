"""Trained risk model: one small logistic-regression model per health category.

TRAINING (train_risk_model.py) uses the 900 patient workbooks from the business partner (a mix of synthetic and real records):
  * inputs  = measurements only (labs, vitals, wearable data, menstrual cycles) plus age and sex.
              Medical history and medications are never used as inputs.
  * answer  = whether the patient's recorded medical history contains a related condition
              (metabolic: prediabetes / type 2 diabetes / borderline glycemic markers;
               cardiovascular: hypertension / hyperlipidemia; women's health: PCOS).
  * 80% of patients train the model, the other 20% (never seen in training) test it.
  * During training, whole data sources (all labs, all vitals, all wearable data...) are hidden at random,
    so the model also works when a patient has uploaded only one document.
The learned weights are saved in healthlens/risk_model.json. HealthLens only uses them, it does not retrain.

USE: score(book) -> for each category a risk score from 0 to 100, a level (Low / Moderate / High) and the
measures that raised or lowered the score most. It is an educational, data-driven score from the provided training
data, not a clinical risk calculator and not a diagnosis.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .data import PatientRecord

MODEL_FILE = Path(__file__).with_name('risk_model.json')

# Measurement keys (healthlens.data.METRICS) grouped by the kind of document they usually come from.
SOURCES = {
    'labs': ['glucose', 'hba1c', 'ldl', 'hdl', 'triglycerides', 'total_chol'],
    'vitals': ['sbp', 'dbp', 'rhr', 'weight', 'bmi'],
    'wearable': ['steps', 'active', 'sleep', 'hrv', 'stress'],
    'cycles': ['cycle_length'],
}
SOURCE_OF = {k: src for src, keys in SOURCES.items() for k in keys}
CATEGORY_INPUTS = {
    'metabolic': ['glucose', 'hba1c', 'triglycerides', 'hdl', 'weight', 'bmi', 'steps', 'active'],
    'cardiovascular': ['sbp', 'dbp', 'rhr', 'ldl', 'total_chol', 'hdl', 'hrv', 'stress', 'sleep', 'steps'],
    'pcos': ['cycle_length', 'bmi', 'weight', 'glucose', 'hba1c', 'triglycerides', 'hdl'],
}
LABELS = {
    'metabolic': ('prediabetes', 'type 2 diabetes', 'borderline glycemic'),
    'cardiovascular': ('hypertension', 'hyperlipidemia'),
    'pcos': ('pcos',),
}
LEVEL_CUTOFFS = (35, 65)           # score < 35 Low, 35-64 Moderate, >= 65 High
NAMES = {'glucose': 'Fasting glucose', 'hba1c': 'HbA1c', 'ldl': 'LDL cholesterol', 'hdl': 'HDL cholesterol',
         'triglycerides': 'Triglycerides', 'total_chol': 'Total cholesterol', 'sbp': 'Systolic blood pressure',
         'dbp': 'Diastolic blood pressure', 'rhr': 'Resting heart rate', 'weight': 'Weight', 'bmi': 'BMI',
         'steps': 'Daily steps', 'active': 'Active minutes', 'sleep': 'Sleep', 'hrv': 'Heart-rate variability',
         'stress': 'Stress score', 'cycle_length': 'Cycle length', 'age': 'Age'}
# Model inputs whose patient-facing name is not '<measure>_<part>' (cycle summaries and single-part inputs).
INPUT_NAMES = {'cycle_mean': 'Average cycle length', 'cycle_sd': 'Cycle-length variation',
               'cycle_long': 'Long cycles (over 35 days)', 'age': 'Age', 'female': 'Sex'}


def label(name: str) -> str:
    """Patient-facing name of a model input: 'ldl_out' -> 'LDL cholesterol', 'cycle_sd' -> 'Cycle-length variation'."""
    if name in INPUT_NAMES:
        return INPUT_NAMES[name]
    return NAMES.get(name.rsplit('_', 1)[0], name.replace('_', ' ').capitalize())


# ---------------------------------------------------------------- inputs (features)
def _days(dates):
    return (dates - dates[0]).days.to_numpy(dtype=float)


def features(book: dict) -> dict[str, float]:
    """Numbers describing one patient's measurements (NaN when a measurement was not uploaded).
    Per lab test: latest value and share of draws outside the listed range. Per daily measure: average of the
    latest 90 days and change per 90 days. Cycles: average length, variation, share longer than 35 days."""
    rec = PatientRecord(book)
    out: dict[str, float] = {}
    age = pd.to_numeric(pd.Series([rec.profile.get('Age')]), errors='coerce').iloc[0]
    out['age'] = float(age) if pd.notna(age) else np.nan
    sex = rec.sex.lower()
    out['female'] = 1.0 if sex.startswith('f') else 0.0 if sex.startswith('m') else np.nan
    for src, keys in SOURCES.items():
        for k in keys:
            s = rec.series.get(k)
            if src == 'labs':
                names = (f'{k}_last', f'{k}_out')
                if s is None or not len(s):
                    out.update(dict.fromkeys(names, np.nan)); continue
                v = s.values
                outside = np.zeros(len(v), bool)
                if s.ref_high is not None: outside |= v > s.ref_high
                if s.ref_low is not None: outside |= v < s.ref_low
                out[names[0]], out[names[1]] = float(v[-1]), float(outside.mean())
            elif src == 'cycles':
                names = ('cycle_mean', 'cycle_sd', 'cycle_long')
                if s is None or len(s) < 2:
                    out.update(dict.fromkeys(names, np.nan)); continue
                v = s.values
                out['cycle_mean'], out['cycle_sd'] = float(v.mean()), float(np.std(v, ddof=1))
                out['cycle_long'] = float((v > 35).mean())
            else:
                names = (f'{k}_mean90', f'{k}_slope')
                if s is None or not len(s):
                    out.update(dict.fromkeys(names, np.nan)); continue
                recent = s.dates >= s.dates[-1] - pd.Timedelta(days=90)
                out[names[0]] = float(np.nanmean(s.values[recent]))
                if len(s) >= 14 and _days(s.dates)[-1] > 30:
                    slope = np.polyfit(_days(s.dates), s.values, 1)[0] * 90
                    out[names[1]] = float(slope)
                else:
                    out[names[1]] = np.nan
    return out


def inputs_for(category: str) -> list[str]:
    names = ['age']
    for k in CATEGORY_INPUTS[category]:
        src = SOURCE_OF[k]
        if src == 'labs':
            names += [f'{k}_last', f'{k}_out']
        elif src == 'cycles':
            names += ['cycle_mean', 'cycle_sd', 'cycle_long']
        else:
            names += [f'{k}_mean90', f'{k}_slope']
    if category != 'pcos':
        names.append('female')
    return names


def source_of_input(name: str) -> str | None:
    if name.startswith('cycle_'):
        return 'cycles'
    return SOURCE_OF.get(name.rsplit('_', 1)[0])


def label_of(book: dict, category: str) -> int | None:
    """1 if the recorded medical history names a related condition, else 0 (None: category does not apply)."""
    rec = PatientRecord(book)
    if category == 'pcos' and not rec.sex.lower().startswith('f'):
        return None
    hist = rec.sheet('Medical History')
    text = ' '.join(str(v).lower() for v in hist.get('Finding / Event', pd.Series(dtype=str)).tolist())
    return int(any(term in text for term in LABELS[category]))


# ---------------------------------------------------------------- the model
def design(rows: list[dict], names: list[str], mean=None, std=None):
    """Standardise inputs; a missing input becomes 0 (= the training average), plus one 'missing' flag per source."""
    X = np.array([[r.get(n, np.nan) for n in names] for r in rows], dtype=float)
    if mean is None:
        mean = np.nanmean(X, axis=0)
        std = np.nanstd(X, axis=0)
        mean = np.where(np.isnan(mean), 0, mean)
        std = np.where((std == 0) | np.isnan(std), 1, std)
    Z = (X - mean) / std
    Z = np.where(np.isnan(Z), 0.0, Z)
    sources = sorted({source_of_input(n) for n in names if source_of_input(n)})
    flags = np.array([[float(all(np.isnan(r.get(n, np.nan)) for n in names if source_of_input(n) == s)) for s in sources]
                      for r in rows], dtype=float).reshape(len(rows), len(sources))
    return np.hstack([Z, flags]), mean, std, [f'{s}_missing' for s in sources]


def fit_logistic(X, y, l2=1.0, iters=50):
    """L2-regularised logistic regression fitted with Newton's method (no extra library needed)."""
    n, d = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(d + 1)
    reg = np.eye(d + 1) * l2
    reg[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        grad = Xb.T @ (p - y) + reg @ w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + reg
        step = np.linalg.solve(H, grad)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w[0], w[1:]


def auc(y, s):
    """Area under the ROC curve (probability a random positive scores above a random negative)."""
    y, s = np.asarray(y), np.asarray(s)
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return float('nan')
    ranks = pd.Series(np.concatenate([pos, neg])).rank().to_numpy()
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


_cache: dict = {}


def load_model(path: Path = MODEL_FILE) -> dict | None:
    key = (str(path), path.stat().st_mtime if path.exists() else None)
    if key not in _cache:
        _cache.clear()
        _cache[key] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    return _cache[key]


def _describe(name: str, value: float) -> str:
    base, _, part = name.rpartition('_')
    if name.startswith('cycle_'):
        return {'cycle_mean': f'average cycle {value:.0f} days', 'cycle_sd': f'cycle length varies by {value:.0f} days',
                'cycle_long': f'{value * 100:.0f}% of cycles longer than 35 days'}[name]
    if name == 'age':
        return f'age {value:.0f}'
    label = NAMES.get(base, base)
    if part == 'last':
        return f'{label} {value:g} (latest)'
    if part == 'out':
        return f'{label} outside the listed range in {value * 100:.0f}% of draws'
    if part == 'mean90':
        return f'{label} average {value:,.0f} (last 90 days)' if abs(value) >= 100 else f'{label} average {value:.1f} (last 90 days)'
    if part == 'slope':
        return f'{label} {"rising" if value > 0 else "falling"} ({value:+.1f} per 90 days)'
    return name


def score(book: dict, model: dict | None = None) -> dict[str, dict]:
    """Risk score (0-100), level and main contributing measures for each category the model covers."""
    model = model or load_model()
    if not model:
        return {}
    feats = features(book)
    results = {}
    for category, m in model['categories'].items():
        if category == 'pcos' and feats.get('female') != 1.0:
            continue
        X, _, _, flag_names = design([feats], m['inputs'], np.array(m['mean']), np.array(m['std']))
        w = np.array(m['weights'])
        z = float(m['bias'] + X[0] @ w)
        p = 1 / (1 + np.exp(-z))
        risk_score = int(round(100 * p))
        level = 'High' if risk_score >= LEVEL_CUTOFFS[1] else 'Moderate' if risk_score >= LEVEL_CUTOFFS[0] else 'Low'
        contrib = []
        for i, name in enumerate(m['inputs']):
            value = feats.get(name, np.nan)
            if not np.isnan(value) and abs(X[0, i] * w[i]) > 0.25:      # only measures that clearly move the score
                contrib.append((X[0, i] * w[i], name, value))
        contrib.sort(reverse=True)
        raising = [{'name': label(n), 'detail': _describe(n, v)} for c, n, v in contrib if c > 0][:3]
        lowering = [{'name': label(n), 'detail': _describe(n, v)} for c, n, v in contrib[::-1] if c < 0][:2]
        used = sorted({source_of_input(n) for n in m['inputs'] if source_of_input(n) and not np.isnan(feats.get(n, np.nan))})
        results[category] = {'score': risk_score, 'level': level, 'raising': raising, 'lowering': lowering,
                             'sources_used': used, 'test_auc': m['metrics']['test_auc'],
                             'trained_on': model['trained_on'], 'tested_on': m['metrics']['test_n']}
    return results
