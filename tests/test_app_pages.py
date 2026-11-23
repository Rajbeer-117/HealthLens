"""Tests for the Summary / Profile / Documents pages, file ingestion, Google Drive import and the chatbot API.
Run:  python -m pytest -q"""
import io
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ['HEALTHLENS_LLM_PROVIDER'] = 'offline'
os.environ['HEALTHLENS_SECRET_KEY'] = os.environ.get('HEALTHLENS_SECRET_KEY') or 'test-secret'   # keeps tests away from the real database at import time

import app as app_module  # noqa: E402


def workbook_bytes(pid='PC0101'):
    with zipfile.ZipFile(ROOT / 'patient_data.zip') as z:
        return z.read(next(n for n in z.namelist() if n.endswith(f'/{pid}.xlsx')))


def zip_of(*pids, nested='Project'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for pid in pids:
            z.writestr(f'{nested}/{pid}.xlsx', workbook_bytes(pid))
    return buf.getvalue()


PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n'


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'DB', tmp_path / 'users.sqlite3')
    monkeypatch.setattr(app_module, 'UPLOADS', tmp_path / 'uploads')
    app_module.app.config['TESTING'] = True
    return tmp_path



def make_client(name='tester'):
    c = app_module.app.test_client()

    password = 'HealthLens@Test123'

    # Register a test user
    register_response = c.post(
        '/register',
        data={
            'username': name,
            'email': f'{name}@example.com',
            'password': password,
            'confirm': password
        },
        follow_redirects=False
    )

    assert register_response.status_code == 302, (
        f"Registration failed for {name}: "
        f"{register_response.get_data(as_text=True)[:500]}"
    )

    # Log in with the registered account
    login_response = c.post(
        '/login',
        data={
            'identity': name,
            'password': password
        },
        follow_redirects=False
    )

    assert login_response.status_code == 302, (
        f"Login failed for {name}: "
        f"{login_response.get_data(as_text=True)[:500]}"
    )

    # Verify that the test user is authenticated
    profile_response = c.get('/profile')

    assert profile_response.status_code == 200, (
        f"Test user {name} is not authenticated."
    )

    return c



@pytest.fixture()
def client(env):
    return make_client()


@pytest.fixture()
def patient_client(client):
    """A signed-in team member who has uploaded two patient workbooks (there is no built-in demo data any more;
    a patient account holds one record, a team account may hold several to test the dataset)."""
    team()
    upload(client, [('PC0101.xlsx', workbook_bytes('PC0101')), ('PC0303.xlsx', workbook_bytes('PC0303'))])
    client.get('/summary?patient=PC0101')
    return client


def csrf(c):
    c.get('/documents')
    with c.session_transaction() as s:
        return s['upload_csrf']


def team(name='tester'):
    """Make the account a team (admin) account: only team accounts may hold several patient records."""
    with app_module.db() as conn:
        conn.execute("INSERT INTO team_admins(user_id, granted_at) SELECT id, '2026-01-01' FROM users WHERE username=? "
                     "ON CONFLICT(user_id) DO NOTHING", (name,))


def upload(c, files, token=None):
    data = {'csrf': token or csrf(c), 'files': [(io.BytesIO(raw), name) for name, raw in files]}
    return c.post('/documents/upload', data=data, content_type='multipart/form-data', follow_redirects=True)


def doc_rows():
    with app_module.db() as conn:
        return conn.execute('SELECT * FROM documents ORDER BY id').fetchall()


def rows(sql, *args):
    with app_module.db() as conn:
        return conn.execute(sql, args).fetchall()


# ------------------------------------------------------------- pages
REMOVED = ['Windsor', 'GROUP 7', 'Synthetic longitudinal', 'AI Integration', 'Evidence Register', 'Evidence items', 'Data Quality',
           'Data Preview', 'Diagnosis History', 'Anomalies', 'AI Health Summary', 'LLM in use', 'Scenario Patterns']


