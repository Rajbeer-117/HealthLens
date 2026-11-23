"""A patient's record, built document by document (DATABASE_DESIGN.md).

The record belongs to the account from the first upload; Patient IDs, sex and date of birth printed in documents are
facts compared with what the record already knows. The back end decides; the patient is never asked to vouch:
  reject  a different name, sex or date of birth, period data for a male record, or a Patient ID the record has not
          seen unless the same name or date of birth shows it is the same person;
  accept  everything else (no ID, a known ID, the first document, old reports);
  leave out impossible single readings, keeping the rest of the document.
"""
import json

import pandas as pd

from conftest import ROOT, app_module, new_browser, upload, rows, uid, csrf, record_key, workbook_bytes  # noqa: F401
from healthlens import record_rules as R, risk_model
from healthlens.data import PatientRecord

DOCS = ROOT / 'sample_documents'


def sample(name):
    return name, (DOCS / name).read_bytes()


def without_id(name):
    """A sample CSV/TXT with its Patient ID removed, as a phone or fitness-tracker export would be."""
    text = (DOCS / name).read_text(encoding='utf-8')
    if name.endswith('.csv'):
        text = '\n'.join(','.join(line.split(',')[1:]) for line in text.splitlines()) + '\n'
    else:
        text = '\n'.join(l for l in text.splitlines() if not l.startswith('Patient ID')) + '\n'
    return name, text.encode('utf-8')


def with_id(name, new_id):
    return f'{new_id}_{name}', (DOCS / name).read_bytes().replace(b'PC0303', new_id.encode())


def docs(name):
    return {r['original_name']: r for r in rows('SELECT * FROM documents WHERE user_id=?', uid(name))}


def note(text):
    """A short typed visit note (TXT), as a clinic might give."""
    return 'note.txt', ('Collected: 2026-02-01\n' + text + '\nLDL cholesterol 120 mg/dL <100\n').encode()


# ---------------------------------------------------------------- the record belongs to the account
def test_record_key_is_the_accounts_and_never_changes(env):
    c = new_browser('maya', make_account=True)
    upload(c, [without_id('04_fitness_tracker_export.csv')])           # no ID at all
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])                 # first ID seen: PC0303
    assert {d['patient_id'] for d in docs('maya').values()} == {'maya'}
    assert [r['value'] for r in rows('SELECT value FROM patient_identifiers')] == ['PC0303']
    assert {r['patient_code'] for r in rows('SELECT patient_code FROM health_records')} == {'maya'}
    page = c.get('/summary').get_data(as_text=True)
    assert '<span>Patient ID</span><strong>PC0303</strong>' in page


def test_documents_without_an_id_join_in_any_order(env):
    c = new_browser('lena', make_account=True)
    upload(c, [sample('08_lab_results_2026-01-08.csv')])
    upload(c, [without_id('03_home_blood_pressure_monitor.csv')])
    upload(c, [without_id('07_clinic_visit_2026-04-18.txt')])
    assert {d['kind'] for d in docs('lena').values()} == {'patient'}


# ---------------------------------------------------------------- a Patient ID not seen before
def test_new_id_with_the_same_name_is_added(env):
    """Another clinic's number, but the report shows the same patient name: same person, added."""
    c = new_browser('omar', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])                  # Maya Patel, PC0303
    html = upload(c, [with_id('07_clinic_visit_2026-04-18.txt', 'HOSP77')]).get_data(as_text=True)
    assert 'Not added' not in html
    assert docs('omar')['HOSP77_07_clinic_visit_2026-04-18.txt']['kind'] == 'patient'
    assert {r['value'] for r in rows('SELECT value FROM patient_identifiers')} == {'PC0303', 'HOSP77'}
    upload(c, [with_id('03_home_blood_pressure_monitor.csv', 'HOSP77')])  # HOSP77 is now known: added
    assert docs('omar')['HOSP77_03_home_blood_pressure_monitor.csv']['kind'] == 'patient'


def test_new_id_with_the_same_date_of_birth_is_added(env):
    c = new_browser('ines', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])                  # DOB 1994-03-15
    html = upload(c, [note('Patient ID: LAB55\nDOB: 1994-03-15')]).get_data(as_text=True)
    assert 'Not added' not in html and docs('ines')['note.txt']['kind'] == 'patient'


def test_new_id_without_a_matching_name_or_birth_date_is_rejected(env):
    c = new_browser('ivy', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])
    html = upload(c, [with_id('08_lab_results_2026-01-08.csv', 'PC0999')]).get_data(as_text=True)
    assert ('PC0999_08_lab_results_2026-01-08.csv shows Patient ID PC0999, but your record has PC0303 and the document '
            'does not show the same name or date of birth. Not added.') in html
    assert 'PC0999_08_lab_results_2026-01-08.csv' not in docs('ivy')
    assert [r['value'] for r in rows('SELECT value FROM patient_identifiers')] == ['PC0303']


