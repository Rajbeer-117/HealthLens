"""Run the HealthLens engine over every patient workbook and summarise the results.

Usage:
    python validate_scenarios.py            # all patients in patient_data.zip
    python validate_scenarios.py --limit 100

Writes:
    validation/validation_results.csv   one row per patient
    validation/validation_summary.md    scenario counts + agreement with recorded history

"Agreement" is a sanity check, not accuracy: for patients whose Medical History already records a
related condition (e.g. 'Hypertension'), we check how often the matching scenario showed related
signals. HealthLens never uses the recorded label to create its scenario status.
"""
from __future__ import annotations

import argparse
import io
import time
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

from healthlens import analyze_book
from healthlens.llm import guardrail_issues

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'validation'

# recorded history terms that relate to each scenario (used only for the sanity check)
RELATED = {
    'metabolic': ('prediabetes', 'type 2 diabetes', 'borderline glycemic'),
    'cardiovascular': ('hypertension', 'hyperlipidemia'),
    'pcos': ('pcos',),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    z = zipfile.ZipFile(ROOT / 'patient_data.zip')
    names = sorted(n for n in z.namelist() if n.lower().endswith('.xlsx') and not n.startswith('__MACOSX'))
    if args.limit:
        names = names[:args.limit]
    rows, errors, t0 = [], [], time.time()
    for i, n in enumerate(names, 1):
        pid = Path(n).stem
        try:
            a = analyze_book(pd.read_excel(io.BytesIO(z.read(n)), sheet_name=None, engine='openpyxl'))
        except Exception as exc:  # report, keep going
            errors.append((pid, repr(exc)))
            continue
        history = ' | '.join(h['finding'] for h in a['history']).lower()
        row = {'patient': pid, 'sex': a['patient']['sex'], 'history': history, **a['counts'],
               'guardrail_flags_in_evidence': sum(bool(guardrail_issues(e['llm_text'])) for e in a['evidence'])}
        for s in a['scenarios']:
            row[f'{s["key"]}_status'] = s['status']
            row[f'{s["key"]}_signals'] = s['signal_count']
        rows.append(row)
        if i % 100 == 0:
            print(f'  {i}/{len(names)} analysed ({time.time() - t0:.0f}s)')
    df = pd.DataFrame(rows)
    OUT.mkdir(exist_ok=True)
    df.to_csv(OUT / 'validation_results.csv', index=False)

    lines = ['# HealthLens Deliverable 2: scenario validation', '',
             f'Patients analysed: **{len(df)}** (errors: {len(errors)}) in {time.time() - t0:.0f} s', '',
             '## Scenario status counts', '', '| Scenario | Several related signals | Some related signals | No notable pattern | Not enough data | Not applicable |',
             '|---|---|---|---|---|---|']
    for key in ('metabolic', 'cardiovascular', 'pcos'):
        c = Counter(df[f'{key}_status'])
        lines.append(f'| {key} | {c["pattern"]} | {c["partial"]} | {c["none"]} | {c["insufficient"]} | {c["not_applicable"]} |')
    lines += ['', '## Sanity check against recorded medical history', '',
              'Share of patients whose history records a related condition and whose scenario showed '
              '"some" or "several" related signals, compared with patients without that history entry.', '',
              '| Scenario | History terms | With history: n / % with signals | Without history: n / % with signals |', '|---|---|---|---|']
    for key, terms in RELATED.items():
        has = df['history'].str.contains('|'.join(terms), na=False)
        applicable = df[f'{key}_status'] != 'not_applicable'
        sig = df[f'{key}_status'].isin(['pattern', 'partial'])
        w, wo = df[has & applicable], df[~has & applicable]
        pw = sig[w.index].mean() * 100 if len(w) else 0
        pwo = sig[wo.index].mean() * 100 if len(wo) else 0
        lines.append(f'| {key} | {", ".join(terms)} | {len(w)} / {pw:.0f}% | {len(wo)} / {pwo:.0f}% |')
    lines += ['', '## Engine output totals (mean per patient)', '']
    for col in ('evidence', 'sustained_trends', 'anomaly_episodes', 'lab_flags', 'relationships', 'co_occurrences', 'timeline_events'):
        lines.append(f'- {col.replace("_", " ")}: {df[col].mean():.1f}')
    lines += ['', f'Evidence statements that tripped the LLM guardrail patterns (should be 0): **{int(df["guardrail_flags_in_evidence"].sum())}**', '',
              '## Limitations', '',
              '- The data is synthetic; agreement with recorded history is a consistency check, not clinical accuracy.',
              '- Thresholds are documented demonstration heuristics, not clinical cut-offs.',
              '- No confidence scores are produced (per business guidance).']
    if errors:
        lines += ['', '## Errors', ''] + [f'- {p}: {e}' for p, e in errors[:20]]
    (OUT / 'validation_summary.md').write_text('\n'.join(lines), encoding='utf-8')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
