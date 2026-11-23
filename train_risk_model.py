"""Train the HealthLens risk model on the 900 patient workbooks from the business partner (patient_data.zip).

    python train_risk_model.py               # train, test, save healthlens/risk_model.json + report
    python train_risk_model.py --limit 200   # quicker trial on fewer patients

For each category (metabolic, cardiovascular, women's health):
  1. inputs  : measurements only (labs, vitals, wearable, cycles) + age/sex - see healthlens/risk_model.py
     answer  : the patient's recorded medical history names a related condition (never used as an input)
  2. split   : 80% of patients for training, 20% held out for testing (fixed seed, same share of positives)
  3. partial uploads: each training patient is also copied 3 times with whole sources hidden at random
     (e.g. only the lab report), so the model learns to work from a single document
  4. regularisation strength chosen by 5-fold cross-validation on the training patients
  5. test    : AUC on the held-out patients, with all data and with one source only, compared with the
     rule-based category signals
Writes healthlens/risk_model.json (used by the app) and validation/risk_model_report.md.
"""
from __future__ import annotations

import os
for _var in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(_var, '1')          # one maths thread per worker: avoids running out of memory on Windows
import argparse
import io
import json
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from healthlens import analyze_book
from healthlens import risk_model as RM

ROOT = Path(__file__).resolve().parent
SEED = 42
L2_GRID = (0.3, 1, 3, 10, 30, 100)


def load_one(args):
    name, raw = args
    book = pd.read_excel(io.BytesIO(raw), sheet_name=None)
    feats = RM.features(book)
    labels = {c: RM.label_of(book, c) for c in RM.CATEGORY_INPUTS}
    rules = {s['key']: s['signal_count'] for s in analyze_book(book)['scenarios']}
    return Path(name).stem, feats, labels, rules


def hide(row, sources):
    out = dict(row)
    for n in out:
        if RM.source_of_input(n) in sources:
            out[n] = np.nan
    return out


def augment(rows, y, sources, rng, copies=3):
    rows2, y2 = list(rows), list(y)
    for r, t in zip(rows, y):
        for _ in range(copies):
            keep = [s for s in sources if rng.random() < 0.5] or [sources[rng.integers(len(sources))]]
            rows2.append(hide(r, [s for s in sources if s not in keep])); y2.append(t)
    return rows2, np.array(y2, dtype=float)


def stratified_split(y, rng, test_share=0.2):
    idx_test = []
    for v in (0, 1):
        idx = np.flatnonzero(y == v)
        rng.shuffle(idx)
        idx_test += list(idx[:int(round(len(idx) * test_share))])
    test = np.zeros(len(y), bool); test[idx_test] = True
    return ~test, test


def train_category(cat, data, rng):
    rows = [(f, lab[cat], rules.get(cat, 0)) for _, f, lab, rules in data if lab[cat] is not None]
    feats = [r[0] for r in rows]; y = np.array([r[1] for r in rows]); rule = np.array([r[2] for r in rows])
    names = RM.inputs_for(cat)
    sources = sorted({RM.source_of_input(n) for n in names if RM.source_of_input(n)})
    tr, te = stratified_split(y, rng)
    tr_rows = [feats[i] for i in np.flatnonzero(tr)]; ytr = y[tr]
    # choose the regularisation strength by 5-fold cross-validation on the training patients
    folds = rng.permutation(len(tr_rows)) % 5
    cv = {}
    for l2 in L2_GRID:
        scores = []
        for k in range(5):
            fit_rows = [r for r, f in zip(tr_rows, folds) if f != k]; fit_y = ytr[folds != k]
            if len(set(fit_y)) < 2 or len(set(ytr[folds == k])) < 2: continue
            a_rows, a_y = augment(fit_rows, fit_y, sources, rng)
            X, mean, std, _ = RM.design(a_rows, names)
            b, w = RM.fit_logistic(X, a_y, l2)
            Xv, *_ = RM.design([r for r, f in zip(tr_rows, folds) if f == k], names, mean, std)
            scores.append(RM.auc(ytr[folds == k], b + Xv @ w))
        cv[l2] = float(np.mean(scores)) if scores else float('nan')
    best = max(cv, key=lambda k: (np.nan_to_num(cv[k]), -k))
    a_rows, a_y = augment(tr_rows, ytr, sources, rng)
    X, mean, std, flags = RM.design(a_rows, names)
    b, w = RM.fit_logistic(X, a_y, best)
    te_rows = [feats[i] for i in np.flatnonzero(te)]; yte = y[te]

    def test_auc(rows_):
        Xt, *_ = RM.design(rows_, names, mean, std)
        return RM.auc(yte, b + Xt @ w), 100 / (1 + np.exp(-(b + Xt @ w)))
    full_auc, s = test_auc(te_rows)
    by_source = {src: round(test_auc([hide(r, [x for x in sources if x != src]) for r in te_rows])[0], 3) for src in sources}
    pred_mod, pred_high = s >= RM.LEVEL_CUTOFFS[0], s >= RM.LEVEL_CUTOFFS[1]
    metrics = {
        'train_n': int(tr.sum()), 'test_n': int(te.sum()), 'positives_train': int(ytr.sum()), 'positives_test': int(yte.sum()),
        'l2': best, 'cv_auc': {str(k): round(v, 3) for k, v in cv.items()},
        'test_auc': round(full_auc, 3), 'test_auc_one_source': by_source,
        'rule_signals_test_auc': round(RM.auc(yte, rule[te]), 3),
        'moderate_or_high': {'sensitivity': round(float(pred_mod[yte == 1].mean()), 3) if yte.sum() else None,
                             'specificity': round(float((~pred_mod)[yte == 0].mean()), 3)},
        'high': {'sensitivity': round(float(pred_high[yte == 1].mean()), 3) if yte.sum() else None,
                 'specificity': round(float((~pred_high)[yte == 0].mean()), 3)},
    }
    return {'inputs': names, 'missing_flags': flags, 'mean': [round(float(v), 6) for v in mean],
            'std': [round(float(v), 6) for v in std], 'weights': [round(float(v), 6) for v in w],
            'bias': round(float(b), 6), 'metrics': metrics}


