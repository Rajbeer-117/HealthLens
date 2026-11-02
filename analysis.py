"""Explainable, statistics-based pattern detection (no black-box models).

Methods
-------
* Sustained trend  : Mann-Kendall test (is there a consistent direction?) + Theil-Sen slope
                     (how fast?), on weekly averages for daily data and on raw draws for labs.
                     Replaces Deliverable 1's first-vs-last comparison.
* Anomaly          : robust z-score of a reading against the person's own previous 28 days
                     (median / MAD). Consecutive unusual days are grouped into one episode.
* Reference range  : lab values compared with the reference range printed in the workbook.
* Relationship     : Spearman rank correlation between weekly averages of two signals.
* Medication window: average before vs. after a medication start (context, not causation).
* Data quality     : coverage, gaps and sparse sources are reported explicitly.
"""
from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import pandas as pd

from .data import PatientRecord, Series
from .evidence import D, EvidenceRegistry, fmt

# Tunable, documented demo thresholds (NOT clinical cut-offs)
TREND_P_DAILY = 0.05
TREND_P_LAB = 0.10          # labs have only ~5 draws, so a slightly looser test is used
MIN_CHANGE_DAILY = 0.05      # 5% change between first and last 4 weeks (default)
# Slow-moving signals: a smaller relative change is still meaningful
MIN_CHANGE_BY_METRIC = {'weight': 0.02, 'bmi': 0.02, 'sbp': 0.03, 'dbp': 0.03, 'rhr': 0.03, 'spo2': 0.01}
MIN_CHANGE_LAB = 0.03
ANOMALY_Z = 3.5
ANOMALY_WINDOW = 28
RELATIONSHIP_RHO = 0.5
MIN_WEEKS = 8
# An anomaly must also be practically large, not just statistically unusual (smooth signals such
# as weight have a tiny MAD, so tiny wobbles would otherwise be flagged).
ANOMALY_MIN_ABS = {'sbp': 12, 'dbp': 8, 'rhr': 8, 'weight': 1.5, 'spo2': 2.5, 'steps': 2500,
                   'sleep': 1.5, 'active': 15, 'hrv': 10, 'sedentary': 2, 'stress': 2}


def lower_label(label: str) -> str:
    """'Fasting glucose' -> 'fasting glucose', but keep 'HbA1c' / 'LDL cholesterol' as written."""
    return label if any(c.isupper() for c in label[1:]) else label.lower()


# ---------------------------------------------------------------- statistics helpers
def mann_kendall(values: np.ndarray) -> tuple[float, float]:
    """Return (S statistic, two-sided p-value) with tie correction."""
    x = np.asarray(values, dtype=float)
    n = len(x)
    if n < 3:
        return 0.0, 1.0
    s = 0.0
    for i in range(n - 1):
        s += np.sign(x[i + 1:] - x[i]).sum()
    _, counts = np.unique(x, return_counts=True)
    var = (n * (n - 1) * (2 * n + 5) - sum(c * (c - 1) * (2 * c + 5) for c in counts)) / 18
    if var <= 0:
        return s, 1.0
    z = (s - 1) / math.sqrt(var) if s > 0 else (s + 1) / math.sqrt(var) if s < 0 else 0.0
    p = math.erfc(abs(z) / math.sqrt(2))
    return float(s), float(p)


