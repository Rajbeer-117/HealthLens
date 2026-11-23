"""Read health values out of one uploaded document.

Patients upload documents one at a time: a lab report PDF, a blood-pressure export from a home monitor, a step
count export, or a full HealthLens workbook. Every document is turned into rows in the project's standard sheets
(Lab Results, Vitals Daily, Wearable Daily, ...), so all of a patient's documents can be combined into one record
(healthlens.data.merge_workbooks) and analysed together.

Supported:
  * Excel workbooks (.xlsx/.xlsm): the HealthLens format, or any sheet whose columns look like lab results,
    vitals, wearable data, symptoms, medications or cycles (sheet names do not matter).
  * CSV exports (.csv): the same table types.
  * Lab / clinic reports as text: PDF (with selectable text), Word (.docx) and .txt. Test names are matched with
    common variants (e.g. "LDL-C", "Hemoglobin A1c"), and SI units used in Canada (mmol/L, umol/L...) are converted.
  * Photos and scans (.png/.jpg): read only if the optional OCR tool Tesseract is installed; otherwise stored only.
Nothing here diagnoses anything: it only copies values that are written in the document.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

# Columns of the standard sheets (as in the HealthLens workbook format).
SHEET_COLUMNS = {
    'Patient Profile': ['Field', 'Value'],
    'Medical History': ['Date', 'Encounter / Type', 'Finding / Event', 'Status', 'Notes'],
    'Medication History': ['Medication ID', 'Medication', 'Dose', 'Frequency', 'Route', 'Start Date', 'End Date',
                           'Status', 'Indication', 'Tolerance / Adverse Effect', 'Change Rationale'],
    'Lab Results': ['Date', 'Test Name', 'Result', 'Unit', 'Reference Range'],
    'Vitals Daily': ['Date', 'Systolic BP', 'Diastolic BP', 'Resting HR', 'Weight kg', 'BMI', 'SpO2 %', 'Temp C',
                     'Respiratory Rate'],
    'Wearable Daily': ['Date', 'Steps', 'Sleep Hours', 'Active Minutes', 'Resting HR', 'HRV ms', 'Calories kcal',
                       'Sedentary Hours', 'Stress Score 1-10'],
    'Habits Weekly': ['Week', 'Week Start', 'Water L/day', 'Fruit/Veg servings/day', 'Exercise sessions/week',
                      'Caffeine cups/day', 'Alcohol drinks/week', 'Smoking', 'Late meals/week', 'Self-reported stress'],
    'Symptoms Timeline': ['Date', 'Symptom', 'Severity', 'Duration', 'Source'],
    'Encounters & Notes': ['Date', 'Encounter', 'Clinical Note'],
    'Medication Adherence': ['Month', 'Medication ID', 'Estimated Adherence %', 'Approx Missed Doses', 'Source'],
    'Women Health': ['Cycle', 'Start Date', 'Cycle Length', 'Bleeding Days', 'Symptoms', 'Severity'],
}
READING_SHEETS = ('Lab Results', 'Vitals Daily', 'Wearable Daily', 'Women Health', 'Habits Weekly', 'Symptoms Timeline')

# ---------------------------------------------------------------- lab tests: names, units, conversions
# canonical name, dataset unit, name variants (longest are tried first, so "hemoglobin a1c" wins over "hemoglobin")
LAB_TESTS = [
    ('HbA1c', '%', ['hba1c', 'hb a1c', 'a1c', 'hemoglobin a1c', 'haemoglobin a1c', 'glycated hemoglobin',
                    'glycated haemoglobin', 'glycosylated hemoglobin',
                    # common OCR misreadings of "A1c" in photos and scans
                    'hemoglobin alc', 'haemoglobin alc', 'hemoglobin aic', 'hbalc', 'hb alc', 'hbaic']),
    ('Fasting glucose', 'mg/dL', ['fasting glucose', 'fasting blood glucose', 'fasting plasma glucose', 'glucose fasting',
                                  'glucose, fasting', 'fbg', 'fpg', 'glucose']),
    ('LDL cholesterol', 'mg/dL', ['ldl cholesterol', 'ldl-cholesterol', 'ldl-c', 'ldl c', 'ldl']),
    ('HDL cholesterol', 'mg/dL', ['hdl cholesterol', 'hdl-cholesterol', 'hdl-c', 'hdl c', 'hdl']),
    ('Total cholesterol', 'mg/dL', ['total cholesterol', 'cholesterol, total', 'cholesterol total', 'cholesterol']),
    ('Triglycerides', 'mg/dL', ['triglycerides', 'triglyceride', 'trig']),
    ('Hemoglobin', 'g/dL', ['hemoglobin', 'haemoglobin', 'hgb', 'hb']),
    ('eGFR', 'mL/min/1.73m²', ['egfr', 'estimated gfr', 'gfr']),
    ('Creatinine', 'mg/dL', ['creatinine', 'creat']),
    ('ALT', 'U/L', ['alanine aminotransferase', 'alt (sgpt)', 'sgpt', 'alt']),
    ('AST', 'U/L', ['aspartate aminotransferase', 'ast (sgot)', 'sgot', 'ast']),
    ('Platelets', 'x10^9/L', ['platelet count', 'platelets', 'plt']),
    ('WBC', 'x10^9/L', ['white blood cell count', 'white blood cells', 'white cell count', 'leukocytes', 'wbc']),
    ('Ferritin', 'ng/mL', ['ferritin']),
    ('Vitamin D', 'ng/mL', ['25-hydroxy vitamin d', '25-oh vitamin d', '25(oh)d', 'vitamin d, 25-hydroxy', 'vitamin d']),
    ('TSH', 'mIU/L', ['thyroid stimulating hormone', 'tsh']),
    ('Vitamin B12', 'pg/mL', ['vitamin b12', 'vitamin b-12', 'cobalamin', 'b12']),
    ('Potassium', 'mmol/L', ['potassium', 'k+']),
    ('Sodium', 'mmol/L', ['sodium', 'na+']),
    ('Free T4', 'ng/dL', ['free t4', 'free thyroxine', 'ft4']),
    ('Urine albumin/creatinine', 'mg/g', ['urine albumin/creatinine', 'albumin/creatinine ratio', 'albumin creatinine ratio',
                                         'uacr', 'acr']),
]
UNIT_OF = {name: unit for name, unit, _ in LAB_TESTS}
ALIASES = sorted(((alias, name) for name, _, variants in LAB_TESTS for alias in variants), key=lambda a: -len(a[0]))

# (test, unit written in the report) -> function converting to the dataset unit
_CHOL, _TG, _GLU = 38.67, 88.57, 18.016
CONVERSIONS = {
    ('Fasting glucose', 'mmol/l'): lambda v: v * _GLU,
    ('LDL cholesterol', 'mmol/l'): lambda v: v * _CHOL,
    ('HDL cholesterol', 'mmol/l'): lambda v: v * _CHOL,
    ('Total cholesterol', 'mmol/l'): lambda v: v * _CHOL,
    ('Triglycerides', 'mmol/l'): lambda v: v * _TG,
    ('HbA1c', 'mmol/mol'): lambda v: v / 10.929 + 2.15,
    ('HbA1c', 'fraction'): lambda v: v * 100,
    ('Creatinine', 'umol/l'): lambda v: v / 88.42,
    ('Hemoglobin', 'g/l'): lambda v: v / 10,
    ('Vitamin D', 'nmol/l'): lambda v: v / 2.496,
    ('Vitamin B12', 'pmol/l'): lambda v: v * 1.355,
    ('Free T4', 'pmol/l'): lambda v: v / 12.87,
    ('Urine albumin/creatinine', 'mg/mmol'): lambda v: v * 8.84,
}
# Units a test can be written in (normalised, see _unit_key; '' = no unit written). A value with any other unit
# belongs to a different test (e.g. a % next to "Hemoglobin" is really HbA1c misread) and is not recorded.
UNITS_OK = {
    'HbA1c': {'%', 'mmol/mol', 'fraction', ''}, 'Fasting glucose': {'mg/dl', 'mmol/l', ''},
    'LDL cholesterol': {'mg/dl', 'mmol/l', ''}, 'HDL cholesterol': {'mg/dl', 'mmol/l', ''},
    'Total cholesterol': {'mg/dl', 'mmol/l', ''}, 'Triglycerides': {'mg/dl', 'mmol/l', ''},
    'Hemoglobin': {'g/dl', 'g/l', ''}, 'eGFR': {'ml/min/1.73m2', ''}, 'Creatinine': {'mg/dl', 'umol/l', ''},
    'ALT': {'u/l', ''}, 'AST': {'u/l', ''}, 'Platelets': {'x10^9/l', ''}, 'WBC': {'x10^9/l', ''},
    'Ferritin': {'ng/ml', ''}, 'Vitamin D': {'ng/ml', 'nmol/l', ''}, 'TSH': {'miu/l', ''},
    'Vitamin B12': {'pg/ml', 'pmol/l', ''}, 'Potassium': {'mmol/l', ''}, 'Sodium': {'mmol/l', ''},
    'Free T4': {'ng/dl', 'pmol/l', ''}, 'Urine albumin/creatinine': {'mg/g', 'mg/mmol', ''},
}
# Values outside these limits (in the dataset unit) cannot be real results: a misread number is skipped, not recorded.
PLAUSIBLE = {
    'HbA1c': (3, 20), 'Fasting glucose': (30, 700), 'LDL cholesterol': (10, 500), 'HDL cholesterol': (5, 200),
    'Total cholesterol': (50, 700), 'Triglycerides': (10, 3000), 'Hemoglobin': (3, 25), 'eGFR': (1, 200),
    'Creatinine': (0.1, 20), 'ALT': (1, 5000), 'AST': (1, 5000), 'Platelets': (5, 2000), 'WBC': (0.1, 200),
    'Ferritin': (1, 10000), 'Vitamin D': (2, 200), 'TSH': (0.001, 150), 'Vitamin B12': (50, 5000),
    'Potassium': (1.5, 10), 'Sodium': (100, 180), 'Free T4': (0.1, 10), 'Urine albumin/creatinine': (0, 10000),
}


def plausible(test: str, value: float, unit: str) -> bool:
    """The unit fits the test and the value (already in the dataset unit) is physically possible."""
    if test not in UNITS_OK:
        return True
    key = _unit_key(unit)
    if key not in UNITS_OK[test] and _unit_key(UNIT_OF[test]) != key:
        return False
    lo, hi = PLAUSIBLE[test]
    return lo <= value <= hi


DECIMALS = {'HbA1c': 1, 'Creatinine': 2, 'Hemoglobin': 1, 'Free T4': 2, 'TSH': 2, 'WBC': 1, 'Potassium': 1}

_UNIT_WORDS = r'(mmol/mol|mmol/l|mg/dl|umol/l|µmol/l|μmol/l|g/dl|g/l|nmol/l|pmol/l|ng/ml|pg/ml|ug/l|µg/l|u/l|iu/l|' \
              r'miu/l|uiu/ml|µiu/ml|mg/g|mg/mmol|ml/min/1\.73\s?m(?:²|2|\^2)?|ml/min|x\s?10\^?9/l|10\^9/l|10\*9/l|' \
              r'x10e9/l|k/ul|10\^3/ul|thou/ul|%|meq/l)'


def _unit_key(unit: str) -> str:
    u = (unit or '').strip().lower().replace('µ', 'u').replace('μ', 'u').replace(' ', '').replace('²', '2').replace('^2', '2')
    if u.startswith('ml/min'):
        return 'ml/min/1.73m2'
    return {'mg/dl': 'mg/dl', 'iu/l': 'u/l', 'uiu/ml': 'miu/l', 'meq/l': 'mmol/l', 'ug/l': 'ng/ml',
            'k/ul': 'x10^9/l', '10^3/ul': 'x10^9/l', 'thou/ul': 'x10^9/l', '10^9/l': 'x10^9/l', '10*9/l': 'x10^9/l',
            'x10e9/l': 'x10^9/l'}.get(u, u)


def canonical_test(name) -> str | None:
    """'LDL-C', 'Hemoglobin A1c', 'Cholesterol, Total' -> the dataset's test name (None if not a known test)."""
    text = ' ' + re.sub(r'\s+', ' ', str(name).lower()) + ' '
    for alias, canon in ALIASES:
        if re.search(r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])', text):
            return canon
    return None


