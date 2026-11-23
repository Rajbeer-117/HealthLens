"""Health records stored in the database: uploaded files, last view per user, sign-in from another computer,
moving files saved by older versions, database upgrade, and each user seeing only their own records.
Owner: Rajbeer Kaur Dutta (Data & Health Information Lead). Shared set-up (database per test, sign-up/sign-in helpers) is in tests/conftest.py.
Run all tests:  python -m pytest -q"""
from contextlib import closing
import io
import json
import re
import sqlite3
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import (PASSWORD, PDF, ROOT, app_module, llm, workbook_bytes, register, login, new_browser, csrf, upload, rows, make_admin, uid, team_names, team_post, record_key)  # noqa: F401


def test_uploaded_files_are_stored_in_the_database_not_on_disk(env):
    c = new_browser(make_account=True)
    upload(c, [('PC0101.xlsx', workbook_bytes('PC0101')), ('scan.pdf', PDF)])
    docs = {r['original_name']: r for r in rows('SELECT * FROM documents')}
    assert docs['PC0101.xlsx']['content'] == workbook_bytes('PC0101')
    assert docs['scan.pdf']['content'] == PDF
    assert not (env / 'uploads').exists()


def test_same_account_works_from_another_computer(env):
    first = new_browser(make_account=True)
    make_admin('tester')                                                     # several records: team account
    upload(first, [('PC0101.xlsx', workbook_bytes('PC0101')), ('PC0303.xlsx', workbook_bytes('PC0303')), ('scan.pdf', PDF)])
    first.get('/summary?patient=PC0303&span=all&category=pcos')
    app_module._analysis_cache.clear()                                       # as if the app had been restarted
    second = new_browser()                                                   # no cookies from the first browser
    html = second.get('/summary').get_data(as_text=True)
    assert '<span>Patient ID</span><strong>PC0303</strong>' in html            # last viewed patient restored
    assert 'Full record' in html and '2025-09-01 to 2026-08-31' in html     # last data span restored
    docs = second.get('/documents').get_data(as_text=True)
    assert 'PC0101.xlsx' in docs and 'PC0303.xlsx' in docs and 'scan.pdf' in docs
    doc = rows("SELECT id FROM documents WHERE original_name='scan.pdf'")[0]
    assert second.get(f'/documents/{doc["id"]}/download').data == PDF


def test_last_view_is_saved_per_user(env):
    c = new_browser(make_account=True)
    make_admin('tester')                                                     # several records: team account
    upload(c, [('PC0101.xlsx', workbook_bytes('PC0101')), ('PC0303.xlsx', workbook_bytes('PC0303'))])
    c.get('/summary?patient=PC0101&span=3m')
    assert dict(rows('SELECT patient, span FROM user_state')[0]) == {'patient': 'PC0101', 'span': '3m'}
    c.get('/summary?patient=PC0303&span=all')
    assert dict(rows('SELECT patient, span FROM user_state')[0]) == {'patient': 'PC0303', 'span': 'all'}


