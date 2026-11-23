"""Backend security: password rules on the server, session key, team (admin) table and roles checked per request,
sign-in redirects, team access from the dashboard, SQL injection, and database connections closing.
Owner: Babita Oli (Backend & Integration Lead). Shared set-up (database per test, sign-up/sign-in helpers) is in tests/conftest.py.
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

from conftest import (columns, PASSWORD, PDF, ROOT, app_module, llm, workbook_bytes, register, login, new_browser, csrf, upload, rows, make_admin, uid, team_names, team_post)  # noqa: F401


@pytest.mark.parametrize('weak,message', [
    ('short1!A', 'Password must be 10–128 characters.'),
    ('alllowercase1!', 'at least one uppercase letter'),
    ('ALLUPPERCASE1!', 'at least one lowercase letter'),
    ('NoNumbersHere!', 'at least one number'),
    ('NoSpecial12345', 'at least one special character'),
])
def test_server_still_enforces_password_rules(env, weak, message):
    html = register(app_module.app.test_client(), password=weak).get_data(as_text=True)
    assert message in html and rows('SELECT * FROM users') == []


def test_passwords_must_match_and_good_password_is_accepted(env):
    c = app_module.app.test_client()
    assert 'Passwords do not match' in register(c, confirm='Different@Pass123').get_data(as_text=True)
    assert 'Account created' in register(c).get_data(as_text=True)
    stored = rows('SELECT password_hash FROM users')[0]['password_hash']
    assert PASSWORD not in stored and stored.count('$') == 2                  # salted PBKDF2 hash, never the password


def test_session_key_is_kept_in_the_database(env, monkeypatch):
    monkeypatch.delenv('HEALTHLENS_SECRET_KEY', raising=False)
    first = app_module._secret_key()
    assert first == app_module._secret_key() and len(first) == 64          # same key after a restart
    assert rows("SELECT value FROM app_settings WHERE key='secret_key'")[0]['value'] == first


def test_team_access_is_stored_separately_from_patient_accounts(env):
    new_browser('babita', make_account=True)
    make_admin('babita')
    assert 'is_admin' not in columns('users')
    assert len(rows('SELECT * FROM team_admins')) == 1


def test_old_admin_column_is_moved_to_the_team_table(env):
    with closing(sqlite3.connect(app_module.DB)) as conn, conn:                             # database from the previous version
        conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL, email TEXT UNIQUE COLLATE NOCASE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0)')
        for name, admin in (('rabiya', 1), ('patient1', 0)):
            conn.execute('INSERT INTO users(username,email,password_hash,created_at,is_admin) VALUES(?,?,?,?,?)',
                         (name, f'{name}@example.com', app_module.hash_password(PASSWORD), '2026-10-01T00:00:00+00:00', admin))
    assert [r['username'] for r in rows('SELECT u.username FROM team_admins t JOIN users u ON u.id=t.user_id')] == ['rabiya']
    assert 'is_admin' not in {r[1] for r in rows('PRAGMA table_info(users)')}
    assert new_browser('rabiya').get('/admin').status_code == 200 and new_browser('patient1').get('/admin').status_code == 404


def test_sign_in_lands_team_on_dashboard_and_patients_on_summary(env):
    register(app_module.app.test_client(), 'babita')
    register(app_module.app.test_client(), 'amy')
    make_admin('babita')
    team, patient = app_module.app.test_client(), app_module.app.test_client()
    assert login(team, 'babita').headers['Location'].endswith('/admin')
    assert login(patient, 'amy').headers['Location'].endswith('/summary')
    assert team.get('/').headers['Location'].endswith('/admin')
    assert patient.get('/').headers['Location'].endswith('/summary')


def test_role_is_checked_in_the_database_on_every_request(env):
    c = new_browser('babita', make_account=True)
    make_admin('babita')
    assert c.get('/admin').status_code == 200
    with app_module.db() as conn:
        conn.execute('DELETE FROM team_admins')                              # access removed while signed in
    assert c.get('/admin').status_code == 404
    with c.session_transaction() as s:
        s['is_admin'] = True; s['role'] = 'admin'                           # anything a browser could send is ignored
    assert c.get('/admin').status_code == 404


def test_no_web_request_can_make_an_account_admin(env):
    c = app_module.app.test_client()
    c.post('/register', data={'username': 'sneaky', 'email': 's@example.com', 'password': PASSWORD, 'confirm': PASSWORD,
                              'is_admin': '1', 'admin': 'true', 'role': 'admin'})
    assert rows('SELECT * FROM team_admins') == []
    login(c, 'sneaky')
    assert c.get('/admin').status_code == 404


def test_admin_access_is_given_and_removed_only_with_the_account_password(env):
    register(app_module.app.test_client(), 'rajbeer')
    team = lambda: [r['username'] for r in rows('SELECT u.username FROM team_admins t JOIN users u ON u.id=t.user_id')]
    assert app_module.set_admin('nobody', PASSWORD) == 'No such registered username.'
    assert 'Incorrect password' in app_module.set_admin('rajbeer', 'wrong') and team() == []
    assert app_module.set_admin('rajbeer', PASSWORD) == 'Admin access enabled for: rajbeer' and team() == ['rajbeer']
    assert app_module.set_admin('rajbeer', PASSWORD, make_admin=False) == 'Admin access removed for: rajbeer' and team() == []


INJECTIONS = ["' OR '1'='1", "' OR 1=1 --", "admin'--", "x'; DROP TABLE users; --", '" OR ""="',
              "1 UNION SELECT id,password_hash FROM users", "babita' AND 1=1; INSERT INTO team_admins VALUES(1,'x'); --"]


@pytest.mark.parametrize('payload', INJECTIONS)
def test_sign_in_is_not_open_to_sql_injection(env, payload):
    register(app_module.app.test_client(), 'babita')
    c = app_module.app.test_client()
    r = c.post('/login', data={'identity': payload, 'password': payload})
    assert r.status_code == 200 and 'Incorrect email/username or password' in r.get_data(as_text=True)
    r = c.post('/login', data={'identity': 'babita', 'password': payload})
    assert r.status_code == 200 and c.get('/summary').status_code == 302    # still signed out
    assert len(rows('SELECT * FROM users')) == 1 and rows('SELECT * FROM team_admins') == []


@pytest.mark.parametrize('payload', INJECTIONS)
def test_sign_up_is_not_open_to_sql_injection(env, payload):
    c = app_module.app.test_client()
    c.post('/register', data={'username': payload, 'email': 'a@example.com', 'password': PASSWORD, 'confirm': PASSWORD})
    c.post('/register', data={'username': 'okname', 'email': payload, 'password': PASSWORD, 'confirm': PASSWORD})
    assert rows('SELECT * FROM users') == [] and rows('SELECT * FROM team_admins') == []


@pytest.mark.parametrize('payload', INJECTIONS)
def test_addresses_and_chat_are_not_open_to_sql_injection(env, payload):
    import urllib.parse
    a = new_browser('alice', make_account=True)
    upload(a, [('PC0101.xlsx', workbook_bytes('PC0101'))])
    b = new_browser('bob', make_account=True)
    q = urllib.parse.quote(payload, safe='')
    assert 'Welcome to HealthLens, bob!' in b.get(f'/summary?patient={q}').get_data(as_text=True)
    assert b.get(f'/api/patients/{q}/analysis').status_code == 404
    assert b.post(f'/api/patients/{q}/ask', headers={'X-CSRF-Token': csrf(b)}, json={'question': payload + ' sleep?'}).status_code == 404
    assert b.get('/api/patients').get_json() == {'patients': []}             # bob still cannot reach alice's record
    assert len(rows('SELECT * FROM users')) == 2 and rows('SELECT * FROM team_admins') == []


def test_every_query_uses_placeholders():
    """No SQL statement in the app is built from text: values are always passed separately (the ? placeholders)."""
    import ast
    tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, 'attr', '') == 'execute' and node.args:
            assert not isinstance(node.args[0], (ast.JoinedStr, ast.BinOp)), f'SQL built from text at line {node.lineno}'


def test_team_member_gives_and_removes_access_from_the_dashboard(env):
    babita = new_browser('babita', make_account=True)
    new_browser('haya', make_account=True)
    make_admin('babita')
    html = babita.get('/admin').get_data(as_text=True)
    assert 'Manage team members' in html and 'data-action="grant" data-user-id="2" data-username="haya"' in html
    assert 'id="teamDialog"' in html and 'type="password" name="password"' in html          # password asked in a pop-up
    r = team_post(babita, 'haya', 'grant')
    html = r.get_data(as_text=True)
    assert 'Team access granted to haya.' in html and team_names() == ['babita', 'haya']
    assert 'data-action="remove" data-user-id="2" data-username="haya"' in html              # now offered Revoke
    haya = new_browser('haya')
    assert haya.get('/admin').status_code == 200                              # works straight away
    r = team_post(babita, 'haya', 'remove')
    assert 'Team access revoked for haya.' in r.get_data(as_text=True) and team_names() == ['babita']
    assert haya.get('/admin').status_code == 404
    actions = [r['action'] for r in rows('SELECT action FROM audit_events')]
    assert 'team_grant' in actions and 'team_remove' in actions               # every change is logged


def test_team_change_needs_the_admins_own_password(env):
    babita = new_browser('babita', make_account=True)
    new_browser('haya', make_account=True)
    make_admin('babita')
    r = team_post(babita, 'haya', 'grant', password='Wrong-Password-1')
    assert 'Your password was incorrect' in r.get_data(as_text=True) and team_names() == ['babita']
    assert 'team_access_denied' in [r['action'] for r in rows('SELECT action FROM audit_events')]


def test_team_change_is_refused_without_a_valid_csrf_token(env):
    babita = new_browser('babita', make_account=True)
    new_browser('haya', make_account=True)
    make_admin('babita')
    r = team_post(babita, 'haya', 'grant', token='forged')
    assert 'Verification failed' in r.get_data(as_text=True) and team_names() == ['babita']


def test_team_member_cannot_remove_their_own_access(env):
    babita = new_browser('babita', make_account=True)
    make_admin('babita')
    html = babita.get('/admin').get_data(as_text=True)
    assert 'data-username="babita"' not in html and '<span class="muted small">You</span>' in html   # no button on your own row
    r = team_post(babita, 'babita', 'remove')
    assert 'cannot revoke your own team access' in r.get_data(as_text=True) and team_names() == ['babita']


def test_patients_and_visitors_cannot_use_team_access(env):
    amy = new_browser('amy', make_account=True)
    new_browser('babita', make_account=True)
    unknown = amy.get('/no-such-page')
    for client in (amy, app_module.app.test_client()):
        r = client.post('/admin/team', data={'csrf': 'x', 'action': 'grant', 'user_id': uid('amy'), 'password': PASSWORD})
        assert r.status_code == 404 and r.data == unknown.data
    r = team_post(amy, 'amy', 'grant')                                        # even with a valid token and password
    assert r.status_code == 404 and team_names() == []


def test_team_access_form_rejects_bad_input_safely(env):
    babita = new_browser('babita', make_account=True)
    make_admin('babita')
    for data in ({'action': 'grant', 'user_id': "1 OR 1=1"}, {'action': 'drop', 'user_id': uid('babita')},
                 {'action': 'grant', 'user_id': '999'}, {'action': 'grant'}):
        r = babita.post('/admin/team', data={'csrf': csrf(babita), 'password': PASSWORD, **data}, follow_redirects=True)
        assert r.status_code == 200 and 'Choose an account first.' in r.get_data(as_text=True)
    assert team_names() == ['babita'] and len(rows('SELECT * FROM users')) == 1


def test_database_connections_are_closed_after_use(env):
    """No 'ResourceWarning: unclosed database': every connection closes when its with-block ends."""
    with app_module.db() as conn:
        conn.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')
    with pytest.raises(ZeroDivisionError):
        with app_module.db() as conn:
            conn.execute("INSERT INTO app_settings(key,value) VALUES('x','y')"); 1/0
    assert rows("SELECT * FROM app_settings WHERE key='x'") == []           # failed changes are undone, then closed


def test_signup_mistake_keeps_username_and_email_but_never_echoes_passwords(env):
    weak = 'NoSpecial12345'
    html = register(app_module.app.test_client(), name='amy_k', password=weak).get_data(as_text=True)
    assert 'at least one special character' in html
    assert 'value="amy_k"' in html and 'value="amy_k@example.com"' in html   # no need to retype these
    assert weak not in html                                                   # passwords are never sent back


def test_duplicate_signup_keeps_what_was_typed(env):
    new_browser('amy', make_account=True)
    html = register(app_module.app.test_client(), name='amy').get_data(as_text=True)
    assert 'already exists' in html and 'value="amy"' in html and PASSWORD not in html


def test_wrong_signin_password_keeps_the_username(env):
    new_browser('amy', make_account=True)
    r = login(app_module.app.test_client(), 'amy', 'Wrong-Password-1')
    html = r.get_data(as_text=True)
    assert 'Incorrect email/username or password.' in html and 'value="amy"' in html and 'Wrong-Password-1' not in html


def test_signup_input_is_escaped_when_shown_again(env):
    c = app_module.app.test_client()
    html = c.post('/register', data={'username': '"><script>x</script>', 'email': 'a@b.co', 'password': 'p',
                                     'confirm': 'p'}).get_data(as_text=True)
    assert '<script>x</script>' not in html and '&#34;&gt;&lt;script&gt;' in html