def convert(test: str, value: float, unit: str) -> tuple[float, str, bool]:
    """Value in the dataset unit. Returns (value, unit, converted?)."""
    key = _unit_key(unit)
    if test == 'HbA1c' and not unit and value < 1:          # 0.062 written as a fraction
        key = 'fraction'
    fn = CONVERSIONS.get((test, key))
    target = UNIT_OF.get(test, unit)
    if fn:
        dec = DECIMALS.get(test, 0)
        return (round(fn(value), dec) if dec else round(fn(value))), target, True
    if not unit or _unit_key(unit) == _unit_key(target):
        return value, target, False
    return value, unit, False


def convert_range(test: str, text: str, unit: str) -> str:
    """'3.5-5.0' in mmol/L -> the same range in the dataset unit, written like the dataset ('<100', '70–99')."""
    if text is None or (not isinstance(text, str) and pd.isna(text)):
        return ''
    t = str(text).strip()
    fn = CONVERSIONS.get((test, _unit_key(unit)))
    nums = re.findall(r'\d+(?:\.\d+)?', t)
    if not nums:
        return t
    dec = DECIMALS.get(test, 0)
    fmt = (lambda v: f'{fn(float(v)):.{dec}f}') if fn else (lambda v: v)
    if re.search(r'\d\s*(?:-|–|to)\s*\d', t) and len(nums) >= 2:
        return f'{fmt(nums[0])}–{fmt(nums[1])}'
    if re.search(r'[<≤]', t):
        return f'<{fmt(nums[0])}'
    if re.search(r'[>≥]', t):
        return f'>{fmt(nums[0])}'
    return t


