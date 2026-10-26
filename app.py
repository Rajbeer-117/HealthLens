import io, zipfile, sqlite3, secrets, hashlib, hmac, re, os
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort

from healthlens import analyze_book
from healthlens import llm
from healthlens.data import clean_sheet

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.environ.get('HEALTHLENS_SECRET_KEY') or secrets.token_hex(32)  # local demo; resets on restart
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=65*1024*1024)
DB = ROOT / 'users.sqlite3'
DATA = ROOT / 'patient_data.zip'
UPLOADS = ROOT / 'local_uploads'  # Local demonstration only; do not upload identifiable health data.
MAX_BOOK = 6*1024*1024
MAX_FILES = 120

def db():
    conn=sqlite3.connect(DB)
    conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL, email TEXT UNIQUE COLLATE NOCASE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)')
    conn.commit()
    return conn

def hash_password(p):
    salt=secrets.token_bytes(16)
    rounds=310000
    return f'{rounds}${salt.hex()}${hashlib.pbkdf2_hmac("sha256",p.encode(),salt,rounds).hex()}'

def verify(p,stored):
    try:
        rounds,salt,digest=stored.split('$')
        result=hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(salt),int(rounds))
        return hmac.compare_digest(result,bytes.fromhex(digest))
    except (ValueError,TypeError): return False

def login_required(fn):
    @wraps(fn)
    def inner(*args,**kwargs):
        if not session.get('uid'): return redirect(url_for('login'))
        return fn(*args,**kwargs)
    return inner

@app.route('/')
def home(): return redirect(url_for('dashboard' if session.get('uid') else 'login'))

@app.route('/register',methods=['GET','POST'])
def register():
    if request.method=='POST':
        username=request.form.get('username','').strip()
        email=request.form.get('email','').strip().lower()
        password=request.form.get('password','')
        confirm=request.form.get('confirm','')
        error=None
        if not re.fullmatch(r'[A-Za-z0-9_]{3,30}',username): error='Username must be 3–30 letters, numbers or underscores.'
        elif not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email): error='Enter a valid email.'
        elif not 10 <= len(password) <= 128: error='Password must be 10–128 characters.'
        elif password!=confirm: error='Passwords do not match.'
        if error: flash(error,'error')
        else:
            try:
                with db() as conn:
                    conn.execute('INSERT INTO users(username,email,password_hash,created_at) VALUES(?,?,?,?)',(username,email,hash_password(password),datetime.now(timezone.utc).isoformat()))
                flash('Account created. Please sign in.','success')
                return redirect(url_for('login'))
            except sqlite3.IntegrityError: flash('Username or email already exists.','error')
    return render_template('register.html')

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        identity=request.form.get('identity','').strip()
        password=request.form.get('password','')
        with db() as conn:
            user=conn.execute('SELECT * FROM users WHERE username=? OR email=?',(identity,identity)).fetchone()
        if user and verify(password,user['password_hash']):
            session.clear()
            session['uid']=user['id']; session['username']=user['username']
            return redirect(url_for('dashboard'))
        flash('Incorrect email/username or password.','error')
    return render_template('login.html')

@app.context_processor
def current_account():
    if not session.get('uid'):
        return {'account': None}
    with db() as conn:
        user=conn.execute('SELECT username, email FROM users WHERE id=?',(session['uid'],)).fetchone()
    return {'account': dict(user) if user else None}

@app.route('/logout',methods=['POST'])
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

def patient_members():
    if not DATA.exists(): return {}
    with zipfile.ZipFile(DATA) as z:
        return {Path(n).stem:n for n in z.namelist() if n.lower().endswith('.xlsx') and not n.startswith('__MACOSX')}

def user_upload_dir():
    return UPLOADS / str(int(session['uid']))

def uploaded_members():
    folder=user_upload_dir()
    return {f.stem:f for f in folder.glob('*.xlsx') if f.is_file()} if folder.exists() else {}

