"""Pages: sign-up without the rules list, welcome message for new users, patient selector, Review button on flags,
no admin trace for patients, and the System dashboard content.
Owner: Haya Shah (Frontend & Validation Lead). Shared set-up (database per test, sign-up/sign-in helpers) is in tests/conftest.py.
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


def test_signup_page_does_not_list_password_rules(env):
    html = app_module.app.test_client().get('/register').get_data(as_text=True)
    for text in ('password-checklist', 'Password must contain', 'One uppercase letter', 'At least 10 characters',
                 'One special character', 'minlength="10"', 'validatePassword'):
        assert text not in html
    assert 'name="password"' in html and 'name="confirm"' in html


def test_new_user_has_no_demo_data(env):
    c = new_browser(make_account=True)
    html = c.get('/summary').get_data(as_text=True)
    assert 'Welcome to HealthLens, tester!' in html and 'Upload documents' in html
    for text in ('PC0101', 'Demo patients', 'demo', 'Recent flags', 'Latest values', 'id="chartData"'):
        assert text not in html
    assert c.get('/api/patients').get_json() == {'patients': []}
    assert c.get('/api/patients/PC0101/analysis').status_code == 404       # demo patients are not reachable
    assert c.get('/summary?source=demo').get_data(as_text=True).count('Welcome to HealthLens') == 1
    h = {'X-CSRF-Token': csrf(c)}
    assert c.post('/api/patients/PC0101/ask', headers=h, json={'question': 'How is my sleep?', 'source': 'demo'}).status_code == 404


def test_profile_and_chat_for_new_user_point_to_documents(env):
    c = new_browser(make_account=True)
    html = c.get('/profile').get_data(as_text=True)
    assert 'No documents uploaded yet.' in html and 'Built-in demo' not in html
    data = json.loads(re.search(r'id="chatData">(.*?)</script>', html, re.S).group(1))
    assert data['patient'] == '' and 'source' not in data


def test_patient_selector_only_appears_with_more_than_one_patient(env):
    c = new_browser(make_account=True)
    upload(c, [('PC0101.xlsx', workbook_bytes('PC0101'))])
    assert 'name="patient"' not in c.get('/summary').get_data(as_text=True)
    upload(c, [('PC0303.xlsx', workbook_bytes('PC0303'))])                   # another patient: not added
    assert 'name="patient"' not in c.get('/summary').get_data(as_text=True)    # patient account: one record only
    assert len(rows('SELECT id FROM documents')) == 1
    make_admin('tester')                                                       # team accounts may hold several
    upload(c, [('PC0303.xlsx', workbook_bytes('PC0303'))])
    html = c.get('/summary').get_data(as_text=True)
    assert 'name="patient"' in html and '>tester<' in html and '>PC0303<' in html


def test_flag_explanation_is_behind_a_review_button(env):
    c = new_browser(make_account=True)
    upload(c, [('PC0101.xlsx', workbook_bytes('PC0101'))])
    html = c.get('/summary?span=all').get_data(as_text=True)
    cards = re.findall(r'<article class="warning-card">(.*?)</article>', html, re.S)
    assert cards
    for card in cards:
        visible, hidden = card.split('<details class="flag-review">')
        assert '<summary class="review-btn">Review</summary>' in hidden       # the button
        assert 'review-body' in hidden and 'Recorded' in hidden               # the explanation opens from it
        assert 'Reference:' in visible and 'Recorded' not in visible
        assert 'Discuss with a qualified clinician' not in visible and 'illustrative adult range' not in visible
        assert 'clinician' in hidden or 'illustrative adult range' in hidden  # same message as before, now inside Review


def test_patients_see_no_trace_of_the_system_dashboard(env):
    c = new_browser('patient', make_account=True)
    for url in ('/summary', '/profile', '/documents'):
        html = c.get(url).get_data(as_text=True)
        assert 'System dashboard' not in html and '/admin' not in html and 'admin' not in html.lower()
    unknown = c.get('/no-such-page')
    for client in (c, app_module.app.test_client()):                         # signed in as a patient, or signed out
        r = client.get('/admin')
        assert r.status_code == 404 and r.data == unknown.data               # identical to an address that does not exist
    for asset in ('/static/js/chat.js', '/static/js/charts.js', '/static/js/documents.js', '/static/css/style.css'):
        with c.get(asset) as r:
            body = r.data.lower()
        assert b'admin' not in body and b'system dashboard' not in body


def test_team_members_get_the_same_app_plus_the_dashboard(env):
    c = new_browser('babita', make_account=True)
    make_admin('babita')
    html = c.get('/summary').get_data(as_text=True)
    assert 'Welcome to HealthLens, babita!' in html                          # same patient experience
    assert '>System dashboard</a>' in html and '>Documents</a>' in html
    upload(c, [('PC0101.xlsx', workbook_bytes('PC0101'))])                  # can upload their own records too
    assert '<span>Patient ID</span><strong>PC0101</strong>' in c.get('/summary').get_data(as_text=True)
    assert c.get('/admin').status_code == 200


def test_system_dashboard_statistics(env):
    a = new_browser('alice', make_account=True)
    upload(a, [('PC0101.xlsx', workbook_bytes('PC0101')), ('scan.pdf', PDF)])
    a.post('/api/patients/alice/ask', headers={'X-CSRF-Token': csrf(a)}, json={'question': 'How is my blood pressure?'})
    a.post('/api/patients/alice/ask', headers={'X-CSRF-Token': csrf(a)}, json={'question': 'Do I have diabetes?'})
    register(app_module.app.test_client(), 'bob')                           # registered, never signed in
    login(app_module.app.test_client(), 'alice', 'Wrong@Password1')         # one failed sign-in
    t = new_browser('team1', make_account=True)
    make_admin('team1')
    with app_module.db() as conn:
        s = app_module.system_stats(conn)
    assert (s['users'], s['admins'], s['signed_in_users'], s['users_with_records'], s['never_uploaded']) == (3, 1, 2, 1, 2)
    assert (s['patient_workbooks'], s['supporting_files'], s['failed_7d'], s['sign_ins']) == (1, 1, 1, 2)
    assert s['storage'] == len(workbook_bytes('PC0101')) + len(PDF)
    assert s['chat']['questions'] == 2 and s['chat']['modes']['screened'] == 1 and s['chat']['modes']['offline'] == 1
    assert sum(s['activity']['sign_ups']) == 3 and sum(s['activity']['sign_ins']) == 2 and len(s['activity']['days']) == 14
    html = t.get('/admin').get_data(as_text=True)
    assert 'Registered users' in html and 'Users who have signed in' in html and 'id="activityChart"' in html
    assert '>Patient<' in html and '>Team<' in html
    assert 'Assistant performance' in html and re.search(r'<td>\d+\.\d\d s</td>', html)


def test_system_dashboard_shows_no_health_data_or_questions(env):
    a = new_browser('alice', make_account=True)
    upload(a, [('PC0101.xlsx', workbook_bytes('PC0101'))])
    a.post('/api/patients/alice/ask', headers={'X-CSRF-Token': csrf(a)}, json={'question': 'Tell me about my glucose levels please'})
    make_admin('alice')
    html = a.get('/admin').get_data(as_text=True)
    for private in ('mmHg', 'mg/dL', 'HbA1c', 'Systolic', 'Tell me about my glucose levels', 'Metformin', 'warning-card'):
        assert private not in html


def test_signup_page_checks_mistakes_in_the_browser_first(env):
    """A mistake is caught before sending, so the page does not reload and nothing typed is lost.
    The script lives in static/js/register.js, so the page itself still lists no rules."""
    html = app_module.app.test_client().get('/register').get_data(as_text=True)
    assert 'js/register.js' in html and 'id="formError"' in html and 'hidden' in html
    js = (ROOT / 'static' / 'js' / 'register.js').read_text(encoding='utf-8')
    for message in ('Password must be 10–128 characters.', 'at least one uppercase letter', 'at least one lowercase letter',
                    'at least one number', 'at least one special character', 'Passwords do not match.',
                    'preventDefault'):
        assert message in js


@pytest.fixture()
def toronto_time(monkeypatch):
    import time
    if not hasattr(time, 'tzset'):
        pytest.skip('changing the time zone inside a test needs Linux or macOS')
    monkeypatch.setenv('TZ', 'America/Toronto')
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_times_are_stored_in_utc_but_shown_in_local_time(env, toronto_time):
    """10:20 pm on Nov 14 in Toronto is 03:20 on Nov 15 in UTC: the dashboard must say Nov 14, 22:20."""
    assert app_module.localtime_filter('2026-11-15T03:20:00+00:00') == '2026-11-14 22:20'
    assert app_module.localtime_filter('2026-11-15T03:20:00.123456+00:00', '%Y-%m-%d') == '2026-11-14'
    assert app_module.localtime_filter(None) == '—'
    babita = new_browser('babita', make_account=True)
    make_admin('babita')
    with app_module.db() as conn:
        conn.execute("UPDATE audit_events SET created_at=? WHERE action='login'", ('2026-11-15T03:20:00+00:00',))
    html = babita.get('/admin').get_data(as_text=True)
    assert '2026-11-14 22:20' in html and '(UTC)' not in html and 'local time' in html


def test_activity_chart_counts_days_in_local_time(env, toronto_time):
    from datetime import datetime, timedelta, timezone
    new_browser('babita', make_account=True)
    late_evening = datetime.now().astimezone().replace(hour=22, minute=30, second=0, microsecond=0)
    stored = late_evening.astimezone(timezone.utc).isoformat(timespec='seconds')   # already "tomorrow" in UTC
    with app_module.db() as conn:
        conn.execute("UPDATE users SET created_at=?", (stored,))
        s = app_module.system_stats(conn)
    today = late_evening.date().isoformat()[5:]
    assert s['activity']['days'][-1] == today and s['activity']['sign_ups'][-1] == 1 and sum(s['activity']['sign_ups']) == 1


# ---------------------------------------------------------------- upload section and risk display
from pathlib import Path  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / 'sample_documents'


def test_documents_page_groups_documents_by_record_and_says_what_was_found(env):
    c = new_browser('sana', make_account=True)
    upload(c, [('01_lab_report_2025-09-25.pdf', (SAMPLES / '01_lab_report_2025-09-25.pdf').read_bytes())])
    upload(c, [('letter.txt', b'Dear patient, see you next week.')])
    html = c.get('/documents').get_data(as_text=True)
    assert 'Your record' in html and 'Patient ID PC0303' in html and '1 document<' in html
    assert '15 lab results.' in html
    assert 'Stored only' in html and 'No health values found; stored only.' in html


def test_summary_shows_risk_level_factors_and_missing_measures(env, monkeypatch):
    monkeypatch.setattr(app_module.risk_model, 'load_model', lambda *a, **k: None)   # rule-based fallback view
    c = new_browser('sana', make_account=True)
    upload(c, [('01_lab_report_2025-09-25.pdf', (SAMPLES / '01_lab_report_2025-09-25.pdf').read_bytes())])
    html = c.get('/summary?category=cardiovascular').get_data(as_text=True)
    assert 'class="pill risk-low">Low risk' in html and 'risk factors' in html
    assert 'Risk factors:</strong> LDL cholesterol above listed range' in html
    assert 'key inputs missing: Blood pressure' in html and 'not medical diagnoses' in html
    assert 'missing' in c.get('/summary').get_data(as_text=True)


def test_summary_shows_the_trained_risk_score_without_any_confidence_value(env):
    c = new_browser('sana', make_account=True)
    upload(c, [('01_lab_report_2025-09-25.pdf', (SAMPLES / '01_lab_report_2025-09-25.pdf').read_bytes())])
    html = c.get('/summary?category=metabolic').get_data(as_text=True)
    assert '<small>/100</small></strong><span>risk score</span>' in html
    assert '/100' in html and 'key inputs available' in html
    assert 'key inputs missing: ' in html and 'This prediction is based on the other' in html and 'so the prediction score may not be accurate' in html         # the business partner's clause
    assert 'confiden' not in html.lower() and 'uncertaint' not in html.lower()
    assert '/100' in c.get('/summary').get_data(as_text=True)                    # category panel


def test_missing_inputs_count_only_what_was_never_uploaded(env):
    """The trained score uses every document, so readings outside the 3-month view are not called missing."""
    c = new_browser('sana', make_account=True)
    upload(c, [(f.name, f.read_bytes()) for f in sorted(SAMPLES.iterdir()) if f.is_file() and f.name[:2].isdigit()])
    html = c.get('/summary?span=3m').get_data(as_text=True)
    assert 'missing-count' not in html and 'may not be accurate' not in c.get('/summary?category=metabolic').get_data(as_text=True)