def test_deleting_the_only_document_with_an_unconfirmed_id_forgets_that_id(env):
    """A wrong first upload does not tie the record to someone else's ID."""
    c = new_browser('noah', make_account=True)
    upload(c, [with_id('08_lab_results_2026-01-08.csv', 'PC0999')])     # someone else's report, uploaded first
    wrong = docs('noah')['PC0999_08_lab_results_2026-01-08.csv']
    c.post(f'/documents/{wrong["id"]}/delete', data={'csrf': csrf(c)})
    assert rows('SELECT * FROM patient_identifiers') == [] and rows('SELECT * FROM observations') == []
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])                 # their own report is accepted
    assert docs('noah')['01_lab_report_2025-09-25.pdf']['kind'] == 'patient'


# ---------------------------------------------------------------- reject: contradicts what the record knows
def test_report_with_another_name_is_rejected(env):
    c = new_browser('zara', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])                  # Maya Patel
    html = upload(c, [note('Patient name: Jane Doe\nPatient ID: PC0303')]).get_data(as_text=True)
    assert 'note.txt lists name Jane Doe, but 01_lab_report_2025-09-25.pdf says Maya Patel. Not added.' in html
    assert 'note.txt' not in docs('zara')


def test_the_same_name_written_differently_is_accepted(env):
    c = new_browser('lou', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])
    upload(c, [note('Patient name: PATEL, Maya R.\nPatient ID: PC0303')])
    assert docs('lou')['note.txt']['kind'] == 'patient'


def test_report_with_another_sex_is_rejected(env):
    c = new_browser('kim', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])                  # Female
    html = upload(c, [note('Patient ID: PC0303\nSex: M')]).get_data(as_text=True)
    assert 'note.txt lists sex as Male, but 01_lab_report_2025-09-25.pdf says Female. Not added.' in html
    assert 'note.txt' not in docs('kim')


def test_report_with_another_birth_date_is_rejected(env):
    c = new_browser('ravi', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])                  # DOB 1994-03-15
    html = upload(c, [note('Patient ID: PC0303\nDOB: 1961-07-02')]).get_data(as_text=True)
    assert 'note.txt lists date of birth 1961-07-02, but 01_lab_report_2025-09-25.pdf says 1994-03-15. Not added.' in html


def test_profile_shows_who_the_record_belongs_to_without_any_form(env):
    c = new_browser('sam', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])
    page = c.get('/profile').get_data(as_text=True)
    assert 'Maya Patel' in page and '1994-03-15' in page and '· from 01_lab_report_2025-09-25.pdf' in page
    assert '<form' not in page.split('id="details"')[1].split('</section>')[0]          # read-only
    assert c.post('/profile/details', data={'csrf': csrf(c), 'sex': 'Male'}).status_code in (404, 405)


def test_there_is_no_button_to_claim_a_document(env):
    c = new_browser('ted', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])
    doc_id = docs('ted')['01_lab_report_2025-09-25.pdf']['id']
    assert c.post(f'/documents/{doc_id}/confirm', data={'csrf': csrf(c), 'answer': 'yes'}).status_code in (404, 405)
    assert "it's mine" not in c.get('/documents').get_data(as_text=True)


# ---------------------------------------------------------------- leave out impossible readings
def test_impossible_readings_are_left_out_and_the_rest_kept(env):
    c = new_browser('ada', make_account=True)
    bp = b'Date,Systolic (mmHg),Diastolic (mmHg)\n2026-01-02,121,79\n2026-01-03,1180,77\n2026-01-04,118,76\n'
    html = upload(c, [('bp.csv', bp)]).get_data(as_text=True)
    assert '1 impossible reading left out' in html
    values = sorted(r['value'] for r in rows("SELECT value FROM observations WHERE measure_code='sbp'"))
    assert values == [118, 121]


def test_drop_implausible_respects_units_and_future_dates():
    labs = pd.DataFrame({'Date': ['2026-01-01', '2026-01-01', '2999-01-01'], 'Test Name': ['Fasting glucose'] * 3,
                         'Result': [5.5, 9000, 90], 'Unit': ['mmol/L', 'mg/dL', 'mg/dL']})
    sheets = {'Lab Results': labs}
    assert R.drop_implausible(sheets) == 2                               # 9000 mg/dL and the future date
    assert sheets['Lab Results']['Result'].tolist() == [5.5]             # 5.5 mmol/L is not judged in mg/dL


# ---------------------------------------------------------------- queryable facts
def test_observations_have_one_row_per_measurement_with_their_source(env):
    c = new_browser('bea', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])
    upload(c, [sample('08_lab_results_2026-01-08.csv')])
    data = c.get(f'/api/patients/{record_key(c)}/observations?measure=ldl').get_json()['observations']
    assert [o['measured_at'] for o in data] == ['2025-09-25', '2026-01-08']
    assert {o['document'] for o in data} == {'01_lab_report_2025-09-25.pdf', '08_lab_results_2026-01-08.csv'}
    assert rows("SELECT category FROM measure_categories WHERE measure_code='ldl'")[0]['category'] == 'cardiovascular'


