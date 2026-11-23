"""Make realistic single documents for one synthetic patient (PC0303) from patient_data.zip.

Real patients do not have one workbook with everything: they get a lab report now and then, export readings from
a blood-pressure monitor or a fitness tracker, and keep visit notes. These files are what a patient would upload,
one by one, to test document-by-document extraction. All values are synthetic (taken from PC0303.xlsx).

Run once (needs reportlab and python-docx, only for making the samples, not for running HealthLens):
    pip install reportlab python-docx
    python validation/make_sample_documents.py
Output: sample_documents/ (already included in the project).
"""
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from healthlens.data import to_datetime  # noqa: E402

PID = 'PC0303'
NAME = 'Maya Patel'                # synthetic name for the sample documents (the dataset has no names)
OUT = ROOT / 'sample_documents'
LAB_NAME = 'Northside Community Laboratory (fictional)'
SI = {  # dataset unit -> unit used on Canadian reports, and the factor
    'Fasting glucose': ('mmol/L', 1 / 18.016, 1), 'LDL cholesterol': ('mmol/L', 1 / 38.67, 2),
    'HDL cholesterol': ('mmol/L', 1 / 38.67, 2), 'Total cholesterol': ('mmol/L', 1 / 38.67, 2),
    'Triglycerides': ('mmol/L', 1 / 88.57, 2), 'Creatinine': ('umol/L', 88.42, 0), 'Hemoglobin': ('g/L', 10, 0),
    'Vitamin D': ('nmol/L', 2.496, 0), 'Vitamin B12': ('pmol/L', 1 / 1.355, 0),
}


def book():
    with zipfile.ZipFile(ROOT / 'patient_data.zip') as z:
        name = next(n for n in z.namelist() if n.endswith(f'/{PID}.xlsx'))
        return pd.read_excel(io.BytesIO(z.read(name)), sheet_name=None)


def labs_on(b, day):
    lab = b['Lab Results'].copy()
    lab['Date'] = to_datetime(lab['Date'])
    return lab[lab['Date'].dt.date == pd.Timestamp(day).date()]


def si_range(test, text):
    unit, f, dec = SI[test]
    import re
    nums = re.findall(r'\d+(?:\.\d+)?', str(text))
    fmt = lambda v: f'{float(v) * f:.{dec}f}'
    if '–' in str(text) or '-' in str(text):
        return f'{fmt(nums[0])} - {fmt(nums[1])}'
    return ('< ' if '<' in str(text) else '> ') + fmt(nums[0])


def report_lines(rows, si):
    out = []
    for _, r in rows.iterrows():
        test, value, unit, ref = r['Test Name'], float(r['Result']), r['Unit'], r['Reference Range']
        if si and test in SI:
            u, f, dec = SI[test]
            value, unit, ref = f'{value * f:.{dec}f}', u, si_range(test, ref)
        else:
            value = f'{value:g}'
            ref = str(ref).replace('–', ' - ')
        name = {'Fasting glucose': 'Glucose, Fasting', 'LDL cholesterol': 'LDL Cholesterol (calc)', 'HbA1c': 'Hemoglobin A1c',
                'Total cholesterol': 'Cholesterol, Total', 'HDL cholesterol': 'HDL Cholesterol'}.get(test, test)
        out.append((name, value, unit, ref))
    return out


def header(day):
    return [LAB_NAME, 'Patient Name: ' + NAME + '      Patient ID: ' + PID,
            'Sex: F      DOB: 1994-03-15', f'Collected: {pd.Timestamp(day):%b %d, %Y} 08:10      Reported: {pd.Timestamp(day) + pd.Timedelta(days=1):%Y-%m-%d}']


def make_pdf(path, day, rows, si=True):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    c.setFont('Helvetica-Bold', 14); c.drawString(54, y, header(day)[0]); y -= 26
    c.setFont('Helvetica', 10)
    for line in header(day)[1:]:
        c.drawString(54, y, line); y -= 15
    y -= 12
    c.setFont('Helvetica-Bold', 10)
    for x, t in ((54, 'TEST'), (250, 'RESULT'), (330, 'UNITS'), (430, 'REFERENCE RANGE')):
        c.drawString(x, y, t)
    y -= 16; c.setFont('Helvetica', 10)
    for name, value, unit, ref in report_lines(rows, si):
        for x, t in ((54, name), (250, value), (330, unit), (430, ref)):
            c.drawString(x, y, t)
        y -= 15
    y -= 20
    c.setFont('Helvetica-Oblique', 8)
    c.drawString(54, y, 'Synthetic sample document for HealthLens testing. Not a real patient or laboratory.')
    c.save()