# ---------------------------------------------------------------- result of reading one document
@dataclass
class Extraction:
    sheets: dict = field(default_factory=dict)        # sheet name -> DataFrame (standard columns)
    kind: str = 'other'                                # workbook | table | report | image | other
    note: str = ''                                     # what was found, shown on the Documents page
    converted: int = 0                                 # values converted from other units

    @property
    def readings(self) -> int:
        return int(sum(len(df) for name, df in self.sheets.items() if name in READING_SHEETS and df is not None))

    @property
    def patient_id(self) -> str:
        prof = self.sheets.get('Patient Profile')
        if prof is None:
            return ''
        for f, v in zip(prof['Field'], prof['Value']):
            if str(f).strip().casefold() == 'patient id' and not pd.isna(v) and str(v).strip():
                return str(v).strip()
        return ''


def _norm(text) -> str:
    return re.sub(r'[^a-z0-9%]+', ' ', str(text).lower()).strip()


def _find(columns, *names, exclude=()):
    """The first column whose normalised header equals or starts with one of the names."""
    cols = {c: _norm(c) for c in columns}
    for name in names:
        n = _norm(name)
        for c, h in cols.items():
            if (h == n or h.startswith(n + ' ') or h.endswith(' ' + n)) and not any(_norm(x) in h for x in exclude):
                return c
    return None