def theil_sen(t: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Median of pairwise slopes (robust to outliers). Returns (slope, intercept)."""
    slopes = [(y[j] - y[i]) / (t[j] - t[i]) for i, j in combinations(range(len(t)), 2) if t[j] != t[i]]
    if not slopes:
        return 0.0, float(np.median(y))
    slope = float(np.median(slopes))
    return slope, float(np.median(y - slope * t))


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def weekly(s: Series) -> pd.Series:
    w = pd.Series(s.values, index=s.dates).resample('W-MON', label='left', closed='left').mean()
    return w.dropna()


# ---------------------------------------------------------------- trends
def trend(s: Series, ev: EvidenceRegistry) -> dict | None:
    if s.kind == 'daily':
        w = weekly(s)
        if len(w) < MIN_WEEKS:
            return None
        dates, y = w.index, w.to_numpy()
        base, recent = y[:4].mean(), y[-4:].mean()
        p_cut, unit_txt = TREND_P_DAILY, f'{len(w)} weekly averages'
        min_change = MIN_CHANGE_BY_METRIC.get(s.key, MIN_CHANGE_DAILY)
        compare_txt = 'first 4 weeks vs last 4 weeks'
    elif s.kind in ('lab', 'cycle'):
        if len(s) < 3:
            return None
        dates, y = s.dates, s.values
        base, recent = y[0], y[-1]
        p_cut, min_change = TREND_P_LAB, MIN_CHANGE_LAB
        unit_txt = f'{len(s)} {"lab draws" if s.kind == "lab" else "cycles"}'
        compare_txt = 'first vs latest value'
    else:
        return None
    t = (dates - dates[0]).days.to_numpy(dtype=float)
    _, p = mann_kendall(y)
    slope, intercept = theil_sen(t, y)
    pct = (recent - base) / abs(base) if base else 0.0
    sustained = p < p_cut and abs(pct) >= min_change and np.sign(slope) == np.sign(recent - base)
    direction = 'stable'
    if sustained:
        direction = 'rising' if slope > 0 else 'falling'
    result = {
        'metric': s.key, 'label': s.label, 'unit': s.unit, 'kind': s.kind, 'n': int(len(y)),
        'direction': direction, 'sustained': bool(sustained), 'p_value': round(p, 4),
        'slope_per_month': round(slope * 30.44, 3), 'baseline': round(float(base), 2),
        'recent': round(float(recent), 2), 'change_pct': round(pct * 100, 1),
        'start': dates[0].strftime('%Y-%m-%d'), 'end': dates[-1].strftime('%Y-%m-%d'),
        'trendline': [
            {'date': dates[0].strftime('%Y-%m-%d'), 'value': round(intercept, 3)},
            {'date': dates[-1].strftime('%Y-%m-%d'), 'value': round(intercept + slope * t[-1], 3)},
        ],
        'evidence': None,
    }
    if sustained:
        word = 'rose' if slope > 0 else 'fell'
        result['evidence'] = ev.add(
            'trend',
            f'{s.label} {word} consistently from {fmt(base, 2)} to {fmt(recent, 2)} {s.unit} '
            f'({pct * 100:+.1f}%, {compare_txt}) between {D(dates[0])} and {D(dates[-1])}; '
            f'about {slope * 30.44:+.2f} {s.unit} per month across {unit_txt}.',
            metric=s.key, source=s.sheet,
            method=f'Mann-Kendall trend test (p = {p:.3f}) + Theil-Sen slope on {unit_txt}.',
            values={'baseline': result['baseline'], 'recent': result['recent'], 'change_pct': result['change_pct'],
                    'p_value': result['p_value'], 'n': result['n']})
    return result


# ---------------------------------------------------------------- anomalies
def anomalies(s: Series, ev: EvidenceRegistry, max_episodes=3) -> list[dict]:
    if s.kind != 'daily' or len(s) < ANOMALY_WINDOW + 7 or s.key not in ANOMALY_MIN_ABS:
        return []
    x = pd.Series(s.values, index=s.dates)
    prior = x.shift(1).rolling(ANOMALY_WINDOW, min_periods=14)
    med = prior.median()
    mad = (x.shift(1) - med).abs().rolling(ANOMALY_WINDOW, min_periods=14).median()
    z = 0.6745 * (x - med) / mad.replace(0, np.nan)
    big = (x - med).abs() >= ANOMALY_MIN_ABS[s.key]
    flagged = z[(z.abs() >= ANOMALY_Z) & big].dropna()
    if flagged.empty:
        return []
    episodes, current = [], [flagged.index[0]]
    for d in flagged.index[1:]:
        if (d - current[-1]).days <= 2:
            current.append(d)
        else:
            episodes.append(current)
            current = [d]
    episodes.append(current)
    scored = sorted(episodes, key=lambda ep: max(abs(z[d]) for d in ep), reverse=True)[:max_episodes]
    out = []
    for ep in sorted(scored):
        peak = max(ep, key=lambda d: abs(z[d]))
        val, base = float(x[peak]), float(med[peak])
        direction = 'higher' if val > base else 'lower'
        span = f'on {D(ep[0])}' if len(ep) == 1 else f'from {D(ep[0])} to {D(ep[-1])} ({len(ep)} unusual days)'
        eid = ev.add(
            'anomaly',
            f'{s.label} was unusually {direction} than this person\'s usual level {span}: '
            f'{fmt(val, 1)} {s.unit} vs a typical {fmt(base, 1)} {s.unit} over the previous {ANOMALY_WINDOW} days.',
            metric=s.key, source=s.sheet,
            method=f'Robust z-score vs prior {ANOMALY_WINDOW}-day median/MAD (|z| = {abs(z[peak]):.1f} ≥ {ANOMALY_Z}).',
            values={'value': round(val, 2), 'typical': round(base, 2), 'z': round(float(z[peak]), 2), 'days': len(ep)})
        out.append({'metric': s.key, 'label': s.label, 'unit': s.unit, 'date': peak.strftime('%Y-%m-%d'),
                    'start': ep[0].strftime('%Y-%m-%d'), 'end': ep[-1].strftime('%Y-%m-%d'),
                    'value': round(val, 2), 'typical': round(base, 2), 'direction': direction,
                    'z': round(float(z[peak]), 2), 'days': len(ep), 'evidence': eid,
                    'points': [{'date': d.strftime('%Y-%m-%d'), 'value': float(x[d])} for d in ep]})
    return out


# ---------------------------------------------------------------- lab reference ranges
def lab_reference(s: Series, ev: EvidenceRegistry) -> dict | None:
    if s.kind != 'lab' or (s.ref_low is None and s.ref_high is None):
        return None
    above = s.values > s.ref_high if s.ref_high is not None else np.zeros(len(s), bool)
    below = s.values < s.ref_low if s.ref_low is not None else np.zeros(len(s), bool)
    outside = above | below
    latest_val, latest_date = float(s.values[-1]), s.dates[-1]
    latest_state = 'above' if above[-1] else 'below' if below[-1] else 'within'
    if not outside.any():
        return {'metric': s.key, 'label': s.label, 'state': 'within', 'evidence': None}
    eid = ev.add(
        'lab_range',
        f'Latest {s.label} was {fmt(latest_val, 2)} {s.unit} on {D(latest_date)}, {latest_state} the workbook\'s '
        f'listed reference range ({s.ref_text} {s.unit}); {int(outside.sum())} of {len(s)} draws were outside that range.',
        metric=s.key, source='Lab Results',
        method='Comparison with the reference range recorded next to each lab result in the workbook.',
        values={'latest': round(latest_val, 2), 'reference': s.ref_text, 'outside': int(outside.sum()),
                'draws': len(s), 'latest_state': latest_state})
    return {'metric': s.key, 'label': s.label, 'unit': s.unit, 'latest': round(latest_val, 2),
            'date': latest_date.strftime('%Y-%m-%d'), 'reference': s.ref_text, 'state': latest_state,
            'outside': int(outside.sum()), 'draws': len(s), 'mostly_above': bool(above.sum() > len(s) / 2),
            'mostly_below': bool(below.sum() > len(s) / 2), 'evidence': eid}


# ---------------------------------------------------------------- relationships between signals
RELATIONSHIP_PAIRS = [
    ('sleep', 'stress'), ('sleep', 'hrv'), ('steps', 'rhr'), ('stress', 'sbp'),
    ('stress', 'rhr'), ('active', 'sbp'), ('sleep', 'rhr'), ('steps', 'weight'),
]


def relationships(rec: PatientRecord, ev: EvidenceRegistry) -> list[dict]:
    out = []
    for a, b in RELATIONSHIP_PAIRS:
        if a not in rec.series or b not in rec.series:
            continue
        sa, sb = rec.series[a], rec.series[b]
        joined = pd.concat([weekly(sa), weekly(sb)], axis=1, join='inner').dropna()
        if len(joined) < 12:
            continue
        rho = spearman(joined.iloc[:, 0].to_numpy(), joined.iloc[:, 1].to_numpy())
        if abs(rho) < RELATIONSHIP_RHO:
            continue
        together = 'higher' if rho > 0 else 'lower'
        eid = ev.add(
            'relationship',
            f'Across {len(joined)} weeks, weeks with higher {lower_label(sa.label)} tended to have {together} '
            f'{lower_label(sb.label)} (Spearman ρ = {rho:+.2f}). This shows the two moved together, not that one caused the other.',
            metric=f'{a}+{b}', source=f'{sa.sheet} / {sb.sheet}',
            method='Spearman rank correlation of weekly averages (|ρ| ≥ 0.5 shown).',
            values={'rho': round(rho, 2), 'weeks': len(joined)})
        out.append({'a': a, 'b': b, 'label_a': sa.label, 'label_b': sb.label, 'rho': round(rho, 2),
                    'weeks': len(joined), 'evidence': eid})
    return out


# ---------------------------------------------------------------- medication context
INDICATION_METRICS = [
    (('metabolic', 'glyc', 'diabet', 'pcos'), ['glucose', 'hba1c', 'weight']),
    (('lipid', 'cholesterol'), ['ldl', 'total_chol', 'triglycerides']),
    (('blood pressure', 'hypertens'), ['sbp', 'dbp']),
    (('thyroid',), ['tsh']),
    (('vitamin d',), ['vitamin_d']),
    (('iron',), ['ferritin', 'hemoglobin']),
]


def medication_context(rec: PatientRecord, ev: EvidenceRegistry) -> list[dict]:
    meds = rec.sheet('Medication History')
    out = []
    if meds.empty or 'Start Date' not in meds.columns:
        return out
    seen = set()
    for _, row in meds.iterrows():
        start = row.get('Start Date')
        name = str(row.get('Medication', '')).strip()
        indication = str(row.get('Indication', '')).lower()
        if pd.isna(start) or not name or name.lower().startswith('no active'):
            continue
        base_name = name.split()[0].lower()
        if base_name in seen:          # a formulation change is not a new start
            continue
        seen.add(base_name)
        metrics = next((m for keys, m in INDICATION_METRICS if any(k in indication for k in keys)), [])
        for key in metrics:
            s = rec.series.get(key)
            if s is None:
                continue
            start = pd.Timestamp(start)
            if s.kind == 'daily':
                before = s.values[(s.dates >= start - pd.Timedelta(days=90)) & (s.dates < start)]
                after = s.values[(s.dates >= start + pd.Timedelta(days=14)) & (s.dates < start + pd.Timedelta(days=120))]
                need, window = 14, '90 days before vs days 14–120 after'
            else:
                before, after = s.values[s.dates < start], s.values[s.dates >= start]
                need, window = 1, 'all draws before vs after'
            if len(before) < need or len(after) < need:
                continue
            b, a = float(np.mean(before)), float(np.mean(after))
            eid = ev.add(
                'medication',
                f'{name} started on {D(start)} ({row.get("Indication", "indication not recorded")}). '
                f'Average {lower_label(s.label)} was {fmt(b, 1)} {s.unit} before and {fmt(a, 1)} {s.unit} after '
                f'({(a - b) / abs(b) * 100 if b else 0:+.1f}%). Timing alone does not show the medication caused this.',
                metric=key, source='Medication History + ' + s.sheet,
                method=f'Mean comparison, {window} the start date.',
                values={'before': round(b, 2), 'after': round(a, 2), 'n_before': int(len(before)), 'n_after': int(len(after))})
            out.append({'medication': name, 'start': start.strftime('%Y-%m-%d'), 'metric': key, 'label': s.label,
                        'unit': s.unit, 'before': round(b, 2), 'after': round(a, 2), 'evidence': eid})
    # adherence context
    adh = rec.sheet('Medication Adherence')
    if not adh.empty and {'Medication ID', 'Estimated Adherence %'}.issubset(adh.columns):
        names = dict(zip(meds.get('Medication ID', []), meds.get('Medication', [])))
        for mid, grp in adh.groupby('Medication ID'):
            vals = pd.to_numeric(grp['Estimated Adherence %'], errors='coerce').dropna()
            if vals.empty:
                continue
            nm = names.get(mid, mid)
            eid = ev.add(
                'adherence',
                f'Estimated adherence for {nm} averaged {vals.mean():.0f}% over {len(vals)} monthly estimates '
                f'(range {vals.min():.0f}–{vals.max():.0f}%; refill or self-report estimates).',
                metric='adherence', source='Medication Adherence',
                method='Average of monthly adherence estimates recorded in the workbook.',
                values={'mean': round(float(vals.mean()), 1), 'months': int(len(vals))})
            out.append({'medication': str(nm), 'metric': 'adherence', 'label': 'Estimated adherence',
                        'unit': '%', 'before': None, 'after': round(float(vals.mean()), 1), 'evidence': eid})
    return out


# ---------------------------------------------------------------- symptoms & history
def recurring_symptoms(rec: PatientRecord, ev: EvidenceRegistry, min_count=3) -> dict[str, dict]:
    sym = rec.sheet('Symptoms Timeline')
    out = {}
    if sym.empty or 'Symptom' not in sym.columns:
        return out
    for name, grp in sym.dropna(subset=['Symptom']).groupby('Symptom'):
        if len(grp) < min_count:
            continue
        dates = grp['Date'].dropna()
        sev = grp['Severity'].value_counts().to_dict() if 'Severity' in grp.columns else {}
        sev_txt = ', '.join(f'{v} {k.lower()}' for k, v in sev.items())
        eid = ev.add(
            'symptom',
            f'"{name}" was recorded {len(grp)} times between {D(dates.min())} and {D(dates.max())} ({sev_txt}).',
            metric='symptom', source='Symptoms Timeline', method='Count of recorded symptom entries.',
            values={'count': int(len(grp))})
        out[str(name).lower()] = {'symptom': str(name), 'count': int(len(grp)), 'evidence': eid}
    return out


def history_context(rec: PatientRecord, ev: EvidenceRegistry) -> list[dict]:
    hist = rec.sheet('Medical History')
    out = []
    col = 'Finding / Event' if 'Finding / Event' in hist.columns else None
    if hist.empty or not col:
        return out
    for _, row in hist.iterrows():
        finding = str(row.get(col, '')).strip()
        if not finding or finding.lower() in ('annual preventive assessment', 'longitudinal health review'):
            continue
        date = row.get('Date')
        when = f' on {D(date)}' if pd.notna(date) else ''
        eid = ev.add('history', f'Medical history records "{finding}"{when} (status: {row.get("Status", "not recorded")}).',
                     metric='history', source='Medical History', method='Recorded entry (not inferred by HealthLens).',
                     values={'finding': finding})
        out.append({'finding': finding, 'date': date.strftime('%Y-%m-%d') if pd.notna(date) else '', 'evidence': eid})
    return out


# ---------------------------------------------------------------- data quality
def data_quality(rec: PatientRecord, ev: EvidenceRegistry) -> dict:
    start, end = rec.window
    expected_days = int((end - start).days) + 1 if start is not None else 0
    sources, issues = [], []
    for sheet, cols in [('Vitals Daily', ['Systolic BP', 'Diastolic BP', 'Resting HR', 'Weight kg']),
                        ('Wearable Daily', ['Steps', 'Sleep Hours', 'Active Minutes', 'HRV ms', 'Stress Score 1-10'])]:
        frame = rec.sheet(sheet)
        if frame.empty or 'Date' not in frame.columns:
            sources.append({'source': sheet, 'status': 'missing', 'coverage': 0, 'detail': 'Sheet not present'})
            issues.append(ev.add('quality', f'The {sheet} sheet is missing, so related signals cannot be analysed.',
                                 source=sheet, method='Sheet presence check.'))
            continue
        dates = frame['Date'].dropna().drop_duplicates().sort_values()
        coverage = len(dates) / expected_days * 100 if expected_days else 0
        gaps = dates.diff().dt.days.fillna(1)
        longest = int(gaps.max() - 1) if len(gaps) else 0
        missing_cells = {c: int(frame[c].isna().sum()) for c in cols if c in frame.columns}
        missing_cells = {c: m for c, m in missing_cells.items() if m}
        detail = f'{len(dates)} of {expected_days} days recorded'
        if longest > 0:
            detail += f'; longest gap {longest} day(s)'
        if missing_cells:
            detail += '; blank values: ' + ', '.join(f'{c} ({m})' for c, m in missing_cells.items())
        sources.append({'source': sheet, 'status': 'ok' if coverage >= 90 and not missing_cells else 'partial',
                        'coverage': round(coverage, 1), 'detail': detail})
        if coverage < 90 or longest >= 7 or missing_cells:
            issues.append(ev.add('quality', f'{sheet}: {detail}. Trends near gaps are less certain.',
                                 source=sheet, method='Coverage and gap check over the data window.'))
    labs = rec.sheet('Lab Results')
    if not labs.empty and 'Test Name' in labs.columns:
        per_test = labs.groupby('Test Name').size()
        sparse = per_test[per_test < 3]
        draws = labs['Date'].dropna().nunique()
        sources.append({'source': 'Lab Results', 'status': 'ok' if sparse.empty else 'partial',
                        'coverage': None, 'detail': f'{draws} lab draw dates; {len(per_test)} tests'
                        + (f'; fewer than 3 results for {", ".join(sparse.index[:6])}' if not sparse.empty else '')})
        issues.append(ev.add('quality', f'Lab results come from only {draws} draw dates in the record, so lab trends rest on few points.',
                             source='Lab Results', method='Count of distinct lab draw dates.'))
    else:
        sources.append({'source': 'Lab Results', 'status': 'missing', 'coverage': 0, 'detail': 'Sheet not present'})
    for sheet in ('Symptoms Timeline', 'Medication History', 'Habits Weekly', 'Medical History', 'Medication Adherence'):
        frame = rec.sheet(sheet)
        sources.append({'source': sheet, 'status': 'ok' if not frame.empty else 'missing', 'coverage': None,
                        'detail': f'{len(frame)} rows' if not frame.empty else 'Sheet not present or empty'})
    cyc = rec.series.get('cycle_length')
    if rec.sex.lower().startswith('f'):
        sources.append({'source': 'Women Health', 'status': 'ok' if cyc is not None else 'missing', 'coverage': None,
                        'detail': f'{len(cyc)} cycles recorded' if cyc is not None else 'No cycle data recorded'})
    else:
        sources.append({'source': 'Women Health', 'status': 'n/a', 'coverage': None, 'detail': 'Not applicable for this profile'})
    return {'window': {'start': start.strftime('%Y-%m-%d') if start is not None else None,
                       'end': end.strftime('%Y-%m-%d') if end is not None else None, 'days': expected_days},
            'sources': sources, 'evidence': issues}


# ---------------------------------------------------------------- multi-signal unusual days
def co_occurring(anomaly_list: list[dict], rec: PatientRecord, ev: EvidenceRegistry, window_days=1) -> list[dict]:
    """Days where two or more different signals were unusual together (± window), plus nearby symptoms."""
    if len(anomaly_list) < 2:
        return []
    items = sorted(anomaly_list, key=lambda a: a['date'])
    used, out = set(), []
    sym = rec.sheet('Symptoms Timeline')
    for i, a in enumerate(items):
        if i in used:
            continue
        da = pd.Timestamp(a['date'])
        group = [i] + [j for j in range(i + 1, len(items))
                       if j not in used and abs((pd.Timestamp(items[j]['date']) - da).days) <= window_days]
        metrics = {items[j]['metric'] for j in group}
        if len(metrics) < 2 or metrics <= {'sbp', 'dbp'}:
            continue
        used.update(group)
        parts = [f'{lower_label(items[j]["label"])} {items[j]["direction"]} ({items[j]["evidence"]})' for j in group]
        near = []
        if not sym.empty and 'Date' in sym.columns:
            close = sym[(sym['Date'] - da).abs() <= pd.Timedelta(days=3)]
            near = sorted(set(close['Symptom'].astype(str)))
        sym_txt = f' Symptom entries within 3 days: {", ".join(near)}.' if near else ''
        eid = ev.add('co_occurrence',
                     f'Around {D(da)}, several signals were unusual at the same time: {"; ".join(parts)}.{sym_txt}',
                     metric='multi', source='Vitals Daily / Wearable Daily / Symptoms Timeline',
                     method=f'Anomaly episodes in different signals within ±{window_days} day; symptoms within ±3 days.',
                     values={'signals': sorted(metrics), 'symptoms': near})
        out.append({'date': a['date'], 'signals': [items[j]['label'] for j in group], 'symptoms': near,
                    'linked': [items[j]['evidence'] for j in group], 'evidence': eid})
    return out
