"""Multi-signal patterns for the three validation scenarios.

Each scenario is a list of transparent checks. A check looks at one signal family and returns
'signal' (something noteworthy, with evidence), 'quiet' (data present, nothing noteworthy) or
'missing' (no data). The scenario status is a COUNT of related signals — it is not a risk score,
probability or diagnosis.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .evidence import D, EvidenceRegistry, fmt

STATUS_TEXT = {
    'pattern': 'Several related signals',
    'partial': 'Some related signals',
    'none': 'No notable pattern',
    'insufficient': 'Not enough data',
    'not_applicable': 'Not applicable',
}


class Ctx:
    def __init__(self, rec, ev: EvidenceRegistry, trends, labs, symptoms, history, meds):
        self.rec, self.ev = rec, ev
        self.trends = {t['metric']: t for t in trends}
        self.labs = {lab['metric']: lab for lab in labs if lab}
        self.symptoms, self.history, self.meds = symptoms, history, meds

    def has(self, key):
        return key in self.rec.series

    def rising(self, key):
        t = self.trends.get(key)
        return t if t and t['direction'] == 'rising' else None

    def falling(self, key):
        t = self.trends.get(key)
        return t if t and t['direction'] == 'falling' else None

    def lab_state(self, key, state):
        lab = self.labs.get(key)
        return lab if lab and lab.get('evidence') and (lab['state'] == state or lab.get(f'mostly_{state}')) else None


def check(name, ctx: Ctx, keys, finders, quiet_text):
    """Run finder functions; return a check result."""
    if not any(ctx.has(k) for k in keys):
        return {'signal': name, 'status': 'missing', 'text': 'No data recorded for this signal.', 'evidence': []}
    found = [f for f in (fn() for fn in finders) if f]
    if not found:
        return {'signal': name, 'status': 'quiet', 'text': quiet_text, 'evidence': []}
    ids, texts = [], []
    for eid, text in found:
        if eid and eid not in ids:
            ids.append(eid)
        texts.append(text)
    return {'signal': name, 'status': 'signal', 'text': '; '.join(texts), 'evidence': ids}


def t_up(ctx, key, label=None):
    t = ctx.rising(key)
    return (t['evidence'], f'{label or t["label"]} trending up ({t["change_pct"]:+.1f}%)') if t else None


def t_down(ctx, key, label=None):
    t = ctx.falling(key)
    return (t['evidence'], f'{label or t["label"]} trending down ({t["change_pct"]:+.1f}%)') if t else None


def lab_hi(ctx, key):
    lab = ctx.lab_state(key, 'above')
    return (lab['evidence'], f'{lab["label"]} above listed range in {lab["outside"]}/{lab["draws"]} draws') if lab else None


def lab_lo(ctx, key):
    lab = ctx.lab_state(key, 'below')
    return (lab['evidence'], f'{lab["label"]} below listed range in {lab["outside"]}/{lab["draws"]} draws') if lab else None


def symptom(ctx, *names):
    for n in names:
        s = ctx.symptoms.get(n)
        if s:
            return s['evidence'], f'"{s["symptom"]}" recorded {s["count"]} times'
    return None


def context_items(ctx, history_terms, med_terms):
    ids = [h['evidence'] for h in ctx.history if any(t in h['finding'].lower() for t in history_terms)]
    ids += [m['evidence'] for m in ctx.meds if m['metric'] != 'adherence' and any(t in m['medication'].lower() for t in med_terms)]
    ids += [m['evidence'] for m in ctx.meds if m['metric'] == 'adherence' and any(t in m['medication'].lower() for t in med_terms)]
    return list(dict.fromkeys(ids))


def bp_recent_share(ctx: Ctx):
    """Share of the last 28 recorded days with BP at or above 130/80 (illustrative, not a diagnosis)."""
    sbp, dbp = ctx.rec.series.get('sbp'), ctx.rec.series.get('dbp')
    if sbp is None or dbp is None:
        return None
    df = pd.DataFrame({'s': pd.Series(sbp.values, index=sbp.dates), 'd': pd.Series(dbp.values, index=dbp.dates)}).dropna()
    if len(df) < 28:
        return None
    last = df.tail(28)
    first = df.head(28)
    share = float(((last.s >= 130) | (last.d >= 80)).mean())
    share0 = float(((first.s >= 130) | (first.d >= 80)).mean())
    if share < 0.5:
        return None
    eid = ctx.ev.add(
        'bp_share',
        f'In the last 28 recorded days ({D(last.index[0])} to {D(last.index[-1])}), {share * 100:.0f}% of readings were '
        f'at or above 130/80 mmHg (first 28 days: {share0 * 100:.0f}%). Average {last.s.mean():.0f}/{last.d.mean():.0f} mmHg.',
        metric='sbp', source='Vitals Daily',
        method='Share of daily readings with systolic ≥ 130 or diastolic ≥ 80 (illustrative educational cut-off).',
        values={'share_recent': round(share * 100), 'share_first': round(share0 * 100)})
    return eid, f'{share * 100:.0f}% of recent BP readings ≥ 130/80'


def cycle_pattern(ctx: Ctx):
    cyc = ctx.rec.series.get('cycle_length')
    if cyc is None or len(cyc) < 3:
        return None
    v = cyc.values
    long_share = float((v > 35).mean())
    sd = float(np.std(v, ddof=1))
    if long_share < 1 / 3 and sd < 7:
        return None
    eid = ctx.ev.add(
        'cycle',
        f'{len(v)} menstrual cycles were recorded between {D(cyc.dates[0])} and {D(cyc.dates[-1])}: average length '
        f'{v.mean():.0f} days (range {v.min():.0f}–{v.max():.0f}, SD {sd:.1f}); {int((v > 35).sum())} cycles were longer than 35 days.',
        metric='cycle_length', source='Women Health',
        method='Cycle-length summary; cycles > 35 days or SD ≥ 7 days are highlighted (educational heuristic).',
        values={'mean': round(float(v.mean()), 1), 'sd': round(sd, 1), 'long_cycles': int((v > 35).sum()), 'n': len(v)})
    return eid, f'{int((v > 35).sum())}/{len(v)} cycles longer than 35 days (SD {sd:.1f} d)'


def summarise(name, key, checks, context, applicable=True, reason='', anchor=None):
    if not applicable:
        return {'key': key, 'name': name, 'status': 'not_applicable', 'status_text': STATUS_TEXT['not_applicable'],
                'checks': [], 'signal_count': 0, 'context': [], 'note': reason, 'evidence': []}
    signals = [c for c in checks if c['status'] == 'signal']
    present = [c for c in checks if c['status'] != 'missing']
    if len(present) < 2:
        status = 'insufficient'
    elif len(signals) >= 3:
        status = 'pattern'
    elif len(signals) == 2:
        status = 'partial'
    else:
        status = 'none'
    # Anchor signal: the signal that defines the scenario. Without it, the other signals overlap with
    # other scenarios (e.g. glucose, lipids), so the scenario is not reported as a pattern.
    if anchor and status in ('pattern', 'partial'):
        a = next((c for c in checks if c['signal'] == anchor), None)
        if a is None or a['status'] != 'signal':
            status = 'none'
            reason = f'{anchor} shows no noteworthy finding, so the other signals are covered by the other scenarios.'
    elif anchor and status == 'none':
        a = next((c for c in checks if c['signal'] == anchor), None)
        if a and a['status'] == 'signal':
            status = 'partial'
    evidence = list(dict.fromkeys(e for c in signals for e in c['evidence']))
    return {'key': key, 'name': name, 'status': status, 'status_text': STATUS_TEXT[status], 'checks': checks,
            'signal_count': len(signals), 'checked': len(present), 'context': context,
            'note': reason, 'evidence': evidence}


def metabolic(ctx: Ctx):
    checks = [
        check('Fasting glucose', ctx, ['glucose'], [lambda: t_up(ctx, 'glucose'), lambda: lab_hi(ctx, 'glucose')],
              'No sustained rise and within listed range'),
        check('HbA1c', ctx, ['hba1c'], [lambda: t_up(ctx, 'hba1c'), lambda: lab_hi(ctx, 'hba1c')],
              'No sustained rise and within listed range'),
        check('Body weight', ctx, ['weight'], [lambda: t_up(ctx, 'weight')], 'No sustained weight increase'),
        check('Physical activity', ctx, ['steps', 'active'],
              [lambda: t_down(ctx, 'steps', 'Daily steps'), lambda: t_down(ctx, 'active')], 'No sustained drop in activity'),
        check('Blood lipids (TG / HDL)', ctx, ['triglycerides', 'hdl'],
              [lambda: lab_hi(ctx, 'triglycerides'), lambda: lab_lo(ctx, 'hdl')], 'Triglycerides and HDL within listed range'),
        check('Related symptoms', ctx, ['glucose'],
              [lambda: symptom(ctx, 'increased thirst', 'frequent urination', 'fatigue')], 'No recurring thirst/urination/fatigue entries'),
    ]
    ctxt = context_items(ctx, ('glyc', 'diabet'), ('metformin',))
    return summarise('Metabolic', 'metabolic', checks, ctxt)


def cardiovascular(ctx: Ctx):
    checks = [
        check('Blood pressure', ctx, ['sbp', 'dbp'],
              [lambda: t_up(ctx, 'sbp'), lambda: t_up(ctx, 'dbp'), lambda: bp_recent_share(ctx)],
              'No sustained rise; most recent readings below 130/80'),
        check('Resting heart rate', ctx, ['rhr'], [lambda: t_up(ctx, 'rhr')], 'No sustained rise'),
        check('Heart-rate variability', ctx, ['hrv'], [lambda: t_down(ctx, 'hrv')], 'No sustained drop'),
        check('Cholesterol', ctx, ['ldl', 'total_chol'],
              [lambda: t_up(ctx, 'ldl'), lambda: lab_hi(ctx, 'ldl'), lambda: lab_hi(ctx, 'total_chol')],
              'No sustained rise and within listed range'),
        check('Stress & sleep', ctx, ['stress', 'sleep'],
              [lambda: t_up(ctx, 'stress'), lambda: t_down(ctx, 'sleep')], 'No sustained change'),
        check('Related symptoms', ctx, ['sbp'],
              [lambda: symptom(ctx, 'dizziness', 'shortness of breath', 'headache')], 'No recurring related symptom entries'),
    ]
    ctxt = context_items(ctx, ('hypertens', 'lipid', 'cholesterol'), ('amlodipine', 'atorvastatin'))
    return summarise('Cardiovascular', 'cardiovascular', checks, ctxt)


def pcos(ctx: Ctx):
    female = ctx.rec.sex.lower().startswith('f')
    has_cycles = ctx.has('cycle_length')
    if not female:
        return summarise("Women's health (PCOS)", 'pcos', [], [], applicable=False,
                         reason='Profile is not female, so cycle-based signals do not apply.')
    if not has_cycles:
        return summarise("Women's health (PCOS)", 'pcos', [], [], applicable=False,
                         reason='No menstrual-cycle data recorded in the Women Health sheet.')
    checks = [
        check('Cycle regularity', ctx, ['cycle_length'], [lambda: cycle_pattern(ctx)], 'Cycle lengths fairly regular'),
        check('Weight / BMI', ctx, ['weight', 'bmi'], [lambda: t_up(ctx, 'weight'), lambda: t_up(ctx, 'bmi')], 'No sustained increase'),
        check('Glycemic markers', ctx, ['glucose', 'hba1c'],
              [lambda: lab_hi(ctx, 'glucose'), lambda: lab_hi(ctx, 'hba1c'), lambda: t_up(ctx, 'hba1c')],
              'Within listed range, no sustained rise'),
        check('Lipids (TG / HDL)', ctx, ['triglycerides', 'hdl'],
              [lambda: lab_hi(ctx, 'triglycerides'), lambda: lab_lo(ctx, 'hdl')], 'Within listed range'),
        check('Cycle-related symptoms', ctx, ['cycle_length'],
              [lambda: symptom(ctx, 'menstrual cramps', 'bloating', 'fatigue')], 'No recurring related symptom entries'),
    ]
    ctxt = context_items(ctx, ('pcos',), ('metformin',))
    return summarise("Women's health (PCOS)", 'pcos', checks, ctxt, anchor='Cycle regularity')


def run_scenarios(ctx: Ctx):
    return [metabolic(ctx), cardiovascular(ctx), pcos(ctx)]