def test_files_saved_on_disk_by_older_versions_are_moved_into_the_database(env):
    c = new_browser(make_account=True)
    uid = rows('SELECT id FROM users')[0]['id']
    folder = env / 'uploads' / str(uid)
    (folder / 'docs').mkdir(parents=True)
    (folder / 'Old.xlsx').write_bytes(workbook_bytes('PC0101'))              # workbook not yet in the table
    (folder / 'docs' / 'abc123.pdf').write_bytes(PDF)                         # row exists but has no content
    with app_module.db() as conn:
        conn.execute("INSERT INTO documents(user_id,original_name,stored_name,kind,patient_id,size,origin,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (uid, 'old.pdf', 'abc123.pdf', 'document', None, len(PDF), 'Computer', '2026-11-01T00:00:00+00:00'))
    html = c.get('/documents').get_data(as_text=True)
    assert 'Old.xlsx' in html and 'old.pdf' in html
    stored = {r['original_name']: r['content'] for r in rows('SELECT original_name, content FROM documents')}
    assert stored['Old.xlsx'] == workbook_bytes('PC0101') and stored['old.pdf'] == PDF
    assert '<span>Patient ID</span><strong>PC0101</strong>' in c.get('/summary').get_data(as_text=True)   # ID read from the old file


def test_deleted_files_do_not_come_back_from_old_disk_copies(env):
    c = new_browser(make_account=True)
    uid = rows('SELECT id FROM users')[0]['id']
    folder = env / 'uploads' / str(uid)
    folder.mkdir(parents=True)
    (folder / 'Old.xlsx').write_bytes(workbook_bytes('PC0101'))
    c.get('/documents')
    doc = rows('SELECT id FROM documents')[0]
    c.post(f'/documents/{doc["id"]}/delete', data={'csrf': csrf(c)}, follow_redirects=True)
    assert not (folder / 'Old.xlsx').exists()
    assert 'Old.xlsx' not in c.get('/documents').get_data(as_text=True)       # second visit: not re-imported
    assert rows('SELECT * FROM documents') == []


def test_existing_database_from_the_previous_version_is_upgraded(env):
    with closing(sqlite3.connect(app_module.DB)) as conn, conn:                             # schema used before this update
        conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL, email TEXT UNIQUE COLLATE NOCASE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)')
        conn.execute('CREATE TABLE documents (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, original_name TEXT NOT NULL, stored_name TEXT NOT NULL, kind TEXT NOT NULL, patient_id TEXT, size INTEGER NOT NULL, origin TEXT NOT NULL, created_at TEXT NOT NULL)')
        conn.execute('INSERT INTO users(username,email,password_hash,created_at) VALUES(?,?,?,?)',
                     ('olduser', 'old@example.com', app_module.hash_password(PASSWORD), '2026-10-01T00:00:00+00:00'))
    c = new_browser('olduser')
    assert c.get('/summary').status_code == 200
    cols = {r[1] for r in rows('PRAGMA table_info(documents)')}
    assert 'content' in cols


def test_returning_user_sees_their_analysed_report(env):
    c = new_browser(make_account=True)
    assert 'Welcome to HealthLens' in c.get('/summary').get_data(as_text=True)          # first visit: nothing yet
    upload(c, [('PC0101.xlsx', workbook_bytes('PC0101'))])
    c.post('/logout')
    html = new_browser().get('/summary').get_data(as_text=True)                         # second sign-in
    assert 'Welcome to HealthLens' not in html
    assert '<span>Patient ID</span><strong>PC0101</strong>' in html and 'Recent flags' in html and 'Latest values' in html


def test_users_only_see_their_own_patients(env):
    a = new_browser('alice', make_account=True)
    upload(a, [('PC0101.xlsx', workbook_bytes('PC0101'))])
    b = new_browser('bob', make_account=True)
    assert 'Welcome to HealthLens, bob!' in b.get('/summary?patient=PC0101').get_data(as_text=True)
    assert b.get('/api/patients').get_json() == {'patients': []}


# ---------------------------------------------------------------- reading values out of single documents
from pathlib import Path  # noqa: E402

import pandas as pd  # noqa: E402

from healthlens import extract as X  # noqa: E402
from healthlens.data import merge_workbooks, to_datetime  # noqa: E402

SAMPLES = ROOT / 'sample_documents'


def pc0303_labs(day):
    with zipfile.ZipFile(ROOT / 'patient_data.zip') as z:
        name = next(n for n in z.namelist() if n.endswith('/PC0303.xlsx'))
        lab = pd.read_excel(io.BytesIO(z.read(name)), sheet_name='Lab Results')
    lab['Date'] = to_datetime(lab['Date'])
    return lab[lab['Date'].dt.date == pd.Timestamp(day).date()]


