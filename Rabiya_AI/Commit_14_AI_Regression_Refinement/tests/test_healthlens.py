"""Automated tests for the HealthLens engine, guardrails and API.  Run:  python -m pytest -q"""
import io
import json
import os
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ['HEALTHLENS_LLM_PROVIDER'] = 'offline'
os.environ['HEALTHLENS_SECRET_KEY'] = os.environ.get('HEALTHLENS_SECRET_KEY') or 'test-secret'   # keeps tests away from the real database at import time

from healthlens import analyze_book, llm  # noqa: E402
from healthlens.analysis import mann_kendall, theil_sen  # noqa: E402
from healthlens.data import parse_range  # noqa: E402


def load(pid):
    z = zipfile.ZipFile(ROOT / 'patient_data.zip')
    name = next(n for n in z.namelist() if n.endswith(f'/{pid}.xlsx'))
    return pd.read_excel(io.BytesIO(z.read(name)), sheet_name=None, engine='openpyxl')


@pytest.fixture(scope='module')
def a101():
    return analyze_book(load('PC0101'))


@pytest.fixture(scope='module')
def a303():
    return analyze_book(load('PC0303'))


# ------------------------------------------------------------- statistics
def test_mann_kendall_detects_monotonic_trend():
    _, p = mann_kendall(np.arange(20, dtype=float))
    assert p < 0.001
    _, p = mann_kendall(np.array([5, 3, 6, 4, 5, 3, 6, 4, 5, 4], dtype=float))
    assert p > 0.2


def test_theil_sen_ignores_outlier():
    t = np.arange(10, dtype=float)
    y = 2 * t + 1
    y[5] = 500
    slope, _ = theil_sen(t, y)
    assert slope == pytest.approx(2, abs=0.01)


@pytest.mark.parametrize('text,expected', [('70–99', (70, 99)), ('>40', (40, None)), ('<100', (None, 100)), ('4.0–5.6', (4.0, 5.6)), ('n/a', (None, None))])
def test_parse_reference_range(text, expected):
    assert parse_range(text) == expected


# ------------------------------------------------------------- engine
def test_every_insight_points_to_real_evidence(a101):
    ids = {e['id'] for e in a101['evidence']}
    refs = [t['evidence'] for t in a101['trends'] if t['evidence']]
    refs += [x['evidence'] for x in a101['anomalies'] + a101['lab_flags'] + a101['medication_context']]
    refs += [e for s in a101['scenarios'] for e in s['evidence'] + s['context']]
    assert refs and set(refs) <= ids


def test_llm_text_has_no_calendar_dates(a101):
    for e in a101['evidence']:
        assert not any(ch in e['llm_text'] for ch in ('2025', '2026')), e['llm_text']


def test_context_is_deidentified(a101):
    ctx = llm.build_context(a101)
    assert 'PC0101' not in ctx and '2026' not in ctx and 'age band 50–59' in ctx


def test_pcos_scenario_for_patient_with_irregular_cycles(a303):
    pcos = next(s for s in a303['scenarios'] if s['key'] == 'pcos')
    assert pcos['status'] in ('pattern', 'partial')
    assert any(c['signal'] == 'Cycle regularity' and c['status'] == 'signal' for c in pcos['checks'])


def test_pcos_not_applicable_for_male(a101):
    assert next(s for s in a101['scenarios'] if s['key'] == 'pcos')['status'] == 'not_applicable'


def test_result_is_json_serialisable(a101):
    json.dumps(a101)


# ------------------------------------------------------------- guardrails
@pytest.mark.parametrize('bad', [
    'You may have prediabetes.', 'These readings are consistent with hypertension.', 'There is a 70% chance this gets worse.',
    'You are at increased risk of heart disease.', 'Please go to the emergency room.', 'This is dangerous.',
    'You should stop taking metformin.', 'Consider increasing your dose.', 'Low sleep is causing the higher stress.',
    'This will lead to diabetes.'])
def test_guardrails_block_unsafe_text(bad):
    assert llm.guardrail_issues(bad)