def make_docx(path, day, rows):
    import docx
    d = docx.Document()
    d.add_heading(LAB_NAME, level=1)
    for line in header(day)[1:]:
        d.add_paragraph(line)
    t = d.add_table(rows=1, cols=4)
    for cell, text in zip(t.rows[0].cells, ('Test', 'Result', 'Units', 'Reference range')):
        cell.text = text
    for name, value, unit, ref in report_lines(rows, si=False):
        for cell, text in zip(t.add_row().cells, (name, value, unit, ref)):
            cell.text = text
    d.add_paragraph('Synthetic sample document for HealthLens testing. Not a real patient or laboratory.')
    d.save(str(path))


def make_png(path, day, rows):
    from PIL import Image, ImageDraw, ImageFont
    lines = header(day) + ['', f'{"TEST":<28}{"RESULT":<10}{"UNITS":<10}REFERENCE RANGE']
    lines += [f'{n:<28}{v:<10}{u:<10}{r}' for n, v, u, r in report_lines(rows, si=True)]
    lines += ['', 'Synthetic sample document for HealthLens testing.']
    try:
        font = ImageFont.truetype('DejaVuSansMono.ttf', 22)
    except OSError:
        font = ImageFont.load_default()
    img = Image.new('RGB', (1100, 60 + 34 * len(lines)), 'white')
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((40, 30 + 34 * i), line, fill='black', font=font)
    img.save(path)


def main():
    OUT.mkdir(exist_ok=True)
    b = book()
    days = sorted(set(to_datetime(b['Lab Results']['Date']).dt.date))
    make_pdf(OUT / '01_lab_report_2025-09-25.pdf', days[0], labs_on(b, days[0]), si=True)
    make_docx(OUT / '02_lab_report_2026-01-22.docx', days[2], labs_on(b, days[2]))
    vit = b['Vitals Daily'].copy(); vit['Date'] = to_datetime(vit['Date'])
    pd.DataFrame({'Patient ID': PID, 'Date': vit['Date'].dt.strftime('%Y-%m-%d'), 'Time': '07:30', 'Systolic (mmHg)': vit['Systolic BP'],
                  'Diastolic (mmHg)': vit['Diastolic BP'], 'Pulse (bpm)': vit['Resting HR']}
                 ).to_csv(OUT / '03_home_blood_pressure_monitor.csv', index=False)
    wear = b['Wearable Daily'].copy(); wear['Date'] = to_datetime(wear['Date'])
    pd.DataFrame({'Patient ID': PID, 'Date': wear['Date'].dt.strftime('%Y-%m-%d'), 'Steps': wear['Steps'],
                  'Sleep (minutes)': (wear['Sleep Hours'] * 60).round().astype('Int64'), 'Resting heart rate': wear['Resting HR'],
                  'HRV (ms)': wear['HRV ms'], 'Active minutes': wear['Active Minutes'], 'Stress score': wear['Stress Score 1-10']}
                 ).to_csv(OUT / '04_fitness_tracker_export.csv', index=False)
    wh = b['Women Health'].copy(); wh['Start Date'] = to_datetime(wh['Start Date'])
    pd.DataFrame({'Patient ID': PID, 'Period start': wh['Start Date'].dt.strftime('%Y-%m-%d'), 'Cycle length (days)': wh['Cycle Length'],
                  'Period length (days)': wh['Bleeding Days']}).to_csv(OUT / '05_period_tracker.csv', index=False)
    make_png(OUT / '06_lab_report_photo_2026-04-18.png', days[3], labs_on(b, days[3]))
    v = vit[vit['Date'].dt.date == days[3]].iloc[0]
    (OUT / '07_clinic_visit_2026-04-18.txt').write_text(
        f'Clinic visit summary (synthetic sample)\nPatient name: {NAME}\nPatient ID: {PID}\nVisit date: {days[3]:%Y-%m-%d}\n'
        f'Blood pressure: {v["Systolic BP"]:.0f}/{v["Diastolic BP"]:.0f} mmHg   Heart rate: {v["Resting HR"]:.0f} bpm\n'
        f'Weight: {v["Weight kg"] * 2.2046:.0f} lb   BMI: {v["BMI"]:.1f}\n'
        'Notes: Routine follow-up. Discussed activity and sleep. Repeat labs as planned.\n', encoding='utf-8')
    labs_on(b, days[1]).assign(Date=lambda d: d['Date'].dt.strftime('%Y-%m-%d')).pipe(lambda d: d.insert(0, 'Patient ID', PID) or d).to_csv(
        OUT / '08_lab_results_2026-01-08.csv', index=False)
    print('Wrote', sorted(p.name for p in OUT.iterdir()))


if __name__ == '__main__':
    main()