@pytest.mark.parametrize('name, day', [('01_lab_report_2025-09-25.pdf', '2025-09-25'),
                                       ('02_lab_report_2026-01-22.docx', '2026-01-22'),
                                       ('08_lab_results_2026-01-08.csv', '2026-01-08')])
def test_lab_reports_are_read_exactly(name, day):
    """PDF (in Canadian mmol/L units), Word and CSV lab reports give back the same tests and values as the source."""
    ex = X.extract(name, (SAMPLES / name).read_bytes())
    got = ex.sheets['Lab Results'].set_index('Test Name')
    orig = pc0303_labs(day).set_index('Test Name')
    assert set(got.index) == set(orig.index)
    for test, row in orig.iterrows():
        assert abs(got.loc[test, 'Result'] - row['Result']) <= max(0.01, 0.006 * row['Result']), test
    assert (pd.to_datetime(got['Date']).dt.date == pd.Timestamp(day).date()).all()
    assert ex.patient_id == 'PC0303'                                             # every sample names its patient


def test_device_exports_are_read():
    bp = X.extract('bp.csv', (SAMPLES / '03_home_blood_pressure_monitor.csv').read_bytes())
    assert bp.kind == 'table' and 'Vitals Daily' in bp.sheets and bp.readings == 365 and bp.patient_id == 'PC0303'
    assert {'Systolic BP', 'Diastolic BP', 'Resting HR'} <= set(bp.sheets['Vitals Daily'].columns)
    fit = X.extract('fit.csv', (SAMPLES / '04_fitness_tracker_export.csv').read_bytes())
    sleep = fit.sheets['Wearable Daily']['Sleep Hours'].dropna()
    assert fit.readings == 365 and 3 < sleep.mean() < 11                        # minutes converted to hours
    cyc = X.extract('cycles.csv', (SAMPLES / '05_period_tracker.csv').read_bytes())
    assert 'Women Health' in cyc.sheets and cyc.readings == 10 and cyc.patient_id == 'PC0303'


def test_clinic_note_gives_vitals_with_converted_weight():
    ex = X.extract('visit.txt', (SAMPLES / '07_clinic_visit_2026-04-18.txt').read_bytes())
    v = ex.sheets['Vitals Daily'].iloc[0]
    assert v['Systolic BP'] > 80 and 40 < v['Weight kg'] < 150 and str(v['Date'])[:10] == '2026-04-18'


@pytest.mark.parametrize('test, value, unit, expected', [
    ('Fasting glucose', 6.0, 'mmol/L', 108), ('LDL cholesterol', 3.0, 'mmol/L', 116), ('Triglycerides', 1.7, 'mmol/L', 151),
    ('HbA1c', 48, 'mmol/mol', 6.5), ('Creatinine', 88.42, 'µmol/L', 1.0), ('Hemoglobin', 140, 'g/L', 14.0),
    ('Vitamin D', 75, 'nmol/L', 30), ('LDL cholesterol', 130, 'mg/dL', 130)])
def test_units_are_converted_to_the_dataset_units(test, value, unit, expected):
    v, u, _ = X.convert(test, value, unit)
    assert u == X.UNIT_OF[test] and abs(v - expected) <= 0.05 * expected


def test_name_variants_are_recognised():
    for written, canon in [('LDL-C', 'LDL cholesterol'), ('Cholesterol, Total', 'Total cholesterol'), ('Hemoglobin A1c', 'HbA1c'),
                           ('Glucose, Fasting', 'Fasting glucose'), ('ALT (SGPT)', 'ALT'), ('25-OH Vitamin D', 'Vitamin D'),
                           ('Hgb', 'Hemoglobin'), ('Hemoglobin Alc', 'HbA1c')]:
        assert X.canonical_test(written) == canon, written