def test_a_risk_snapshot_is_kept_per_set_of_documents(env):
    c = new_browser('cal', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')]); c.get('/summary'); c.get('/summary')
    upload(c, [sample('03_home_blood_pressure_monitor.csv')]); c.get('/summary')
    snaps = rows("SELECT documents, model_version, inputs_missing FROM risk_assessments WHERE category='cardiovascular' ORDER BY id")
    assert len(snaps) == 2 and snaps[0]['documents'] != snaps[1]['documents']
    assert 'seed' in snaps[0]['model_version'] and 'vitals' in json.loads(snaps[0]['inputs_missing'])


def test_documents_from_earlier_versions_get_their_facts_once(env):
    c = new_browser('dov', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])
    before = len(rows('SELECT id FROM observations'))                    # the lab results HealthLens tracks
    with app_module.db() as conn:                                        # as an earlier version left it
        conn.execute('DELETE FROM observations')
        conn.execute('DELETE FROM patient_identifiers')
        conn.execute('DELETE FROM patient_attributes')
        conn.execute('UPDATE documents SET facts_done=NULL')
    c.get('/summary'); c.get('/summary')
    assert len(rows('SELECT id FROM patient_identifiers')) == 1 and len(rows('SELECT id FROM observations')) == before > 0


# ---------------------------------------------------------------- pure rules
def test_judge_rules():
    known = {'name': R.Known('Maya Patel', False, 'a.pdf'), 'sex': R.Known('Female', False, 'a.pdf'),
             'birth_date': R.Known('1994-03-15', False, 'a.pdf')}
    judge = lambda **f: R.judge(R.DocFacts(**f), known, {'A1'}, 'x').action
    assert judge(sex='Female') == 'accept' and judge(sex='Male') == 'reject'
    assert judge(birth_date='1960-01-01') == 'reject'
    when = pd.Timestamp('2025-09-25')
    assert judge(age=31, doc_date=when) == 'accept' and judge(age=58, doc_date=when) == 'reject'
    assert judge(name='Patel, Maya') == 'accept' and judge(name='Jane Doe') == 'reject'
    assert judge(patient_id='A1') == 'accept'                                        # known ID
    assert judge(patient_id='B1') == 'reject'                                        # new ID, nothing matches
    assert judge(patient_id='B1', name='Maya Patel') == 'accept'                     # new ID, same name
    assert judge(patient_id='B1', birth_date='1994-03-15') == 'accept'               # new ID, same date of birth
    assert R.judge(R.DocFacts(patient_id='A1'), {}, set(), 'x').action == 'accept'   # first ID on the record
    assert R.judge(R.DocFacts(has_cycles=True), {'sex': R.Known('Male', False)}, set(), 'x').action == 'reject'
    assert {R.judge(R.DocFacts(**f), known, {'A1'}, 'x').action for f in ({}, {'name': ''})} == {'accept'}


def test_names_match_ignoring_capitals_order_punctuation_and_middle_names():
    assert R.same_name('Doe, Jane', 'jane doe') and R.same_name('Jane A. Doe', 'Jane Doe')
    assert R.same_name("Mary-Ann O'Neil", "mary ann o'neil")
    assert not R.same_name('Jane Doe', 'John Doe') and not R.same_name('Maya', 'Maya Patel')


def test_period_tracker_alone_counts_as_female():
    cycles = pd.DataFrame({'Start Date': pd.date_range('2025-01-01', periods=6, freq='38D'),
                           'Cycle Length': [38, 41, 33, 45, 36, 40]})
    rec = PatientRecord({'Women Health': cycles})
    assert rec.sex == 'Female' and rec.sex_inferred
    assert PatientRecord({'Patient Profile': pd.DataFrame({'Field': ['Sex'], 'Value': ['Male']})}).sex == 'Male'


def test_risk_factor_names_are_readable():
    for name in ('cycle_mean', 'cycle_sd', 'cycle_long', 'ldl_out', 'bmi_mean90', 'age'):
        assert '_' not in risk_model.label(name)


def test_documents_left_waiting_by_an_earlier_version_become_stored_only(env):
    c = new_browser('vic', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])
    with app_module.db() as conn:                                         # as the earlier "ask the patient" version left it
        conn.execute("UPDATE documents SET kind='pending'")
    c.get('/documents')
    assert [d['kind'] for d in docs('vic').values()] == ['document']


def test_profile_card_shows_the_records_name_and_every_patient_id(env):
    c = new_browser('una', make_account=True)
    upload(c, [sample('01_lab_report_2025-09-25.pdf')])                   # Maya Patel, PC0303
    upload(c, [note('Patient name: PATEL, Maya R.\nPatient ID: WIC-4471')])    # same person, walk-in clinic
    page = c.get('/profile').get_data(as_text=True)
    assert '<dt>Patient ID</dt><dd>PC0303, WIC-4471</dd>' in page and '<dt>Name</dt><dd>Maya Patel</dd>' in page