def validate_workbook(raw):
    if len(raw)>MAX_BOOK: raise ValueError('Each workbook must be 6 MB or smaller.')
    try:
        book=pd.read_excel(io.BytesIO(raw),sheet_name=None,engine='openpyxl')
    except Exception as exc:
        raise ValueError('The selected file is not a readable XLSX workbook.') from exc
    if 'Patient Profile' not in book: raise ValueError('Workbook must include a Patient Profile sheet.')
    if not any(k in book for k in ('Lab Results','Vitals Daily','Wearable Daily')):
        raise ValueError('Workbook must include Lab Results, Vitals Daily, or Wearable Daily.')
    return book

@app.route('/upload-patients', methods=['POST'])
@login_required
def upload_patients():
    if request.form.get('csrf')!=session.get('upload_csrf'):
        flash('Upload verification failed. Refresh and try again.','error')
        return redirect(url_for('dashboard'))
    incoming=request.files.get('patient_file')
    if not incoming or not incoming.filename:
        flash('Choose an XLSX workbook or ZIP archive.','error')
        return redirect(url_for('dashboard'))
    filename=Path(incoming.filename).name
    ext=Path(filename).suffix.lower()
    if ext not in ('.xlsx','.zip'):
        flash('Only .xlsx or .zip files are supported.','error')
        return redirect(url_for('dashboard'))
    raw=incoming.read()
    try:
        candidates=[]
        if ext=='.xlsx':
            candidates=[(filename,raw)]
        else:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries=[entry for entry in archive.infolist() if not entry.is_dir() and entry.filename.lower().endswith('.xlsx') and not Path(entry.filename).name.startswith('~$')]
                if not entries or len(entries)>MAX_FILES: raise ValueError('ZIP must contain 1–120 XLSX patient workbooks.')
                if any(e.file_size>MAX_BOOK for e in entries): raise ValueError('A workbook exceeds the 6 MB limit.')
                candidates=[(Path(e.filename).name,archive.read(e)) for e in entries]
        prepared=[]
        for name,data in candidates:
            stem=Path(name).stem
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',stem): raise ValueError('Patient filenames must contain only letters, numbers, underscores or hyphens.')
            validate_workbook(data)
            prepared.append((stem,data))
        if len({stem for stem,_ in prepared})!=len(prepared): raise ValueError('Duplicate patient filenames in ZIP.')
        folder=user_upload_dir()
        folder.mkdir(parents=True,exist_ok=True)
        for stem,data in prepared: (folder/(stem+'.xlsx')).write_bytes(data)
        flash(f'Imported {len(prepared)} patient workbook(s). Choose a patient to view their charts and warnings.','success')
        return redirect(url_for('dashboard',source='uploaded',patient=prepared[0][0]))
    except (ValueError,zipfile.BadZipFile,RuntimeError) as exc:
        flash(str(exc),'error')
        return redirect(url_for('dashboard'))

def load_book(member):
    with zipfile.ZipFile(DATA) as z: raw=z.read(member)
    return pd.read_excel(io.BytesIO(raw),sheet_name=None,engine='openpyxl')

def prepare(frame):
    frame=frame.copy()
    if 'Date' in frame.columns:
        col=frame['Date']
        numeric=pd.to_numeric(col,errors='coerce')
        excel=pd.to_datetime(numeric,unit='D',origin='1899-12-30',errors='coerce')
        ordinary=pd.to_datetime(col,errors='coerce')
        frame['Date']=excel.where(numeric.notna(),ordinary)
        frame=frame.dropna(subset=['Date']).sort_values('Date')
    return frame

def safe_rows(frame,limit=150):
    """Table preview: dates shown as YYYY-MM-DD (Excel serials converted), blanks shown empty."""
    clean=clean_sheet(frame).head(limit)
    def cell(v):
        if v is None or (not isinstance(v,str) and pd.isna(v)): return ''
        if isinstance(v,pd.Timestamp): return v.strftime('%Y-%m-%d')
        return str(v)
    return {'columns':[str(x) for x in clean.columns],'rows':[[cell(v) for v in row] for row in clean.itertuples(index=False,name=None)]}