def _dates(series):
    from .data import to_datetime                        # Excel serial numbers and text dates
    return to_datetime(series)


# ---------------------------------------------------------------- tables (Excel sheets and CSV files)
def _lab_table(df) -> pd.DataFrame | None:
    test = _find(df.columns, 'test name', 'test', 'analyte', 'component', 'lab test', 'investigation', 'parameter', 'name')
    value = _find(df.columns, 'result', 'value', 'result value', 'observed value', 'reading')
    date_c = _find(df.columns, 'date', 'collection date', 'collected', 'date collected', 'test date', 'specimen date',
                   'result date', 'date time')
    if not (test and value and date_c):
        return None
    unit = _find(df.columns, 'unit', 'units', 'uom')
    ref = _find(df.columns, 'reference range', 'reference', 'ref range', 'normal range', 'range', 'reference interval')
    rows, converted = [], 0
    for _, r in df.iterrows():
        canon = canonical_test(r[test]) or (str(r[test]).strip() if not pd.isna(r[test]) else None)
        raw = pd.to_numeric(str(r[value]).replace(',', '').strip().lstrip('<>≤≥ '), errors='coerce') if not pd.isna(r[value]) else np.nan
        if canon is None or pd.isna(raw):
            continue
        u = '' if unit is None or pd.isna(r[unit]) else str(r[unit]).strip()
        v, u2, did = convert(canon, float(raw), u) if canon in UNIT_OF else (float(raw), u, False)
        if canon in UNIT_OF and not plausible(canon, v, u):
            continue
        converted += did
        rr = '' if ref is None else (convert_range(canon, r[ref], u) if canon in UNIT_OF else ('' if pd.isna(r[ref]) else str(r[ref])))
        rows.append({'Date': r[date_c], 'Test Name': canon, 'Result': v, 'Unit': u2, 'Reference Range': rr})
    out = pd.DataFrame(rows, columns=SHEET_COLUMNS['Lab Results'])
    if len(out):
        out['Date'] = _dates(out['Date'])
        out = out[out['Date'].notna()]
    out.attrs['converted'] = converted
    return out


_VITALS = {'Systolic BP': ('systolic', 'sys', 'sbp', 'systolic bp', 'systolic mmhg'),
           'Diastolic BP': ('diastolic', 'dia', 'dbp', 'diastolic bp', 'diastolic mmhg'),
           'Resting HR': ('resting hr', 'pulse', 'heart rate', 'hr', 'resting heart rate', 'pulse bpm'),
           'Weight kg': ('weight kg', 'weight', 'body weight'),
           'BMI': ('bmi', 'body mass index'),
           'SpO2 %': ('spo2 %', 'spo2', 'oxygen saturation', 'o2 sat'),
           'Temp C': ('temp c', 'temperature', 'temp', 'body temperature'),
           'Respiratory Rate': ('respiratory rate', 'resp rate', 'breaths per minute')}
_WEARABLE = {'Steps': ('steps', 'step count', 'daily steps'),
             'Sleep Hours': ('sleep hours', 'sleep', 'hours asleep', 'sleep duration', 'time asleep'),
             'Active Minutes': ('active minutes', 'activity minutes', 'exercise minutes', 'active min', 'move minutes'),
             'Resting HR': ('resting hr', 'resting heart rate', 'resting heart rate bpm'),
             'HRV ms': ('hrv ms', 'hrv', 'heart rate variability'),
             'Calories kcal': ('calories kcal', 'calories', 'active calories', 'energy burned'),
             'Sedentary Hours': ('sedentary hours', 'sedentary'),
             'Stress Score 1-10': ('stress score 1 10', 'stress score', 'stress')}