@pytest.mark.parametrize('url', ['/summary', '/profile', '/documents'])
def test_pages_render_without_removed_content(client, url):
    r = client.get(url)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert [w for w in REMOVED if w in html] == []
    assert 'id="chatPanel"' in html and 'id="chatLauncher"' in html          # chatbot on every page
    for label in ('Summary', 'Profile', 'Documents'):
        assert f'>{label}</a>' in html


@pytest.mark.parametrize('url', ['/summary', '/profile', '/documents'])
def test_pages_require_login(env, url):
    r = app_module.app.test_client().get(url)
    assert r.status_code == 302 and '/login' in r.headers['Location']


def test_old_dashboard_url_redirects(client):
    r = client.get('/dashboard?patient=PC0303')
    assert r.status_code == 302 and r.headers['Location'].endswith('/summary?patient=PC0303')


def test_removed_ai_pages_and_endpoints_are_gone(client):
    assert client.get('/ai').status_code == 404
    token = csrf(client)
    for path in ('/api/patients/PC0101/ai-summary', '/api/llm/test'):
        assert client.post(path, headers={'X-CSRF-Token': token}, json={}).status_code == 404
    assert client.get('/api/llm/status').status_code == 404


def test_summary_shows_flags_readings_and_charts(patient_client):
    client = patient_client
    html = client.get('/summary?patient=PC0101').get_data(as_text=True)
    assert 'Recent flags' in html and 'warning-card' in html
    assert 'Latest values' in html and 'Blood pressure' in html
    assert 'id="healthChart"' in html and 'id="chartData"' in html
    assert 'anomalies' not in html                                           # unusual-reading markers are not sent to the page


def test_profile_has_account_and_medical_history(patient_client):
    client = patient_client
    html = client.get('/profile?patient=PC0101').get_data(as_text=True)
    assert 'tester@example.com' in html and 'Member since' in html
    assert 'Borderline glycemic markers' in html                             # Medical History sheet entry
    assert 'Metformin' in html and 'Encounters' not in html


def test_active_patient_is_shared_across_pages_and_chat(patient_client):
    client = patient_client
    client.get('/summary?patient=PC0303')
    html = client.get('/profile').get_data(as_text=True)
    assert 'PC0303' in html and '"patient": "PC0303"' in html.replace('&#34;', '"')


def test_new_user_sees_welcome_message_not_data(client):
    html = client.get('/summary').get_data(as_text=True)
    assert 'Welcome to HealthLens, tester!' in html
    assert 'No documents uploaded yet.' in html and '/documents' in html
    assert 'Recent flags' not in html and 'warning-card' not in html and 'id="chartData"' not in html
    assert client.get('/profile').status_code == 200


# ------------------------------------------------------------- uploads
def test_upload_workbook_zip_and_pdf(client):
    team()                                                                   # several patients: team account
    r = upload(client, [('PC0101 (1).xlsx', workbook_bytes('PC0101')), ('batch.zip', zip_of('PC0002', 'PC0003')), ('scan.pdf', PDF)])
    html = r.get_data(as_text=True)
    assert '3 documents analysed' in html and '1 document stored' in html
    rows = {(x['original_name'], x['kind'], x['origin']) for x in doc_rows()}
    assert ('PC0101_1.xlsx', 'patient', 'Computer') in rows                   # messy browser name is cleaned
    assert ('PC0002.xlsx', 'patient', 'Computer · ZIP') in rows
    assert ('scan.pdf', 'document', 'Computer') in rows
    # the patient is the Patient ID written inside the workbook, not the file name
    assert {x['patient_id'] for x in doc_rows() if x['kind'] == 'patient'} == {'PC0101', 'PC0002', 'PC0003'}
    # uploaded patients are now analysable and become the active patient
    s = client.get('/summary').get_data(as_text=True)
    assert 'PC0101' in s and 'PC0101_1' not in s and 'Recent flags' in s
    assert 'PC0003' in client.get('/api/patients').get_json()['patients']