def report(model):
    lines = ['# HealthLens risk model: training and test report', '',
             f'Trained on the patient workbooks provided by the business partner (patient_data.zip, a mix of synthetic and real records; {model["patients"]} patients, {model["trained"]}). '
             'Inputs are measurements only; the answer is whether the recorded medical history names a related condition. '
             '80% of patients trained the model, 20% (never seen in training) tested it.', '',
             '| Category | Train / test patients | Positives (test) | Test AUC (all data) | Test AUC, one source only | '
             'Old rule signals, test AUC | Moderate+ sensitivity / specificity |', '|---|---|---|---|---|---|---|']
    for cat, m in model['categories'].items():
        mt = m['metrics']
        one = ', '.join(f'{k} {v:.2f}' for k, v in mt['test_auc_one_source'].items())
        mh = mt['moderate_or_high']
        lines.append(f'| {cat} | {mt["train_n"]} / {mt["test_n"]} | {mt["positives_test"]} | **{mt["test_auc"]:.2f}** | {one} | '
                     f'{mt["rule_signals_test_auc"]:.2f} | {mh["sensitivity"]} / {mh["specificity"]} |')
    lines += ['', 'AUC: 0.5 = no better than chance, 1.0 = perfect separation of patients with and without the condition.',
              f'Levels: score < {RM.LEVEL_CUTOFFS[0]} Low, {RM.LEVEL_CUTOFFS[0]}–{RM.LEVEL_CUTOFFS[1] - 1} Moderate, '
              f'≥ {RM.LEVEL_CUTOFFS[1]} High.', '',
              '## Limitations',
              '- Training data: the results show how well the model separates the provided patients, not clinical accuracy.',
              '- The answer key is the recorded history in the provided workbooks, not a confirmed diagnosis.',
              (f"- Women's health: only {model['categories']['pcos']['metrics']['positives_train'] + model['categories']['pcos']['metrics']['positives_test']} "
               f"patients have PCOS in their history ({model['categories']['pcos']['metrics']['positives_test']} in the test set), "
               "so its test result is not reliable and should not be presented as accurate."),
              '- Educational score, not a diagnosis; the app says so next to every score.']
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=None, help='parallel workers (default: 1 on Windows, up to 4 elsewhere)')
    args = ap.parse_args()
    t0 = time.time()
    with zipfile.ZipFile(ROOT / 'patient_data.zip') as z:
        items = [(n, z.read(n)) for n in sorted(z.namelist()) if n.lower().endswith('.xlsx')]
    if args.limit:
        items = items[:args.limit]
    workers = args.workers if args.workers is not None else (1 if os.name == 'nt' else min(4, os.cpu_count() or 1))
    print(f'Reading {len(items)} patient workbooks ({workers} worker{"s" if workers > 1 else ""})...', flush=True)
    data = []
    pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    try:
        for row in (pool.map(load_one, items, chunksize=8) if pool else map(load_one, items)):
            data.append(row)
            if len(data) % 100 == 0 or len(data) == len(items):
                print(f'  {len(data)} / {len(items)} read ({time.time() - t0:.0f} s)', flush=True)
    finally:
        if pool: pool.shutdown()
    print(f'Read {len(data)} patients in {time.time() - t0:.0f} s')
    rng = np.random.default_rng(SEED)
    model = {'patients': len(data), 'trained': date.today().isoformat(), 'seed': SEED,
             'trained_on': 'patient_data.zip (provided training data)', 'categories': {}}
    for cat in RM.CATEGORY_INPUTS:
        model['categories'][cat] = train_category(cat, data, rng)
        m = model['categories'][cat]['metrics']
        print(f'{cat:15} test AUC {m["test_auc"]:.3f} (old rules {m["rule_signals_test_auc"]:.3f}) one source {m["test_auc_one_source"]}')
    RM.MODEL_FILE.write_text(json.dumps(model, indent=1), encoding='utf-8')
    (ROOT / 'validation' / 'risk_model_report.md').write_text(report(model), encoding='utf-8')
    print(f'Saved {RM.MODEL_FILE.relative_to(ROOT)} and validation/risk_model_report.md ({time.time() - t0:.0f} s)')


if __name__ == '__main__':
    main()