def _measure_table(df, mapping, sheet) -> pd.DataFrame | None:
    date_c = _find(df.columns, 'date', 'day', 'measurement date', 'date time', 'datetime', 'time')
    if not date_c:
        return None
    out = pd.DataFrame({'Date': _dates(df[date_c])})
    found = 0
    for target, names in mapping.items():
        col = _find([c for c in df.columns if c != date_c], *names)
        if col is not None:
            vals = pd.to_numeric(df[col].astype(str).str.replace(',', '').str.extract(r'(-?\d+(?:\.\d+)?)')[0], errors='coerce')
            if target == 'Weight kg' and re.search(r'\blbs?\b', str(col).lower()):
                vals = (vals / 2.2046).round(1)
            if target == 'Temp C' and re.search(r'\b(f|fahrenheit)\b', str(col).lower()):
                vals = ((vals - 32) / 1.8).round(1)
            if target == 'Sleep Hours' and re.search(r'\bmin', str(col).lower()):
                vals = (vals / 60).round(2)
            out[target] = vals
            found += 1
    if sheet == 'Vitals Daily' and 'Systolic BP' not in out:     # "Blood pressure: 128/82" in one column
        bp = _find(df.columns, 'blood pressure', 'bp')
        if bp is not None:
            parts = df[bp].astype(str).str.extract(r'(\d{2,3})\s*/\s*(\d{2,3})')
            out['Systolic BP'] = pd.to_numeric(parts[0], errors='coerce')
            out['Diastolic BP'] = pd.to_numeric(parts[1], errors='coerce')
            found += 2
    if not found:
        return None
    out = out[out['Date'].notna()]
    values = [c for c in out.columns if c != 'Date']
    out = out[out[values].notna().any(axis=1)]
    if len(out) and out['Date'].duplicated().any():              # several readings a day -> the day's average
        out = out.groupby(out['Date'].dt.normalize(), as_index=False)[values].mean().round(1)
    return out.reindex(columns=[c for c in SHEET_COLUMNS[sheet] if c in out.columns or c == 'Date'])


def _symptoms_table(df):
    d, s = _find(df.columns, 'date'), _find(df.columns, 'symptom', 'symptoms')
    if not (d and s):
        return None
    out = pd.DataFrame({'Date': _dates(df[d]), 'Symptom': df[s]})
    for col, names in (('Severity', ('severity',)), ('Duration', ('duration',)), ('Source', ('source',))):
        c = _find(df.columns, *names)
        out[col] = df[c] if c else None
    return out[out['Date'].notna() & out['Symptom'].notna()]


def _cycles_table(df):
    start = _find(df.columns, 'start date', 'period start', 'cycle start')
    length = _find(df.columns, 'cycle length', 'length')
    if not (start and length):
        return None
    out = pd.DataFrame({'Cycle': range(1, len(df) + 1), 'Start Date': _dates(df[start]),
                        'Cycle Length': pd.to_numeric(df[length], errors='coerce')})
    for col, names in (('Bleeding Days', ('bleeding days', 'period length', 'period days')), ('Symptoms', ('symptoms',)),
                       ('Severity', ('severity',))):
        c = _find(df.columns, *names)
        out[col] = df[c] if c else None
    return out[out['Start Date'].notna()]


def _medications_table(df):
    med = _find(df.columns, 'medication', 'drug', 'medicine')
    if not med or _find(df.columns, 'estimated adherence %', 'adherence'):
        return None
    out = pd.DataFrame({'Medication ID': [f'MED-{i + 1:02d}' for i in range(len(df))], 'Medication': df[med]})
    for col in SHEET_COLUMNS['Medication History'][2:]:
        c = _find(df.columns, col)
        out[col] = _dates(df[c]) if c and 'Date' in col else (df[c] if c else None)
    return out[out['Medication'].notna()]


def table_to_sheet(df: pd.DataFrame, hint: str = '') -> tuple[str, pd.DataFrame] | None:
    """Recognise what a table holds from its columns and return it in the standard format."""
    df = df.dropna(how='all').dropna(axis=1, how='all')
    if df.empty:
        return None
    df.columns = [str(c).strip() for c in df.columns]
    lab = _lab_table(df)
    if lab is not None and len(lab):
        return 'Lab Results', lab
    headers = ' '.join(_norm(c) for c in df.columns)
    if re.search(r'\bsteps?\b|\bsleep\b|\bhrv\b|active minutes|calories', headers):
        t = _measure_table(df, _WEARABLE, 'Wearable Daily')
        if t is not None and len(t):
            return 'Wearable Daily', t
    if re.search(r'systolic|diastolic|blood pressure|\bbp\b|pulse|heart rate|weight|\bbmi\b|spo2|temp', headers):
        t = _measure_table(df, _VITALS, 'Vitals Daily')
        if t is not None and len(t):
            return 'Vitals Daily', t
    for builder, sheet in ((_cycles_table, 'Women Health'), (_symptoms_table, 'Symptoms Timeline'),
                           (_medications_table, 'Medication History')):
        t = builder(df)
        if t is not None and len(t):
            return sheet, t
    return None