# Deliverable 1 diagnosis-history reader (still used by the Diagnosis History tab)
def history_events(book):
    events=[]
    for sheet_name,frame in book.items():
        if not any(term in sheet_name.lower() for term in ('diagnos','condition','medical history','problem')): continue
        clean=prepare(frame)
        for _,row in clean.head(100).iterrows():
            fields={str(k):str(v) for k,v in row.items() if pd.notna(v)}
            if not fields: continue
            date=fields.get('Date',fields.get('Diagnosis Date','Not dated'))
            detail=' · '.join(f'{k}: {v}' for k,v in fields.items() if k not in ('Date','Diagnosis Date'))
            events.append({'date':str(date)[:10],'source':sheet_name,'detail':detail[:450]})
    return sorted(events,key=lambda x:x['date'],reverse=True)[:100]

# ---------------------------------------------------------------- Deliverable 2: analysis engine + AI layer

VITAL_RULES = {'sbp': (90, 119), 'dbp': (60, 79), 'rhr': (60, 100)}  # illustrative adult ranges, not clinical rules


def get_book(source, pid):
    """Return (workbook dict, cache key) for a demo or uploaded patient, or (None, None)."""
    if source == 'uploaded':
        members = uploaded_members()
        if pid not in members: return None, None
        path = members[pid]
        return (lambda: pd.read_excel(path, sheet_name=None, engine='openpyxl')), f'u{session["uid"]}:{pid}:{path.stat().st_mtime}'
    members = patient_members()
    if pid not in members: return None, None
    return (lambda: load_book(members[pid])), f'demo:{pid}'


_analysis_cache = {}


def get_analysis(source, pid):
    loader, key = get_book(source, pid)
    if loader is None: return None, None
    if key not in _analysis_cache:
        book = loader()
        if len(_analysis_cache) > 64: _analysis_cache.pop(next(iter(_analysis_cache)))
        _analysis_cache[key] = (book, analyze_book(book))
    return _analysis_cache[key]


def health_flags_v2(analysis):
    """Latest-value review flags: lab values use the workbook's own reference range; vitals use illustrative ranges."""
    flags = []
    for f in analysis['lab_flags']:
        if f['state'] == 'within': continue
        flags.append({'metric': f['label'], 'value': f'{f["latest"]:g}', 'unit': f['unit'], 'date': f['date'],
                      'reference': f['reference'], 'severity': 'Review', 'evidence': f['evidence'],
                      'message': f'Latest result is {f["state"]} the reference range listed in the workbook ({f["outside"]} of {f["draws"]} draws outside). Discuss with a qualified clinician; interpretation depends on your health context.'})
    for key, (low, high) in VITAL_RULES.items():
        c = analysis['charts'].get(key)
        if not c or not c['points']: continue
        last = c['points'][-1]
        if last['value'] < low or last['value'] > high:
            flags.append({'metric': c['label'], 'value': f'{last["value"]:g}', 'unit': c['unit'], 'date': last['date'],
                          'reference': f'{low}–{high}', 'severity': 'Review', 'evidence': None,
                          'message': 'Latest daily reading is outside an illustrative adult range. A single reading varies day to day; see the Trends tab for the longer pattern.'})
    return flags


def csrf_token():
    return session.setdefault('upload_csrf', secrets.token_urlsafe(24))


