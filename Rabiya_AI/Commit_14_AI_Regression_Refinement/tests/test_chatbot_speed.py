"""Chatbot speed and timing: response times recorded per answer, the AI prompt and answer unchanged,
Ollama model kept loaded (keep_alive) and warmed up when a page opens.
Owner: Rabiya Farooq (Team Lead & AI Lead). Shared set-up (database per test, sign-up/sign-in helpers) is in tests/conftest.py.
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


def test_chat_reply_includes_timing_and_is_recorded(env):
    c = new_browser(make_account=True)
    upload(c, [('PC0101.xlsx', workbook_bytes('PC0101'))])
    r = c.post(f'/api/patients/{record_key(c)}/ask', headers={'X-CSRF-Token': csrf(c)}, json={'question': 'How has my blood pressure changed?'}).get_json()
    t = r['timing']
    assert {'total_ms', 'analysis_ms', 'llm_ms', 'attempts'} <= set(t)
    assert t['total_ms'] >= t['analysis_ms'] >= 0
    assert {'answer', 'evidence', 'mode', 'guardrails', 'trace', 'latency_ms'} <= set(r)   # same response fields as before
    m = rows('SELECT * FROM chat_metrics')
    assert len(m) == 1 and m[0]['patient'] == record_key(c) and m[0]['mode'] == 'offline' and m[0]['total_ms'] == t['total_ms']
    assert 'blood pressure' not in json.dumps([dict(x) for x in m]).lower()   # no question text is stored


def test_screened_questions_are_timed_too(env):
    c = new_browser(make_account=True)
    upload(c, [('PC0101.xlsx', workbook_bytes('PC0101'))])
    r = c.post(f'/api/patients/{record_key(c)}/ask', headers={'X-CSRF-Token': csrf(c)}, json={'question': 'Do I have diabetes?'}).get_json()
    assert r['mode'] == 'screened' and r['timing']['total_ms'] >= 0
    assert rows('SELECT mode FROM chat_metrics')[0]['mode'] == 'screened'


class _Provider(BaseHTTPRequestHandler):
    """Mock OpenAI-compatible and Ollama endpoints that record every request."""
    requests = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        _Provider.requests.append((self.path, body))
        if self.path.endswith('/api/generate'):
            out = {'done': True}
        else:
            user = next(m['content'] for m in body['messages'] if m['role'] == 'user')
            first_id = next(ln.split(' ')[0] for ln in user.splitlines() if ln[:1] == 'E' and ln[1:2].isdigit())
            content = json.dumps({'answer': 'Your readings changed a little over the record.', 'evidence': [first_id]})
            out = {'message': {'content': content}} if self.path.endswith('/api/chat') else {'choices': [{'message': {'content': content}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def provider():
    _Provider.requests = []
    srv = HTTPServer(('127.0.0.1', 0), _Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown(); srv.server_close()


@pytest.fixture(scope='module')
def a101():
    import pandas as pd
    from healthlens import analyze_book
    with zipfile.ZipFile(ROOT / 'patient_data.zip') as z:
        raw = z.read(next(n for n in z.namelist() if n.endswith('/PC0101.xlsx')))
    return analyze_book(pd.read_excel(io.BytesIO(raw), sheet_name=None, engine='openpyxl'))


def test_llm_receives_exactly_the_original_prompt(a101, provider, monkeypatch):
    """The model gets the same system prompt, full record context and task as before, with no length cap,
    and the answer passed back is exactly what the model wrote."""
    monkeypatch.setenv('HEALTHLENS_LLM_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    monkeypatch.setenv('OPENAI_BASE_URL', f'http://127.0.0.1:{provider.server_port}/v1')
    q = 'How has my blood pressure changed?'
    r = llm.ask(a101, q)
    path, body = _Provider.requests[0]
    assert set(body) == {'model', 'temperature', 'messages'} and body['temperature'] == 0.2
    assert body['messages'] == [{'role': 'system', 'content': llm.HEALTHLENS_SYSTEM_PROMPT},
                                {'role': 'system', 'content': llm.HEALTHLENS_SYSTEM_PROMPT},
                                {'role': 'user', 'content': f'{llm.build_context(a101)}\n\n{llm.ASK_TASK}\n<question>{q}</question>'}]
    assert r['mode'] == 'llm' and r['answer'] == 'Your readings changed a little over the record.'
    llm.ask(a101, q)
    assert len(_Provider.requests) == 2                                      # every question goes to the model, as before


def test_ollama_request_only_adds_keep_alive(a101, provider, monkeypatch):
    monkeypatch.setenv('HEALTHLENS_LLM_PROVIDER', 'ollama')
    monkeypatch.setenv('OLLAMA_HOST', f'http://127.0.0.1:{provider.server_port}')
    r = llm.ask(a101, 'Is my sleep related to my stress?')
    path, body = _Provider.requests[-1]
    assert path == '/api/chat' and r['mode'] == 'llm'
    assert body['options'] == {'temperature': 0.2} and body['format'] == 'json' and body['stream'] is False
    assert body['keep_alive'] == llm.KEEP_ALIVE                              # keeps the model in memory (speed only)
    assert set(body) == {'model', 'stream', 'format', 'options', 'keep_alive', 'messages'}


def test_warm_up_loads_ollama_once_and_skips_other_providers(provider, monkeypatch):
    monkeypatch.setenv('HEALTHLENS_LLM_PROVIDER', 'offline')
    assert llm.warm_up(force=True) is False
    monkeypatch.setenv('HEALTHLENS_LLM_PROVIDER', 'ollama')
    monkeypatch.setenv('OLLAMA_HOST', f'http://127.0.0.1:{provider.server_port}')
    llm._warm['at'] = 0.0
    assert llm.warm_up() is True and llm.warm_up() is False                 # at most once every 10 minutes
    for _ in range(50):
        if _Provider.requests: break
        threading.Event().wait(0.05)
    path, body = _Provider.requests[0]
    assert path == '/api/generate' and body == {'model': llm.config()['model'], 'prompt': '', 'keep_alive': llm.KEEP_ALIVE}