def _standard_sheet_name(name: str) -> str | None:
    n = _norm(name)
    for sheet in SHEET_COLUMNS:
        if _norm(sheet) == n:
            return sheet
    aliases = {'labs': 'Lab Results', 'lab': 'Lab Results', 'lab results': 'Lab Results', 'bloodwork': 'Lab Results',
               'vitals': 'Vitals Daily', 'wearable': 'Wearable Daily', 'profile': 'Patient Profile',
               'patient': 'Patient Profile', 'medications': 'Medication History', 'symptoms': 'Symptoms Timeline',
               'cycles': 'Women Health', 'womens health': 'Women Health', 'habits': 'Habits Weekly'}
    return aliases.get(n)


def _profile_from_table(df) -> pd.DataFrame | None:
    if {'Field', 'Value'} <= set(map(str, df.columns)):
        return df[['Field', 'Value']]
    return None


def read_workbook(raw: bytes) -> Extraction:
    book = pd.read_excel(io.BytesIO(raw), sheet_name=None, engine='openpyxl')
    ex = Extraction(kind='workbook')
    for name, df in book.items():
        std = _standard_sheet_name(name)
        if std == 'Patient Profile':
            prof = _profile_from_table(df)
            if prof is not None:
                ex.sheets['Patient Profile'] = prof
            continue
        if std and set(SHEET_COLUMNS[std]) <= set(map(str, df.columns)):
            ex.sheets[std] = df                           # already in the standard format: kept exactly as written
            continue
        found = table_to_sheet(df, name)
        if found:
            sheet, table = found
            ex.converted += table.attrs.get('converted', 0)
            ex.sheets[sheet] = pd.concat([ex.sheets[sheet], table], ignore_index=True) if sheet in ex.sheets else table
    return ex


def read_csv(raw: bytes) -> Extraction:
    text = None
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        df = pd.read_csv(io.StringIO(text or ''), sep=None, engine='python')
    except Exception:
        return Extraction(kind='table', note='The CSV file could not be read as a table.')
    ex = Extraction(kind='table')
    named = next((c for c in df.columns if _norm(c) in ('patient name', 'name', 'full name')), None)
    names = {clean_name(v) for v in df[named].dropna()} - {''} if named is not None else set()
    if named is not None:
        df = df.drop(columns=[named])
    who = _find(df.columns, 'patient id', 'patient', 'user id')      # exports that name the patient
    if who is not None:
        ids = {str(v).strip() for v in df[who].dropna() if str(v).strip()}
        if len(ids) == 1:
            ex.sheets['Patient Profile'] = pd.DataFrame([('Patient ID', ids.pop())], columns=['Field', 'Value'])
        df = df.drop(columns=[who])
    if len(names) == 1:
        row = pd.DataFrame([('Name', names.pop())], columns=['Field', 'Value'])
        prof = ex.sheets.get('Patient Profile')
        ex.sheets['Patient Profile'] = row if prof is None else pd.concat([prof, row], ignore_index=True)
    found = table_to_sheet(df)
    if found:
        ex.sheets[found[0]] = found[1]
        ex.converted = found[1].attrs.get('converted', 0)
    return ex


# ---------------------------------------------------------------- lab and clinic reports (text)
_MONTHS = 'jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec'
_DATE = (r'(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{4}|'
         rf'(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}\s+(?:{_MONTHS})[a-z]*\.?\s+\d{{4}})')
_REPORT_DATE = re.compile(r'(?:collected|collection date|date collected|specimen collected|sample date|date of collection|'
                          r'test date|date of service|visit date|report date|date reported|reported|date)\s*(?:on)?\s*[:\-]?\s*'
                          + _DATE, re.I)
_NUM = r'(\d+(?:\.\d+)?)'


def parse_date(text: str):
    t = text.strip().replace('.', '')
    try:
        if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{2,4}', t):
            a, b, y = (int(x) for x in t.split('/'))
            y = y + 2000 if y < 100 else y
            month, day = (a, b) if a <= 12 else (b, a)        # North-American month/day unless that is impossible
            return pd.Timestamp(datetime(y, month, day))
        return pd.Timestamp(pd.to_datetime(t, dayfirst=bool(re.match(r'\d{1,2}\s+[a-z]', t, re.I))))
    except (ValueError, TypeError):
        return None


def _report_date(text: str):
    for m in _REPORT_DATE.finditer(text):
        d = parse_date(m.group(1))
        if d is not None and d.year > 1990 and d <= pd.Timestamp.now() + pd.Timedelta(days=1):
            return d
    for m in re.finditer(_DATE, text, re.I):                     # any date in the document
        d = parse_date(m.group(1))
        if d is not None and 2000 < d.year and d <= pd.Timestamp.now() + pd.Timedelta(days=1):
            return d
    return None


_NAME_STOP = r'(?=\s{2,}|\s*\b(?:patient\s*(?:id|identifier|no|number)|sex|gender|dob|date\s+of\s+birth|birth\s+date|age|mrn)\b|\s*$)'


