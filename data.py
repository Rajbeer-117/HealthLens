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
        return str(self.profile.get('Sex', '')).strip()

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
