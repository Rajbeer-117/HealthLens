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
    c.post('/register', data={'username': name, 'email': f'{name}@example.com', 'password': 'longpassword1', 'confirm': 'longpassword1'})
    c.post('/login', data={'identity': name, 'password': 'longpassword1'})
    return c


@pytest.fixture()
def client(env):
    return make_client()


def csrf(c):
    c.get('/documents')
    with c.session_transaction() as s:
        return s['upload_csrf']


def upload(c, files, token=None):
    data = {'csrf': token or csrf(c), 'files': [(io.BytesIO(raw), name) for name, raw in files]}
    return c.post('/documents/upload', data=data, content_type='multipart/form-data', follow_redirects=True)


def doc_rows():
    conn = sqlite3.connect(app_module.DB)
    conn.row_factory = sqlite3.Row
    return conn.execute('SELECT * FROM documents ORDER BY id').fetchall()


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


def test_summary_shows_flags_readings_and_charts(client):
    html = client.get('/summary?patient=PC0101').get_data(as_text=True)
    assert 'Recent flags' in html and 'warning-card' in html
    assert 'Latest values' in html and 'Blood pressure' in html
    assert 'id="healthChart"' in html and 'id="chartData"' in html
    assert 'anomalies' not in html                                           # unusual-reading markers are not sent to the page


def test_profile_has_account_and_medical_history(client):
    html = client.get('/profile?patient=PC0101').get_data(as_text=True)
    assert 'tester@example.com' in html and 'Member since' in html
    assert 'Borderline glycemic markers' in html                             # Medical History sheet entry
    assert 'Metformin' in html and 'Encounters' not in html


def test_active_patient_is_shared_across_pages_and_chat(client):
    client.get('/summary?patient=PC0303')
    html = client.get('/profile').get_data(as_text=True)
    assert 'PC0303' in html and '"patient": "PC0303"' in html.replace('&#34;', '"')


def test_empty_uploaded_source_shows_guidance(client):
    html = client.get('/summary?source=uploaded').get_data(as_text=True)
    assert 'No patient records to show yet' in html and '/documents' in html
    assert client.get('/profile?source=uploaded').status_code == 200


# ------------------------------------------------------------- uploads
def test_upload_workbook_zip_and_pdf(client):
    r = upload(client, [('PC0101 (1).xlsx', workbook_bytes('PC0101')), ('batch.zip', zip_of('PC0002', 'PC0003')), ('scan.pdf', PDF)])
    html = r.get_data(as_text=True)
    assert '3 patient workbooks imported and analysed' in html and '1 document stored' in html
    rows = {(x['original_name'], x['kind'], x['origin']) for x in doc_rows()}
    assert ('PC0101_1.xlsx', 'patient', 'Computer') in rows                   # messy browser name is cleaned
    assert ('PC0002.xlsx', 'patient', 'Computer · ZIP') in rows
    assert ('scan.pdf', 'document', 'Computer') in rows
    # uploaded patients are now analysable and become the active patient
    s = client.get('/summary').get_data(as_text=True)
    assert 'PC0101_1' in s and 'Recent flags' in s
    assert 'PC0003' in client.get('/api/patients?source=uploaded').get_json()['patients']


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


def test_workbook_without_patient_profile_is_rejected(client):
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame({'a': [1]}).to_excel(buf, sheet_name='Other', index=False)
    html = upload(client, [('random.xlsx', buf.getvalue())]).get_data(as_text=True)
    assert 'Patient Profile sheet' in html and doc_rows() == []


def test_upload_requires_csrf_token(client):
    html = upload(client, [('a.pdf', PDF)], token='wrong').get_data(as_text=True)
    assert 'verification failed' in html and doc_rows() == []


def test_too_many_files_in_one_request(client):
    html = upload(client, [(f'f{i}.pdf', PDF) for i in range(21)]).get_data(as_text=True)
    assert 'at most 20 files' in html and doc_rows() == []


def test_uploaded_name_is_never_used_as_a_path(client, env):
    upload(client, [('../../evil.pdf', PDF), ('..\\..\\evil2.pdf', PDF)])
    rows = doc_rows()
    assert [r['original_name'] for r in rows] == ['evil.pdf', 'evil2.pdf']
    for r in rows:
        assert '/' not in r['stored_name'] and r['stored_name'].endswith('.pdf') and r['stored_name'] != r['original_name']
    assert not list(env.glob('evil*'))                                       # nothing escaped the uploads folder
    assert len(list((env / 'uploads').rglob('*.pdf'))) == 2


def test_zip_entry_larger_than_limit_is_rejected(client, monkeypatch):
    monkeypatch.setattr(app_module, 'MAX_BOOK', 1000)
    html = upload(client, [('big.zip', zip_of('PC0002'))]).get_data(as_text=True)
    assert 'exceeds the 6 MB limit' in html and doc_rows() == []


def test_reuploading_a_patient_replaces_it(client):
    upload(client, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    upload(client, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    assert [r['patient_id'] for r in doc_rows()] == ['PC0002']


# ------------------------------------------------------------- download / delete / isolation
def test_download_and_delete(client):
    upload(client, [('scan.pdf', PDF)])
    doc = doc_rows()[0]
    r = client.get(f'/documents/{doc["id"]}/download')
    assert r.status_code == 200 and r.data == PDF
    assert 'attachment' in r.headers['Content-Disposition'] and 'scan.pdf' in r.headers['Content-Disposition']
    assert r.headers['X-Content-Type-Options'] == 'nosniff'
    assert client.post(f'/documents/{doc["id"]}/delete', data={'csrf': 'bad'}, follow_redirects=True).status_code == 200
    assert len(doc_rows()) == 1                                              # bad token: nothing deleted
    r = client.post(f'/documents/{doc["id"]}/delete', data={'csrf': csrf(client)}, follow_redirects=True)
    assert 'Deleted scan.pdf' in r.get_data(as_text=True) and doc_rows() == []
    assert not list(app_module.UPLOADS.rglob('*.pdf'))


def test_deleting_a_patient_removes_it_from_the_patient_list(client):
    upload(client, [('PC0002.xlsx', workbook_bytes('PC0002'))])
    doc = doc_rows()[0]
    client.post(f'/documents/{doc["id"]}/delete', data={'csrf': csrf(client)})
    assert client.get('/api/patients?source=uploaded').get_json()['patients'] == []
    assert client.get('/summary?source=uploaded').status_code == 200         # falls back to the empty state, no crash


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
    assert '1 patient workbook imported' in drive_import(client)
    row = doc_rows()[0]
    assert (row['patient_id'], row['origin']) == ('PC0005', 'Google Drive')


def test_drive_import_zip_with_utf8_filename(client, monkeypatch):
    fake_drive(monkeypatch, FakeResponse(zip_of('PC0006', 'PC0007'), disposition="attachment; filename=\"x.zip\"; filename*=UTF-8''Patients%20%C3%A9t%C3%A9.zip"))
    assert '2 patient workbooks imported' in drive_import(client)
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
def test_chat_answers_and_screens_unsafe_questions(client):
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
    body = {'source': 'uploaded', 'question': 'How is my sleep?'}
    assert client.post('/api/patients/PC0002/ask', headers=h, json=body).status_code == 200
    other = make_client('mallory')
    assert other.post('/api/patients/PC0002/ask', headers={'X-CSRF-Token': csrf(other)}, json=body).status_code == 404