def clean_name(value) -> str:
    """A patient name as printed, tidied: '(synthetic)'-style notes, titles and extra spaces removed."""
    v = re.sub(r'\(.*?\)', ' ', str(value or ''))
    v = re.sub(r'\b(?:mr|mrs|ms|miss|dr)\.?\s+', ' ', v, flags=re.I)
    v = ' '.join(v.replace('\u00a0', ' ').split()).strip(' ,.;:-')
    return v if re.fullmatch(r"[A-Za-z][A-Za-z .,'-]{1,60}", v) and len(re.findall(r'[A-Za-z]', v)) >= 2 else ''


def _profile_from_text(text: str) -> pd.DataFrame | None:
    fields = []
    m = re.search(r'(?:patient\s*name|^[ \t]*(?:full\s+)?name)\s*:\s*(.+?)' + _NAME_STOP, text, re.I | re.M)
    name = clean_name(m.group(1)) if m else ''
    if name:
        fields.append(('Name', name))
    m = re.search(r'patient\s*(?:id|identifier|no\.?|number|#)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{1,30})', text, re.I)
    if m:
        fields.append(('Patient ID', m.group(1)))
    m = re.search(r'\b(?:sex|gender)\s*[:\-]?\s*(female|male|f|m)\b', text, re.I)
    if m:
        fields.append(('Sex', {'f': 'Female', 'm': 'Male'}.get(m.group(1).lower(), m.group(1).title())))
    m = re.search(r'\b(?:dob|date of birth|birth date)\s*[:\-]?\s*' + _DATE, text, re.I)
    dob = parse_date(m.group(1)) if m else None
    if dob is not None and not (1900 < dob.year and dob <= pd.Timestamp.now()):
        dob = None
    if dob is not None:              # kept as a fact: it is compared with the patient's confirmed date of birth
        fields.append(('Date of Birth', dob.strftime('%Y-%m-%d')))
    m = re.search(r'\bage\s*[:\-]?\s*(\d{1,3})\b', text, re.I)
    if m and 0 < int(m.group(1)) < 120:
        fields.append(('Age', int(m.group(1))))
    elif dob is not None:
        today = date.today()
        fields.append(('Age', today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))))
    return pd.DataFrame(fields, columns=['Field', 'Value']) if fields else None


def _labs_from_text(text: str, when) -> tuple[pd.DataFrame, int]:
    rows, converted, seen = [], 0, set()
    for line in text.splitlines():
        low = ' ' + re.sub(r'\s+', ' ', line.lower()) + ' '
        for alias, canon in ALIASES:
            m = re.search(r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])', low)
            if not m:
                continue
            rest = low[m.end():]
            rest = re.sub(r'^\s*(\([^)]*\)|,?\s*(?:serum|plasma|blood|calc(?:ulated)?|direct|total|fasting|random))\s*', ' ', rest)
            v = re.search(r'(?<![\d.])[<>≤≥]?\s*' + _NUM + r'(?![\d/])', rest)
            if not v:
                break
            after = rest[v.end():]
            u = re.match(r'\s*(?:[hl]\b|\*|high|low)?\s*' + _UNIT_WORDS, after, re.I)
            unit = u.group(1) if u else ''
            if not unit and canon == 'HbA1c' and '%' in rest[:v.end() + 3]:
                unit = '%'
            ref = re.search(r'(\d+(?:\.\d+)?\s*(?:-|–|to)\s*\d+(?:\.\d+)?|[<>≤≥]\s*=?\s*\d+(?:\.\d+)?)', after[(u.end() if u else 0):])
            value, unit2, did = convert(canon, float(v.group(1)), unit)
            if not plausible(canon, value, unit):
                break                                        # wrong unit for this test, or a misread number
            key = (canon, value)
            if canon not in seen and key not in seen:
                rows.append({'Date': when, 'Test Name': canon, 'Result': value, 'Unit': unit2,
                             'Reference Range': convert_range(canon, ref.group(1), unit) if ref else ''})
                seen.add(canon)
                converted += did
            break
    return pd.DataFrame(rows, columns=SHEET_COLUMNS['Lab Results']), converted


def _vitals_from_text(text: str, when) -> pd.DataFrame | None:
    low = text.lower()
    row = {'Date': when}
    m = re.search(r'(?:blood pressure|\bbp\b)\D{0,20}(\d{2,3})\s*/\s*(\d{2,3})', low)
    if m:
        row['Systolic BP'], row['Diastolic BP'] = float(m.group(1)), float(m.group(2))
    for col, pat in (('Resting HR', r'(?:resting heart rate|heart rate|pulse)\D{0,12}(\d{2,3})\b'),
                     ('BMI', r'\bbmi\D{0,10}(\d{2}(?:\.\d)?)'),
                     ('SpO2 %', r'(?:spo2|oxygen saturation)\D{0,12}(\d{2,3})\s*%'),
                     ('Respiratory Rate', r'respiratory rate\D{0,12}(\d{1,2})\b')):
        m = re.search(pat, low)
        if m:
            row[col] = float(m.group(1))
    m = re.search(r'\bweight\D{0,12}(\d{2,3}(?:\.\d)?)\s*(kg|lbs?|pounds)\b', low)
    if m:
        w = float(m.group(1))
        row['Weight kg'] = round(w / 2.2046, 1) if m.group(2).startswith(('lb', 'pound')) else w
    m = re.search(r'\btemp(?:erature)?\D{0,12}(\d{2,3}(?:\.\d)?)\s*°?\s*([cf])\b', low)
    if m:
        t = float(m.group(1))
        row['Temp C'] = round((t - 32) / 1.8, 1) if m.group(2) == 'f' else t
    if len(row) == 1:
        return None
    return pd.DataFrame([row]).reindex(columns=[c for c in SHEET_COLUMNS['Vitals Daily'] if c in row])