def api_auth(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if not session.get('uid'): return jsonify({'error': 'Not signed in'}), 401
        if request.method == 'POST' and request.headers.get('X-CSRF-Token') != session.get('upload_csrf'):
            return jsonify({'error': 'CSRF check failed. Refresh the page.'}), 400
        return fn(*args, **kwargs)
    return inner


def _source(value):
    return value if value in ('demo', 'uploaded') else 'demo'


@app.route('/dashboard')
@login_required
def dashboard():
    source = _source(request.args.get('source', 'demo'))
    members = uploaded_members() if source == 'uploaded' else patient_members()
    empty = dict(username=session['username'], patients=[], selected='', source=source, upload_csrf=csrf_token(),
                 profile={}, flags=[], history=[], meds={'columns': [], 'rows': []}, sheets={}, a=None, ai=llm.status())
    if not members:
        return render_template('dashboard.html', error=('No uploaded patient workbooks yet. Upload an XLSX file or ZIP archive below.' if source == 'uploaded' else 'patient_data.zip not found or contains no .xlsx workbooks'), **empty)
    ids = sorted(members)
    selected = request.args.get('patient', 'PC0101')
    if selected not in members: selected = ids[0]
    book, analysis = get_analysis(source, selected)
    profile = book.get('Patient Profile', pd.DataFrame())
    info = dict(zip(profile.get('Field', []), profile.get('Value', [])))
    return render_template('dashboard.html', username=session['username'], error=None, patients=ids, selected=selected,
                           source=source, upload_csrf=csrf_token(), profile=info, flags=health_flags_v2(analysis),
                           history=history_events(book), meds=safe_rows(book.get('Medication History', pd.DataFrame())),
                           sheets={k: safe_rows(v) for k, v in book.items()}, a=analysis, ai=llm.status())


@app.route('/ai')
@login_required
def ai_page():
    """AI Integration page: live LLM status, connection test, pipeline, guardrails and audit log."""
    return render_template('ai.html', username=session['username'], ai=llm.status(), upload_csrf=csrf_token(),
                           audit=llm.read_audit(30), system_prompt=llm.SYSTEM_PROMPT,
                           guardrails=sorted({name for name, _ in llm.GUARDRAILS}),
                           screens=[k for k, _, _ in llm.SCREENS])


@app.context_processor
def ai_badge():
    return {'ai_status': llm.status() if session.get('uid') else None}


# ---------------------------------------------------------------- REST API

@app.route('/api/llm/test', methods=['POST'])
@api_auth
def api_llm_test():
    result = llm.test_connection()
    llm.audit(session['uid'], '-', 'connection-test', {**result, 'mode': 'llm' if result['ok'] else 'error',
                                                         'guardrails': {'passed': None, 'issues': []}})
    return jsonify(result)


@app.route('/api/llm/status')
@api_auth
def api_llm_status():
    return jsonify(llm.status())


@app.route('/api/patients')
@api_auth
def api_patients():
    source = _source(request.args.get('source', 'demo'))
    members = uploaded_members() if source == 'uploaded' else patient_members()
    return jsonify({'source': source, 'patients': sorted(members)})


@app.route('/api/patients/<pid>/analysis')
@api_auth
def api_analysis(pid):
    _, analysis = get_analysis(_source(request.args.get('source', 'demo')), pid)
    if analysis is None: return jsonify({'error': 'Patient not found'}), 404
    return jsonify(analysis)


@app.route('/api/patients/<pid>/ai-summary', methods=['POST'])
@api_auth
def api_ai_summary(pid):
    body = request.get_json(silent=True) or {}
    _, analysis = get_analysis(_source(body.get('source', 'demo')), pid)
    if analysis is None: return jsonify({'error': 'Patient not found'}), 404
    result = llm.summarize(analysis)
    llm.audit(session['uid'], pid, 'ai-summary', result)
    return jsonify(result)


@app.route('/api/patients/<pid>/ask', methods=['POST'])
@api_auth
def api_ask(pid):
    body = request.get_json(silent=True) or {}
    question = str(body.get('question', '')).strip()
    if not 3 <= len(question) <= 500: return jsonify({'error': 'Ask a question between 3 and 500 characters.'}), 400
    _, analysis = get_analysis(_source(body.get('source', 'demo')), pid)
    if analysis is None: return jsonify({'error': 'Patient not found'}), 404
    result = llm.ask(analysis, question)
    llm.audit(session['uid'], pid, 'ask', result)
    return jsonify(result)


if __name__=='__main__':
    app.run(debug=True,host='127.0.0.1',port=5000)