@pytest.mark.parametrize('good', [
    'Your record lists PCOS in the medical history.', 'This is not a diagnosis.',
    'Sleep and stress moved together, but this does not show that one caused the other.',
    'These patterns are worth discussing with a healthcare professional.'])
def test_guardrails_allow_safe_text(good):
    assert not llm.guardrail_issues(good)


def test_engine_evidence_never_trips_guardrails(a101, a303):
    for a in (a101, a303):
        assert not [e for e in a['evidence'] if llm.guardrail_issues(e['llm_text'])]


def test_validate_rejects_unknown_evidence(a101):
    reply = {'overview': 'ok', 'points': [{'title': 't', 'explanation': 'x', 'evidence': ['E999']}]}
    assert any('unknown evidence' in p for p in llm.validate(reply, {e['id'] for e in a101['evidence']}, 'summary'))


@pytest.mark.parametrize('q,kind', [('Do I have diabetes?', 'diagnosis'), ('Should I stop taking metformin?', 'medication'),
                                    ('I have chest pain right now', 'acute'), ('How is my sleep?', None)])
def test_question_screening(q, kind):
    assert llm.screen_question(q)[0] == kind


def test_offline_summary_and_answer(a303):
    s = llm.summarize(a303)
    assert s['mode'] == 'offline' and s['points']
    ans = llm.ask(a303, 'How has my blood pressure changed?')
    assert ans['evidence']