def test_misread_or_impossible_values_are_never_recorded():
    """OCR can turn 'A1c' into 'Alc' or drop a decimal point: a wrong value must not become a false flag."""
    text = ('Collected: 2026-04-18\nPatient ID: PC0303\nHemoglobin 5.74 %\nGlucose, Fasting 5100 mg/dL\n'
            'ALT 22 U/L 7 - 56\n')
    labs = X.read_report_text(text).sheets['Lab Results']
    assert list(labs['Test Name']) == ['ALT']


def test_report_without_a_date_or_values_is_stored_only():
    assert X.extract('note.txt', b'Patient ID: PC0303\nLDL 130 mg/dL').readings == 0                  # no date
    assert 'stored only' in X.extract('letter.txt', b'Dear patient, see you next week. 2026-04-01').note
    assert 'no selectable text' in X.extract('scan.pdf', PDF).note or X.extract('scan.pdf', PDF).readings == 0


def test_documents_for_one_patient_combine_without_double_counting():
    lab = X.extract('a.pdf', (SAMPLES / '01_lab_report_2025-09-25.pdf').read_bytes()).sheets
    again = X.extract('b.pdf', (SAMPLES / '01_lab_report_2025-09-25.pdf').read_bytes()).sheets   # the same report twice
    assert len(again['Lab Results']) == 15
    bp = X.extract('bp.csv', (SAMPLES / '03_home_blood_pressure_monitor.csv').read_bytes()).sheets
    merged = merge_workbooks([lab, bp, again])
    assert len(merged['Lab Results']) == 15 and len(merged['Vitals Daily']) == 365


# ---------------------------------------------------------------- only documents of this patient are added
def resaved(pid):
    """The same workbook opened and saved again: different bytes, same values."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(workbook_bytes(pid)))
    out = io.BytesIO(); wb.save(out)
    assert out.getvalue() != workbook_bytes(pid)
    return out.getvalue()


def test_resaved_copy_of_an_uploaded_document_is_not_added_again(env):
    c = new_browser(make_account=True)
    upload(c, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    html = upload(c, [('PC0002.xlsx', resaved('PC0002'))]).get_data(as_text=True)
    assert 'PC0002.xlsx: already uploaded.' in html
    assert len(rows('SELECT id FROM documents')) == 1


def test_another_patients_exports_are_refused(env):
    """PC0303's exports show PC0303, and nothing else in them (name, date of birth) matches PC0002's record."""
    c = new_browser(make_account=True)
    upload(c, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    names = ['03_home_blood_pressure_monitor.csv', '05_period_tracker.csv', '08_lab_results_2026-01-08.csv']
    html = upload(c, [(n, (SAMPLES / n).read_bytes()) for n in names]).get_data(as_text=True)
    for n in names:
        assert n in html
    assert html.count('Not added.') == 3
    assert [r['original_name'] for r in rows('SELECT original_name FROM documents')] == ['PC0002.xlsx']


def test_period_data_is_not_added_to_a_male_record(env):
    c = new_browser(make_account=True)
    upload(c, [('PC0002.xlsx', workbook_bytes('PC0002'))])                  # PC0002.xlsx says Male
    tracker = b'Period start,Cycle length (days)\n2026-01-03,31\n2026-02-03,30\n'
    html = upload(c, [('my_periods.csv', tracker)]).get_data(as_text=True)
    assert 'my_periods.csv contains period data, but PC0002.xlsx says Male. Not added.' in html
    assert not rows("SELECT id FROM documents WHERE original_name='my_periods.csv'")
def test_own_export_without_an_id_and_with_new_values_is_added(env):
    c = new_browser(make_account=True)
    upload(c, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    bp = b'Date,Systolic (mmHg),Diastolic (mmHg)\n2026-09-02,128,82\n2026-09-03,126,80\n'
    html = upload(c, [('bp_september.csv', bp)]).get_data(as_text=True)
    assert 'bp_september.csv: 2 vitals days.' in html
    assert {r['patient_id'] for r in rows('SELECT patient_id FROM documents')} == {'tester'}