def test_one_bad_file_does_not_block_the_others(client):
    r = upload(client, [('fake.xlsx', b'not a workbook'), ('tool.exe', b'MZ'), ('ok.pdf', PDF), ('empty.zip', zip_of())])
    html = r.get_data(as_text=True)
    assert '1 document stored' in html
    assert 'fake.xlsx: The file is not a readable XLSX workbook' in html
    assert 'tool.exe: unsupported file type' in html
    assert 'ZIP must contain' in html
    assert [x['original_name'] for x in doc_rows()] == ['ok.pdf']


def test_content_must_match_extension(client):
    html = upload(client, [('x.pdf', b'hello'), ('x.png', b'hello'), ('x.docx', b'hello')]).get_data(as_text=True)
    assert html.count('does not look like a valid') == 3 and doc_rows() == []


def test_workbook_without_health_readings_is_rejected(client):
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame({'a': [1]}).to_excel(buf, sheet_name='Other', index=False)
    html = upload(client, [('random.xlsx', buf.getvalue())]).get_data(as_text=True)
    assert 'no health readings were found' in html and doc_rows() == []


def test_upload_requires_csrf_token(client):
    html = upload(client, [('a.pdf', PDF)], token='wrong').get_data(as_text=True)
    assert 'verification failed' in html and doc_rows() == []


def test_too_many_files_in_one_request(client):
    html = upload(client, [(f'f{i}.pdf', PDF) for i in range(21)]).get_data(as_text=True)
    assert 'at most 20 files' in html and doc_rows() == []


def test_uploaded_name_is_never_used_as_a_path(client, env):
    other = PDF + b'% second file\n'                                         # identical files are only stored once
    upload(client, [('../../evil.pdf', PDF), ('..\\..\\evil2.pdf', other)])
    rows = doc_rows()
    assert [r['original_name'] for r in rows] == ['evil.pdf', 'evil2.pdf']
    for r in rows:
        assert '/' not in r['stored_name'] and r['stored_name'].endswith('.pdf') and r['stored_name'] != r['original_name']
    assert not list(env.glob('evil*'))                                       # nothing written outside the database
    assert not (env / 'uploads').exists()                                    # files are stored in the database, not on disk
    assert [r['content'] for r in rows] == [PDF, other]


def test_zip_entry_larger_than_limit_is_rejected(client, monkeypatch):
    monkeypatch.setattr(app_module, 'MAX_BOOK', 1000)
    html = upload(client, [('big.zip', zip_of('PC0002'))]).get_data(as_text=True)
    assert 'exceeds the 6 MB limit' in html and doc_rows() == []