# ------------------------------------------------------------- LLM path with a mock provider (first reply unsafe -> retry)
class _Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        users = [m for m in body['messages'] if m['role'] == 'user']
        ids = [ln.split(' ')[0] for ln in users[0]['content'].splitlines() if ln[:1] == 'E' and ln[1:2].isdigit()]
        if len(users) == 1:
            reply = {'overview': 'You may have prediabetes.', 'points': [{'title': 'x', 'explanation': 'y', 'evidence': ['E999']}]}
        else:
            reply = {'overview': 'A year of records.', 'points': [{'title': 'Labs', 'explanation': 'Some values sat above range.', 'evidence': ids[:1]}],
                     'questions': ['What does this mean?'], 'limitations': 'Synthetic data.'}
        out = json.dumps({'choices': [{'message': {'content': json.dumps(reply)}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def test_llm_retry_after_guardrail_failure(a101, monkeypatch):
    srv = HTTPServer(('127.0.0.1', 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv('HEALTHLENS_LLM_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    monkeypatch.setenv('OPENAI_BASE_URL', f'http://127.0.0.1:{srv.server_port}/v1')
    llm._cache.clear()
    s = llm.summarize(a101)
    srv.shutdown(); srv.server_close()
    assert s['mode'] == 'llm' and s['overview'] == 'A year of records.'


def test_llm_unreachable_falls_back(a101, monkeypatch):
    monkeypatch.setenv('HEALTHLENS_LLM_PROVIDER', 'ollama')
    monkeypatch.setenv('OLLAMA_HOST', 'http://127.0.0.1:9')
    monkeypatch.setenv('HEALTHLENS_LLM_TIMEOUT', '2')
    llm._cache.clear()
    s = llm.summarize(a101)
    assert s['mode'] == 'fallback' and s['points']


# ------------------------------------------------------------- API
@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'DB', tmp_path / 'users.sqlite3')
    monkeypatch.setattr(app_module, 'UPLOADS', tmp_path / 'uploads')
    app_module.app.config['TESTING'] = True
    c = app_module.app.test_client()
    c.post('/register', data={'username': 'tester', 'email': 't@example.com', 'password': 'longpassword1', 'confirm': 'longpassword1'})
    c.post('/login', data={'identity': 'tester', 'password': 'longpassword1'})
    return c


def test_api_requires_login():
    import app as app_module
    assert app_module.app.test_client().get('/api/patients').status_code == 401



def test_api_flow(client):
    # Register and log in a test user
    password = 'HealthLens@Test123'

    register_response = client.post(
        '/register',
        data={
            'username': 'apitester',
            'email': 'apitester@example.com',
            'password': password,
            'confirm': password
        },
        follow_redirects=False
    )

    assert register_response.status_code == 302, (
        f"Registration failed: {register_response.get_data(as_text=True)[:500]}"
    )

    login_response = client.post(
        '/login',
        data={
            'identity': 'apitester',
            'password': password
        },
        follow_redirects=False
    )

    assert login_response.status_code == 302, (
        f"Login failed: {login_response.get_data(as_text=True)[:500]}"
    )

    # Verify authentication
    profile = client.get('/profile')
    assert profile.status_code == 200

    # Upload a patient workbook (new accounts start empty: there is no demo data)
    client.get('/documents')
    with client.session_transaction() as s:
        token = s['upload_csrf']
    z = zipfile.ZipFile(ROOT / 'patient_data.zip')
    raw = z.read(next(n for n in z.namelist() if n.endswith('/PC0101.xlsx')))
    client.post('/documents/upload', data={'csrf': token, 'files': [(io.BytesIO(raw), 'PC0101.xlsx')]},
                content_type='multipart/form-data')

    # Verify patient API
    response = client.get('/api/patients')
    assert response.status_code == 200
    patients = response.get_json()['patients']
    assert patients == ['apitester']              # the account's own record; PC0101 is stored as a Patient ID
    pid = patients[0]

    # Verify patient analysis
    response = client.get(f'/api/patients/{pid}/analysis')
    assert response.status_code == 200

    a = response.get_json()
    assert a['evidence'] and a['scenarios']

    # Verify summary page
    page = client.get(f'/summary?patient={pid}')
    assert page.status_code == 200

    # Get CSRF token
    client.get('/documents')

    with client.session_transaction() as s:
        token = s['upload_csrf']

    # Verify chatbot rejects requests without CSRF
    response = client.post(
        f'/api/patients/{pid}/ask',
        json={'question': 'How is my sleep?'}
    )
    assert response.status_code == 400

    # Verify chatbot answers valid questions
    response = client.post(
        f'/api/patients/{pid}/ask',
        json={'question': 'How is my sleep?'},
        headers={'X-CSRF-Token': token}
    )

    assert response.status_code == 200
    assert response.get_json()['answer']

    # Verify unsafe medical questions are screened
    response = client.post(
        f'/api/patients/{pid}/ask',
        json={'question': 'Do I have diabetes?'},
        headers={'X-CSRF-Token': token}
    )

    assert response.status_code == 200
    assert response.get_json()['mode'] == 'screened'



def test_summary_includes_deidentified_trace(a101):
    s = llm.summarize(a101)
    assert 'trace' in s and 'PC0101' not in s['trace']['user_prompt'] and 'EVIDENCE:' in s['trace']['user_prompt']


# ---------------------------------------------------------------- risk level, score and factors (healthlens.risk)
from healthlens import risk  # noqa: E402


def scen(status, statuses, key='cardiovascular'):
    checks = [{'signal': f'S{i}', 'status': s, 'text': f'text {i}', 'evidence': []} for i, s in enumerate(statuses)]
    return {'key': key, 'name': key, 'status': status, 'checks': checks, 'note': ''}


@pytest.mark.parametrize('status, statuses, level', [
    ('pattern', ['signal'] * 3 + ['quiet'] * 3, 'High'), ('partial', ['signal'] * 2 + ['quiet'] * 4, 'Moderate'),
    ('none', ['signal'] + ['quiet'] * 5, 'Low'), ('insufficient', ['signal'] + ['missing'] * 5, 'Low'),
    ('insufficient', ['missing'] * 6, None)])
def test_risk_level_follows_the_category_rule(status, statuses, level):
    r = risk.assess(scen(status, statuses))
    assert r['level'] == level and r['score'] == statuses.count('signal') and r['out_of'] == 6


def test_risk_from_a_single_document_names_what_is_missing():
    r = risk.assess(scen('insufficient', ['signal', 'missing', 'missing', 'missing', 'missing', 'missing']))
    assert r['label'] == 'Low risk' and r['limited'] and r['available'] == 1
    assert r['factors'] == [{'name': 'S0', 'detail': 'text 0'}]
    assert '5 of 6 key inputs missing: S1, S2, S3, S4 and S5. This prediction is based on the other 1 (S0)' in r['missing_note']
    assert 'may not be accurate' in r['missing_note']
    assert 'not a diagnosis' in r['disclaimer']


def test_measures_outside_the_period_point_to_the_full_record():
    r = risk.assess(scen('none', ['quiet', 'missing', 'missing']), missing_in_period={'S1'})
    assert r['outside'] == ['S1'] and r['absent'] == ['S2'] and 'Not in this period: S1 (see Full record)' in r['missing_note']


def test_not_applicable_category_has_no_level():
    r = risk.assess({'key': 'pcos', 'name': 'PCOS', 'status': 'not_applicable', 'checks': [], 'note': 'Male patient'})
    assert r['applicable'] is False and r['level'] is None


def test_every_sample_patient_gets_a_valid_risk_level():
    for pid in ('PC0101', 'PC0303', 'PC0450'):
        for s in analyze_book(load(pid))['scenarios']:
            r = risk.assess(s)
            assert r['level'] in (None, 'Low', 'Moderate', 'High')
            if r['applicable'] and r['level']:
                assert r['level'] == risk.LEVEL_OF_STATUS.get(s['status'], r['level'])


# ---------------------------------------------------------------- trained risk model (healthlens/risk_model.py)
from healthlens import risk_model as RM  # noqa: E402

MODEL = RM.load_model()


def test_trained_model_file_covers_every_category_and_was_tested_on_unseen_patients():
    assert MODEL and set(MODEL['categories']) == {'metabolic', 'cardiovascular', 'pcos'} and MODEL['patients'] == 900
    for cat, m in MODEL['categories'].items():
        mt = m['metrics']
        assert mt['train_n'] > 3 * mt['test_n'] and mt['test_n'] > 0                     # 80 / 20 split
        assert len(m['weights']) == len(m['inputs']) + len(m['missing_flags'])
        assert mt['test_auc'] > 0.6, (cat, mt['test_auc'])                                # better than chance on new patients


def test_model_never_uses_medical_history_or_medications_as_inputs():
    for m in MODEL['categories'].values():
        assert not any(w in name for name in m['inputs'] for w in ('history', 'medication', 'med_', 'adherence'))


def test_model_scores_a_full_record_and_a_single_document():
    full = RM.score(load('PC0303'))
    assert set(full) == {'metabolic', 'cardiovascular', 'pcos'}
    for r in full.values():
        assert 0 <= r['score'] <= 100 and r['level'] in ('Low', 'Moderate', 'High') and r['raising'] is not None
    labs_only = {'Patient Profile': load('PC0303')['Patient Profile'], 'Lab Results': load('PC0303')['Lab Results']}
    single = RM.score(labs_only)
    assert single['metabolic']['sources_used'] == ['labs'] and 0 <= single['metabolic']['score'] <= 100
    assert 'pcos' not in RM.score(load('PC0101'))                                     # male patient: not applicable


def test_risk_summary_uses_the_trained_score_and_keeps_the_missing_inputs_clause():
    s = scen('none', ['quiet', 'missing', 'quiet', 'missing', 'quiet', 'quiet'])
    model = {'level': 'Moderate', 'score': 51, 'raising': [{'name': 'LDL', 'detail': 'LDL cholesterol 147 (latest)'}],
             'lowering': [], 'sources_used': ['labs']}
    r = risk.assess(s, model=model)
    assert (r['level'], r['risk_score'], r['source']) == ('Moderate', 51, 'trained model')
    assert '2 of 6 key inputs missing: S1 and S3. This prediction is based on the other 4 (S0, S2, S4 and S5), so the prediction score may not be accurate' in r['missing_note']
    assert r['factors'][0]['detail'].startswith('LDL')
    assert risk.assess(s)['risk_score'] is None                                       # no model -> rule-based


def test_model_outputs_never_contain_a_confidence_value():
    text = json.dumps(RM.score(load('PC0303'))).lower()
    assert 'confiden' not in text and 'probab' not in text and 'uncertain' not in text