def read_report_text(text: str, kind: str = 'report') -> Extraction:
    ex = Extraction(kind=kind)
    when = _report_date(text)
    prof = _profile_from_text(text)
    if prof is not None:
        ex.sheets['Patient Profile'] = prof
    if when is None:
        probe = pd.Timestamp('2000-01-01')                  # are there values at all, or just no date?
        has_values = len(_labs_from_text(text, probe)[0]) or _vitals_from_text(text, probe) is not None
        ex.note = 'No test date found; stored only.' if has_values else 'No health values found; stored only.'
        return ex
    labs, ex.converted = _labs_from_text(text, when)
    if len(labs):
        ex.sheets['Lab Results'] = labs
    vitals = _vitals_from_text(text, when)
    if vitals is not None:
        ex.sheets['Vitals Daily'] = vitals
    return ex


def pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    pages = []
    for page in reader.pages[:30]:
        try:
            pages.append(page.extract_text(extraction_mode='layout') or '')
        except TypeError:                                  # older pypdf without layout mode
            pages.append(page.extract_text() or '')
    return '\n'.join(pages)


def docx_text(raw: bytes) -> str:
    """Text of a Word document, paragraph by paragraph and table row by table row (no extra library needed)."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        xml = z.read('word/document.xml').decode('utf-8', 'ignore')
    # A table row is one line (its cells separated by spaces), so a test and its result stay together.
    xml = re.sub(r'<w:tr[ >].*?</w:tr>', lambda m: re.sub(r'</w:p>', ' ', m.group(0)).replace('</w:tc>', '   ') + '\n', xml, flags=re.S)
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'<w:tab/>', '  ', xml)
    return re.sub(r'<[^>]+>', '', xml).replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')


def ocr_available() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def image_text(raw: bytes) -> str | None:
    if not ocr_available():
        return None
    import pytesseract
    from PIL import Image
    return pytesseract.image_to_string(Image.open(io.BytesIO(raw)))


# ---------------------------------------------------------------- entry point
def extract(filename: str, raw: bytes) -> Extraction:
    """Read one uploaded document. Never raises for unreadable content: the note says what happened."""
    ext = ('.' + filename.rsplit('.', 1)[-1].lower()) if '.' in filename else ''
    try:
        if ext in ('.xlsx', '.xlsm'):
            ex = read_workbook(raw)
        elif ext == '.csv':
            ex = read_csv(raw)
        elif ext == '.pdf':
            text = pdf_text(raw)
            ex = read_report_text(text) if text.strip() else Extraction(
                kind='report', note='No readable text (scanned PDF); stored only.')
        elif ext == '.docx':
            ex = read_report_text(docx_text(raw))
        elif ext == '.txt':
            ex = read_report_text(raw.decode('utf-8', 'ignore'))
        elif ext in ('.png', '.jpg', '.jpeg'):
            text = image_text(raw)
            ex = read_report_text(text, kind='image') if text else Extraction(
                kind='image', note='Photo stored only (OCR not installed).')
        else:
            return Extraction(note='Stored only.')
    except Exception as error:                              # a damaged or unusual file: keep it, explain why
        return Extraction(kind='other', note=f'The file could not be read ({type(error).__name__}).')
    if not ex.note:
        ex.note = describe(ex)
    return ex


def describe(ex: Extraction) -> str:
    """'9 lab results and 1 vitals reading found (4 converted from mmol/L, umol/L...)'."""
    parts = []
    names = {'Lab Results': ('lab result', 'lab results'), 'Vitals Daily': ('vitals day', 'vitals days'),
             'Wearable Daily': ('activity/sleep day', 'activity/sleep days'), 'Women Health': ('cycle', 'cycles'),
             'Habits Weekly': ('habits week', 'habits weeks'), 'Symptoms Timeline': ('symptom entry', 'symptom entries')}
    for sheet, (one, many) in names.items():
        n = len(ex.sheets.get(sheet, ()))
        if n:
            parts.append(f'{n} {one if n == 1 else many}')
    if not parts:
        return 'No health values found; stored only.'
    return ', '.join(parts) + '.'