def test_uploading_the_same_workbook_again_is_not_stored_twice(client):
    upload(client, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    html = upload(client, [('PC0002 copy.xlsx', workbook_bytes('PC0002'))]).get_data(as_text=True)
    assert [r['patient_id'] for r in doc_rows()] == ['tester']             # the account's record
    assert 'PC0002_copy.xlsx: already uploaded.' in html


# ------------------------------------------------------------- download / delete / isolation

def test_download_and_delete(client):
    # Upload a test PDF
    upload(client, [('scan.pdf', PDF)])
    doc = doc_rows()[0]

    # Download the PDF and buffer the response
    r = client.get(
        f'/documents/{doc["id"]}/download',
        buffered=True
    )

    # Verify the downloaded file
    assert r.status_code == 200
    assert r.data == PDF
    assert 'attachment' in r.headers['Content-Disposition']
    assert 'scan.pdf' in r.headers['Content-Disposition']
    assert r.headers['X-Content-Type-Options'] == 'nosniff'

    # Close the response to release the file handle on Windows
    r.close()

    # Test deletion with an invalid CSRF token
    r = client.post(
        f'/documents/{doc["id"]}/delete',
        data={'csrf': 'bad'},
        follow_redirects=True
    )

    assert r.status_code == 200
    assert len(doc_rows()) == 1  # File must not be deleted

    # Test deletion with a valid CSRF token
    r = client.post(
        f'/documents/{doc["id"]}/delete',
        data={'csrf': csrf(client)},
        follow_redirects=True
    )

    # Verify successful deletion
    assert 'Deleted scan.pdf' in r.get_data(as_text=True)
    assert doc_rows() == []

    # Verify the PDF was removed from storage
    assert not list(app_module.UPLOADS.rglob('*.pdf'))



def test_deleting_a_patient_removes_it_from_the_patient_list(client):
    upload(client, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    doc = doc_rows()[0]
    client.post(f'/documents/{doc["id"]}/delete', data={'csrf': csrf(client)})
    assert client.get('/api/patients').get_json()['patients'] == []
    assert 'Welcome to HealthLens' in client.get('/summary').get_data(as_text=True)   # back to the empty state, no crash


def test_other_users_cannot_download_or_delete_my_files(client):
    upload(client, [('private.pdf', PDF)])
    doc = doc_rows()[0]
    other = make_client('mallory')
    assert other.get(f'/documents/{doc["id"]}/download').status_code == 404
    assert other.post(f'/documents/{doc["id"]}/delete', data={'csrf': csrf(other)}).status_code == 404
    assert 'private.pdf' not in other.get('/documents').get_data(as_text=True)
    assert len(doc_rows()) == 1


def test_workbooks_already_on_disk_are_listed(client, env):
    folder = env / 'uploads' / '1'
    folder.mkdir(parents=True)
    (folder / 'PC0777.xlsx').write_bytes(workbook_bytes('PC0101'))           # as left by the previous version of the app
    assert 'PC0777.xlsx' in client.get('/documents').get_data(as_text=True)


# ------------------------------------------------------------- Google Drive
@pytest.mark.parametrize('link,expected', [
    ('https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing', 'https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlMnOp'),
    ('https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp', 'https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlMnOp'),
    ('https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOp&export=download', 'https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlMnOp'),
    ('https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOp/edit#gid=0', 'https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOp/export?format=xlsx'),
])
def test_drive_links_become_direct_download_urls(link, expected):
    assert app_module.drive_download_url(link)[0] == expected


@pytest.mark.parametrize('link', [
    'https://evil.example.com/file/d/1AbCdEfGhIjKlMnOp/view', 'https://drive.google.com.evil.com/file/d/1AbCdEfGhIjKlMnOp/view',
    'http://169.254.169.254/latest/meta-data', 'file:///etc/passwd', 'javascript:alert(1)', 'not a link', '',
    'https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp', 'https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit',
    'https://drive.google.com/file/d/short/view'])
def test_non_drive_or_unsupported_links_are_refused(link):
    with pytest.raises(ValueError):
        app_module.drive_download_url(link)


class FakeResponse:
    def __init__(self, body, ctype='application/octet-stream', disposition=''):
        from email.message import Message
        self._body = body
        self.headers = Message()
        self.headers['Content-Type'] = ctype
        if disposition:
            self.headers['Content-Disposition'] = disposition

    def read(self, n=-1): return self._body if n < 0 else self._body[:n]
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake_drive(monkeypatch, response=None, error=None):
    class Opener:
        def open(self, req, timeout=None):
            assert req.full_url.startswith(('https://drive.google.com/', 'https://docs.google.com/'))
            if error: raise error
            return response
    monkeypatch.setattr(app_module.urllib.request, 'build_opener', lambda *h: Opener())


LINK = 'https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view'


def drive_import(c, link=LINK):
    return c.post('/documents/drive', data={'csrf': csrf(c), 'link': link}, follow_redirects=True).get_data(as_text=True)


def test_drive_import_workbook(client, monkeypatch):
    fake_drive(monkeypatch, FakeResponse(workbook_bytes('PC0005'), disposition='attachment; filename="PC0005.xlsx"'))
    assert '1 document analysed' in drive_import(client)
    row = doc_rows()[0]
    assert (row['patient_id'], row['origin']) == ('tester', 'Google Drive')
    assert [r['value'] for r in rows('SELECT value FROM patient_identifiers')] == ['PC0005']


def test_drive_import_zip_with_utf8_filename(client, monkeypatch):
    team()                                                                   # several patients: team account
    fake_drive(monkeypatch, FakeResponse(zip_of('PC0006', 'PC0007'), disposition="attachment; filename=\"x.zip\"; filename*=UTF-8''Patients%20%C3%A9t%C3%A9.zip"))
    assert '2 documents analysed' in drive_import(client)
    assert {r['origin'] for r in doc_rows()} == {'Google Drive · ZIP'}


def test_drive_import_without_filename_sniffs_type(client, monkeypatch):
    fake_drive(monkeypatch, FakeResponse(PDF))
    assert '1 document stored' in drive_import(client)
    assert doc_rows()[0]['original_name'].endswith('.pdf')


def test_drive_private_file_gives_helpful_error(client, monkeypatch):
    fake_drive(monkeypatch, response=FakeResponse(b'<!doctype html><html>Sign in</html>', ctype='text/html'))
    assert 'Anyone with the link' in drive_import(client) and doc_rows() == []
    fake_drive(monkeypatch, error=urllib.error.HTTPError(LINK, 403, 'Forbidden', {}, None))
    assert 'refused the download' in drive_import(client)
    fake_drive(monkeypatch, error=urllib.error.URLError('offline'))
    assert 'Could not reach Google Drive' in drive_import(client)


def test_drive_file_over_size_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, 'MAX_REMOTE', 50)
    fake_drive(monkeypatch, FakeResponse(PDF * 10, disposition='attachment; filename="big.pdf"'))
    assert 'larger than 60 MB' in drive_import(client) and doc_rows() == []


def test_drive_bad_link_message_and_csrf(client):
    assert 'Paste a Google Drive' in drive_import(client, 'https://example.com/x')
    r = client.post('/documents/drive', data={'csrf': 'bad', 'link': LINK}, follow_redirects=True)
    assert 'verification failed' in r.get_data(as_text=True)


def test_redirects_only_followed_to_google_hosts():
    h = app_module._GoogleOnlyRedirects()
    req = urllib.request.Request('https://drive.google.com/uc?id=x')
    ok = h.redirect_request(req, None, 303, 'See Other', {}, 'https://drive.usercontent.google.com/download?id=x')
    assert ok is not None
    for bad in ('https://evil.example.com/x', 'http://drive.google.com/x', 'https://google.com.evil.com/x', 'https://169.254.169.254/'):
        with pytest.raises(urllib.error.HTTPError):
            h.redirect_request(req, None, 302, 'Found', {}, bad)


# ------------------------------------------------------------- medical history reader
def test_medical_history_reader_handles_other_layouts():
    import pandas as pd
    book = {'Diagnoses': pd.DataFrame({'Diagnosis Date': [45245], 'Diagnosis': ['Hypothyroidism'], 'Status': ['Active'], 'Dr': ['Lee']}),
            'Medical History': pd.DataFrame({'Date': [45728, 45245], 'Finding / Event': ['B', 'A'], 'Notes': ['n1', None]}),
            'Lab Results': pd.DataFrame({'Date': [1]})}
    out = app_module.medical_history(book)
    titles = [e['title'] for e in out]
    assert set(titles) == {'Hypothyroidism', 'A', 'B'} and 'Lab Results' not in titles
    assert next(e for e in out if e['title'] == 'Hypothyroidism')['extra'] == 'Dr: Lee'
    assert out[0]['date'] >= out[-1]['date']                                  # newest first


def test_profile_fields_skip_blanks_and_format_numbers():
    import numpy as np
    assert app_module.profile_fields({'Age': 52.0, 'Sex': 'Male', 'Blood': np.nan, 'X': None}) == [('Age', '52'), ('Sex', 'Male')]


# ------------------------------------------------------------- chatbot API
def test_chat_answers_and_screens_unsafe_questions(patient_client):
    client = patient_client
    token = csrf(client)
    h = {'X-CSRF-Token': token}
    a = client.post('/api/patients/PC0101/ask', headers=h, json={'question': 'How has my blood pressure changed?'}).get_json()
    assert a['answer'] and a['mode'] == 'offline'
    d = client.post('/api/patients/PC0101/ask', headers=h, json={'question': 'Do I have diabetes?'}).get_json()
    assert d['mode'] == 'screened' and 'cannot diagnose' in d['answer']
    assert client.post('/api/patients/PC0101/ask', headers=h, json={'question': 'hi'}).status_code == 400
    assert client.post('/api/patients/PC0101/ask', headers={'X-CSRF-Token': 'nope'}, json={'question': 'How is my sleep?'}).status_code == 400
    assert client.post('/api/patients/NOPE/ask', headers=h, json={'question': 'How is my sleep?'}).status_code == 404


def test_chat_is_scoped_to_the_signed_in_users_uploads(client):
    upload(client, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    h = {'X-CSRF-Token': csrf(client)}
    body = {'question': 'How is my sleep?'}
    assert client.post('/api/patients/tester/ask', headers=h, json=body).status_code == 200
    other = make_client('mallory')
    assert other.post('/api/patients/tester/ask', headers={'X-CSRF-Token': csrf(other)}, json=body).status_code == 404


# ------------------------------------------------------------- category panel + 3-month data span

def test_summary_has_three_category_panel(patient_client):
    client = patient_client
    html = client.get('/summary?patient=PC0101').get_data(as_text=True)
    assert 'category-panel' in html
    for name in ('Metabolic', 'Cardiovascular', "Women&#39;s health (PCOS)"):
        assert name in html


def test_default_span_is_last_three_months(patient_client):
    client = patient_client
    html = client.get('/summary?patient=PC0101').get_data(as_text=True)
    assert 'Last 3 months' in html and '2026-06-01 to 2026-08-31' in html


def test_full_record_span_switch(patient_client):
    client = patient_client
    html = client.get('/summary?patient=PC0101&span=all').get_data(as_text=True)
    assert '2025-09-01 to 2026-08-31' in html
    html = client.get('/summary?patient=PC0101').get_data(as_text=True)   # choice is remembered
    assert '2025-09-01 to 2026-08-31' in html


def test_category_filters_readings_and_charts(patient_client):
    client = patient_client
    import json, re
    html = client.get('/summary?patient=PC0101&category=cardiovascular').get_data(as_text=True)
    assert 'id="category"' in html and 'Blood pressure' in html
    charts = json.loads(re.search(r'id="chartData">(.*?)</script>', html, re.S).group(1))['charts']
    assert 'sbp' in charts and 'glucose' not in charts
    html = client.get('/summary?patient=PC0101&category=metabolic').get_data(as_text=True)
    charts = json.loads(re.search(r'id="chartData">(.*?)</script>', html, re.S).group(1))['charts']
    assert 'glucose' in charts and 'sbp' not in charts


def test_pcos_category_not_applicable_for_male(patient_client):
    client = patient_client
    html = client.get('/summary?patient=PC0101&category=pcos').get_data(as_text=True)
    assert 'Not applicable' in html


def test_engine_three_month_trim():
    import io, zipfile
    import pandas as pd
    from healthlens import analyze_book
    z = zipfile.ZipFile(ROOT / 'patient_data.zip')
    name = next(n for n in z.namelist() if n.endswith('/PC0101.xlsx'))
    book = pd.read_excel(io.BytesIO(z.read(name)), sheet_name=None, engine='openpyxl')
    a = analyze_book(book, 3)
    assert a['span'] == {'months': 3, 'start': '2026-06-01', 'end': '2026-08-31'}
    assert all(p['date'] >= '2026-06-01' for c in a['charts'].values() for p in c['points'])


# ------------------------------------------------------------- documents uploaded one by one (backend)
from pathlib import Path  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / 'sample_documents'


def sample(name):
    return (name, (SAMPLES / name).read_bytes())


def records():
    with app_module.db() as conn:
        return conn.execute('SELECT * FROM health_records ORDER BY id').fetchall()


def test_a_single_lab_report_creates_the_record_and_its_readings(client):
    html = upload(client, [sample('01_lab_report_2025-09-25.pdf')]).get_data(as_text=True)
    assert '01_lab_report_2025-09-25.pdf: 15 lab results.' in html
    doc = doc_rows()[0]
    assert (doc['kind'], doc['patient_id'], doc['doc_type'], doc['readings']) == ('patient', 'tester', 'report', 15)
    rows_ = records()
    assert {r['sheet'] for r in rows_} == {'Lab Results', 'Patient Profile'} and all(r['document_id'] == doc['id'] for r in rows_)
    s = client.get('/summary').get_data(as_text=True)
    assert 'PC0303' in s and 'risk' in s and 'missing' in s                # a risk level from one document


def test_documents_without_patient_id_join_the_accounts_record(client):
    upload(client, [sample('01_lab_report_2025-09-25.pdf')])
    html = upload(client, [sample('03_home_blood_pressure_monitor.csv')]).get_data(as_text=True)
    assert 'Your record' in html and 'Patient ID PC0303' in html and '2 documents' in html
    assert {d['patient_id'] for d in doc_rows()} == {'tester'}
    assert sum(r['sheet'] == 'Vitals Daily' for r in records()) == 365


NO_ID_BP = ('bp.csv', b'Date,Systolic (mmHg),Diastolic (mmHg)\n2026-01-02,121,79\n2026-01-03,118,77\n')   # device export without an ID


def test_first_document_without_any_id_starts_a_record_named_after_the_account(client):
    upload(client, [NO_ID_BP])
    assert doc_rows()[0]['patient_id'] == 'tester'


def test_document_without_id_is_refused_when_the_account_has_several_patients(client):
    team()
    upload(client, [('PC0002.xlsx', workbook_bytes('PC0002')), ('PC0003.xlsx', workbook_bytes('PC0003'))])
    html = upload(client, [NO_ID_BP]).get_data(as_text=True)
    assert 'cannot tell whose record it is' in html and len(doc_rows()) == 2


def test_deleting_a_document_removes_only_its_readings(client):
    upload(client, [sample('01_lab_report_2025-09-25.pdf')])
    upload(client, [sample('08_lab_results_2026-01-08.csv')])
    first, second = sorted(doc_rows(), key=lambda d: d['id'])
    csrf_token = csrf(client)
    html = client.post(f'/documents/{second["id"]}/delete', data={'csrf': csrf_token}, follow_redirects=True).get_data(as_text=True)
    assert "Your summary now uses the remaining 1 upload" in html
    assert {r['document_id'] for r in records()} == {first['id']}


def test_a_document_with_no_values_is_stored_only_with_the_reason(client):
    html = upload(client, [('letter.txt', b'Dear patient, see you next week.')]).get_data(as_text=True)
    d = doc_rows()[0]
    assert d['kind'] == 'document' and 'stored only' in d['note'] and records() == []
    assert 'No health values found; stored only.' in html


def test_old_uploads_get_their_readings_once(client):
    """Workbooks stored by the previous version (no rows in health_records yet) are read once, automatically."""
    upload(client, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    with app_module.db() as conn:
        conn.execute('DELETE FROM health_records')
        conn.execute('UPDATE documents SET readings=NULL, doc_type=NULL')
    assert 'PC0002' in client.get('/summary').get_data(as_text=True)
    n = len(records())
    client.get('/summary')
    assert n > 0 and len(records()) == n and doc_rows()[0]['readings'] > 0


def test_new_tables_exist_for_document_by_document_records(env):
    with app_module.db() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        doc_cols = {r[1] for r in conn.execute('PRAGMA table_info(documents)')}
    assert {'patients', 'health_records'} <= tables and {'doc_type', 'readings', 'note', 'sha256'} <= doc_cols


def test_patient_account_holds_one_record_and_refuses_another_patient(client):
    upload(client, [sample('01_lab_report_2025-09-25.pdf')])                              # Maya Patel, PC0303
    html = upload(client, [('PC0002.xlsx', workbook_bytes('PC0002'))]).get_data(as_text=True)
    assert 'PC0002.xlsx' in html and 'Not added.' in html
    assert [d['kind'] for d in doc_rows()] == ['patient']                                # nothing from it is stored
    assert 'name="patient"' not in client.get('/summary').get_data(as_text=True)          # no patient selector
    html = upload(client, [sample('03_home_blood_pressure_monitor.csv')]).get_data(as_text=True)   # known ID: joins
    assert 'Your record' in html and '2 documents' in html


def test_a_zip_with_several_patients_adds_the_first_and_refuses_the_other(client):
    html = upload(client, [('batch.zip', zip_of('PC0002', 'PC0003'))]).get_data(as_text=True)
    assert {d['original_name']: d['kind'] for d in doc_rows()} == {'PC0002.xlsx': 'patient'}
    assert 'PC0003.xlsx' in html and 'Not added.' in html


# ---------------------------------------------------------------- one summary per report, plus the combined summary
def report_ids():
    return {d['original_name']: d['id'] for d in doc_rows()}


def test_each_report_has_its_own_summary_and_the_summary_page_combines_them(client):
    upload(client, [sample('01_lab_report_2025-09-25.pdf')])
    upload(client, [sample('03_home_blood_pressure_monitor.csv')])
    ids = report_ids()
    overall = client.get('/summary?category=cardiovascular').get_data(as_text=True)
    assert 'All 2 reports combined' in overall
    page = client.get(f'/documents/{ids["01_lab_report_2025-09-25.pdf"]}/summary?category=cardiovascular')
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and 'REPORT SUMMARY' in html and '01_lab_report_2025-09-25.pdf' in html
    assert 'from this report only' in html and 'Overall summary (2 reports)' in html
    assert '/100' in html and 'key inputs missing: Blood pressure' in html        # the lab report has no blood pressure
    assert 'key inputs missing: Blood pressure' not in overall                     # the combined summary has it
    assert f'/documents/{ids["01_lab_report_2025-09-25.pdf"]}/summary?category=metabolic' in html   # categories stay on the report
    assert '2025-09-25' in html and 'Last 3 months' not in html                   # the report's own date, no span switch


def test_report_summary_is_only_for_the_owner_and_for_documents_with_values(client):
    upload(client, [sample('01_lab_report_2025-09-25.pdf'), ('scan.pdf', PDF)])
    ids = report_ids()
    stored = client.get(f'/documents/{ids["scan.pdf"]}/summary', follow_redirects=True).get_data(as_text=True)
    assert 'scan.pdf has no health values to summarise.' in stored
    other = make_client('someone')
    assert other.get(f'/documents/{ids["01_lab_report_2025-09-25.pdf"]}/summary').status_code == 404


def test_documents_page_links_to_each_report_summary(client):
    upload(client, [sample('01_lab_report_2025-09-25.pdf')])
    html = client.get('/documents').get_data(as_text=True)
    assert f'/documents/{report_ids()["01_lab_report_2025-09-25.pdf"]}/summary' in html and '>View<' in html


def test_womens_health_without_cycle_data_is_scored_not_called_not_applicable(client):
    """A female patient who has uploaded only a lab report: the trained model scores from the labs and cycle data is
    named as missing (Not applicable is only for male records)."""
    upload(client, [sample('01_lab_report_2025-09-25.pdf')])
    html = client.get('/summary?category=pcos').get_data(as_text=True)
    assert 'Not applicable' not in html and 'No cycle data uploaded yet.' in html
    assert '/100' in html and 'key inputs missing: Cycle regularity' in html and 'so the prediction score may not be accurate' in html
