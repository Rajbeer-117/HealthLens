import io, zipfile, sqlite3, secrets, hashlib, hmac, re, os, sys, getpass, time, json
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from email.message import Message
from functools import wraps
from pathlib import Path
import numpy as np
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort, send_file

from healthlens import analyze_book
from healthlens import llm
from healthlens.data import clean_sheet, merge_workbooks
from healthlens.extract import extract
from healthlens import risk, risk_model, record_rules
from healthlens.data import METRICS

from pathlib import PurePosixPath
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=65*1024*1024)
DB = ROOT / 'users.sqlite3'       # accounts, uploaded files (stored as BLOBs), each user's last view, chatbot timings
UPLOADS = ROOT / 'local_uploads'  # Only read to migrate files saved on disk by older versions; new uploads go into the database.
MAX_BOOK = 6*1024*1024          # one patient workbook
MAX_FILES = 120                 # workbooks inside one ZIP
MAX_DOC = 15*1024*1024          # one supporting document (PDF, Word, image ...)
MAX_REMOTE = 60*1024*1024       # one file imported from Google Drive
MAX_PER_UPLOAD = 20             # files in one upload request
# Supporting documents are stored and can be downloaded, but are not analysed.
DOC_TYPES = {'.pdf': 'PDF', '.docx': 'Word', '.txt': 'Text', '.csv': 'CSV', '.png': 'Image', '.jpg': 'Image', '.jpeg': 'Image'}

def clean_upload_filename(filename):
    normalized = str(filename).replace("\\", "/")
    basename = PurePosixPath(normalized).name
    return secure_filename(basename)

class ClosingConnection(sqlite3.Connection):
    """'with db() as conn:' saves (or undoes) the changes AND closes the connection afterwards.
    Plain sqlite3 only saves, leaving the file open ('ResourceWarning: unclosed database')."""
    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()

def db():
    """The database: the local file users.sqlite3."""
    return sqlite_db()

def sqlite_db():
    """The local database file (default). Creates or upgrades its tables."""
    conn=sqlite3.connect(DB, factory=ClosingConnection)
    conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL, email TEXT UNIQUE COLLATE NOCASE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, original_name TEXT NOT NULL, stored_name TEXT NOT NULL, kind TEXT NOT NULL, patient_id TEXT, size INTEGER NOT NULL, origin TEXT NOT NULL, created_at TEXT NOT NULL)')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    # Each user's last viewed patient and data span, so the same report opens after signing in on any computer.
    conn.execute('CREATE TABLE IF NOT EXISTS user_state (user_id INTEGER PRIMARY KEY, patient TEXT, span TEXT, updated_at TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_metrics (id INTEGER PRIMARY KEY, user_id INTEGER, patient TEXT, mode TEXT,
                    total_ms INTEGER, llm_ms INTEGER, attempts INTEGER, cached INTEGER, prompt_chars INTEGER, created_at TEXT NOT NULL)''')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
    # Team (admin) access is kept in its own table, separate from patient accounts. Older databases stored it in a
    # users.is_admin column: those flags are moved here once and the column is removed.
    conn.execute('''CREATE TABLE IF NOT EXISTS team_admins (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    granted_at TEXT NOT NULL)''')
    if 'is_admin' in columns:
        conn.execute("INSERT OR IGNORE INTO team_admins(user_id, granted_at) SELECT id, ? FROM users WHERE is_admin=1",
                     (datetime.now(timezone.utc).isoformat(timespec='seconds'),))
        try: conn.execute('ALTER TABLE users DROP COLUMN is_admin')
        except sqlite3.OperationalError: pass          # very old SQLite: the column stays but is no longer used
    doc_cols = {r[1] for r in conn.execute('PRAGMA table_info(documents)')}
    upgrades = {'content': 'ALTER TABLE documents ADD COLUMN content BLOB',          # the uploaded file itself
                'doc_type': 'ALTER TABLE documents ADD COLUMN doc_type TEXT',      # workbook | table | report | image | other
                'readings': 'ALTER TABLE documents ADD COLUMN readings INTEGER',   # health readings found in the document
                'note': 'ALTER TABLE documents ADD COLUMN note TEXT',              # what was found (Documents page)
                'sha256': 'ALTER TABLE documents ADD COLUMN sha256 TEXT',          # fingerprint: same file not stored twice
                'facts_done': 'ALTER TABLE documents ADD COLUMN facts_done INTEGER'}  # identifiers/attributes/observations written
    for col in upgrades:
        if col not in doc_cols:
            conn.execute(upgrades[col])
    # Documents left 'waiting for the patient's answer' by an earlier version: the backend now decides, and nothing
    # from them was ever added, so they are kept as files only.
    conn.execute("UPDATE documents SET kind='document', note='Not added: it could not be matched to your record.' "
                 "WHERE kind='pending'")
    # A patient's record is built document by document: each upload adds its readings to health_records,
    # linked to the patient and to the document they came from.
    conn.execute('''CREATE TABLE IF NOT EXISTS patients (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, patient_code TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(user_id, patient_code))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS health_records (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, patient_code TEXT NOT NULL,
                    document_id INTEGER NOT NULL, sheet TEXT NOT NULL, record_date TEXT, data TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS health_records_patient ON health_records (user_id, patient_code)')
    conn.execute('CREATE INDEX IF NOT EXISTS health_records_document ON health_records (document_id)')
    for statement in RECORD_SCHEMA_SQLITE:
        conn.execute(statement)
    conn.commit()
    return conn

# ---------------------------------------------------------------- record design (see DATABASE_DESIGN.md)
# health_records keeps every extracted row as it was read (raw layer); the tables below hold the facts in a form
# that can be checked and queried. Every fact points to the document it came from, so deleting a document removes
# exactly what it contributed.
RECORD_SCHEMA_SQLITE = [
    # Patient IDs printed in documents (labs and clinics each issue their own). Not the key of the record.
    '''CREATE TABLE IF NOT EXISTS patient_identifiers (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, patient_code TEXT NOT NULL,
       value TEXT NOT NULL, source_document_id INTEGER, confirmed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
       UNIQUE(user_id, patient_code, value))''',
    # Sex and date of birth, from the patient ('user', confirmed) or from a document ('document').
    '''CREATE TABLE IF NOT EXISTS patient_attributes (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, patient_code TEXT NOT NULL,
       name TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL, source_document_id INTEGER, recorded_at TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS patient_attributes_patient ON patient_attributes (user_id, patient_code, name)',
    # Catalogue of measures and which risk categories use them.
    'CREATE TABLE IF NOT EXISTS measures (code TEXT PRIMARY KEY, name TEXT NOT NULL, unit TEXT NOT NULL, kind TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS measure_categories (measure_code TEXT NOT NULL REFERENCES measures(code), category TEXT NOT NULL,
       PRIMARY KEY (measure_code, category))''',
    # One row per measurement: the patient had this value, at this time, according to this document.
    '''CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, patient_code TEXT NOT NULL,
       document_id INTEGER NOT NULL, measure_code TEXT NOT NULL REFERENCES measures(code), value REAL NOT NULL, unit TEXT,
       measured_at TEXT NOT NULL, ref_low REAL, ref_high REAL)''',
    'CREATE INDEX IF NOT EXISTS observations_patient ON observations (user_id, patient_code, measure_code, measured_at)',
    'CREATE INDEX IF NOT EXISTS observations_document ON observations (document_id)',
    # Risk scores are derived: a snapshot each time the set of documents changes, with the model that made it.
    '''CREATE TABLE IF NOT EXISTS risk_assessments (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, patient_code TEXT NOT NULL,
       category TEXT NOT NULL, score INTEGER, level TEXT, model_version TEXT, inputs_used TEXT, inputs_missing TEXT,
       factors TEXT, documents TEXT NOT NULL, computed_at TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS risk_assessments_patient ON risk_assessments (user_id, patient_code, computed_at)',
]


def _secret_key():
    """Session signing key. Kept in the database so sign-ins survive a restart of the app."""
    if os.environ.get('HEALTHLENS_SECRET_KEY'): return os.environ['HEALTHLENS_SECRET_KEY']
    with db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key='secret_key'").fetchone()
        if row: return row['value']
        key = secrets.token_hex(32)
        conn.execute("INSERT INTO app_settings(key,value) VALUES('secret_key',?)", (key,))
        return key

app.secret_key = _secret_key()

def load_state(uid):
    with db() as conn:
        row = conn.execute('SELECT patient, span FROM user_state WHERE user_id=?', (uid,)).fetchone()
    return {'patient': row['patient'] or '', 'span': row['span'] or DEFAULT_SPAN} if row else {}

def save_state(uid, patient, span):
    with db() as conn:
        conn.execute('''INSERT INTO user_state(user_id,patient,span,updated_at) VALUES(?,?,?,?)
                        ON CONFLICT(user_id) DO UPDATE SET patient=excluded.patient, span=excluded.span, updated_at=excluded.updated_at''',
                     (uid, patient, span, datetime.now(timezone.utc).isoformat(timespec='seconds')))

def audit_event(action, detail='', user_id=None, username=None):
    """Minimal audit metadata only; no passwords, OTPs or patient measurements."""
    try:
        with db() as conn:
            conn.execute('INSERT INTO audit_events(user_id,username,action,detail,created_at) VALUES(?,?,?,?,?)',
                         (user_id if user_id is not None else session.get('uid'),
                          username if username is not None else session.get('username'),
                          action, str(detail)[:200], datetime.now(timezone.utc).isoformat(timespec='seconds')))
    except sqlite3.Error:
        app.logger.exception('Unable to write audit event')

def is_team_admin(uid):
    """Checked in the database on every request (never trusted from the browser or the session)."""
    if not uid: return False
    with db() as conn:
        return conn.execute('SELECT 1 FROM team_admins WHERE user_id=?', (int(uid),)).fetchone() is not None

def admin_required(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if not is_team_admin(session.get('uid')): abort(404)   # same "not found" page as any address that does not exist
        return fn(*args, **kwargs)
    return inner

@app.context_processor
def admin_nav_context():
    return {'is_admin': is_team_admin(session.get('uid'))}

@app.route('/admin')
@admin_required
def admin_dashboard():
    """System dashboard for the team. Patients never see a link to it and get "not found" if they try the address."""
    page = max(1, min(request.args.get('page', 1, type=int), 100000))
    limit = 30
    with db() as conn:
        stats = system_stats(conn)
        events = conn.execute('SELECT * FROM audit_events ORDER BY id DESC LIMIT ? OFFSET ?', (limit, (page-1)*limit)).fetchall()
        users = conn.execute('''SELECT u.id, u.username, u.created_at, (t.user_id IS NOT NULL) AS is_admin,
                                  (SELECT MAX(created_at) FROM audit_events e WHERE e.user_id=u.id AND e.action='login') AS last_login,
                                  (SELECT COUNT(*) FROM documents d WHERE d.user_id=u.id) AS files
                                FROM users u LEFT JOIN team_admins t ON t.user_id=u.id ORDER BY u.id DESC LIMIT 15''').fetchall()
        chat_times = conn.execute('SELECT * FROM chat_metrics ORDER BY id DESC LIMIT 20').fetchall()
        accounts = conn.execute('''SELECT u.id, u.username, t.granted_at, (t.user_id IS NOT NULL) AS is_admin FROM users u
                                   LEFT JOIN team_admins t ON t.user_id=u.id
                                   ORDER BY (t.user_id IS NULL), LOWER(u.username), u.id''').fetchall()
    return render_template('admin.html', stats=stats, events=events, users=users, chat_times=chat_times, llm_status=llm.status(),
                           storage_place='local file users.sqlite3',
                           accounts=accounts, me=session['uid'], csrf=csrf_token(),
                           page=page, pages=max(1,(stats['events']+limit-1)//limit))

@app.route('/admin/team', methods=['POST'])
@admin_required
def admin_team_access():
    """An existing team member gives or removes team access for another account, confirming with their OWN password.
    Patients cannot reach this (404), and nobody can remove their own access, so the team cannot lock itself out."""
    if not csrf_ok(request.form.get('csrf')):
        flash('Verification failed. Refresh the page and try again.', 'error')
        return redirect(url_for('admin_dashboard'))
    action = request.form.get('action')
    target = request.form.get('user_id', type=int)
    with db() as conn:
        me = conn.execute('SELECT password_hash FROM users WHERE id=?', (session['uid'],)).fetchone()
        user = conn.execute('SELECT id, username FROM users WHERE id=?', (target,)).fetchone() if target else None
    if action not in ('grant', 'remove') or not user:
        flash('Choose an account first.', 'error')
    elif not me or not verify(request.form.get('password', ''), me['password_hash']):
        audit_event('team_access_denied', 'Wrong password when changing team access')
        flash('Your password was incorrect. No changes were made.', 'error')
    elif action == 'remove' and user['id'] == session['uid']:
        flash('You cannot revoke your own team access. Ask another team member.', 'error')
    else:
        with db() as conn:
            if action == 'grant':
                conn.execute('INSERT INTO team_admins(user_id, granted_at) VALUES(?,?) ON CONFLICT(user_id) DO NOTHING',
                             (user['id'], datetime.now(timezone.utc).isoformat(timespec='seconds')))
            else:
                conn.execute('DELETE FROM team_admins WHERE user_id=?', (user['id'],))
        done = f'Team access {"granted to" if action == "grant" else "revoked for"} {user["username"]}'
        audit_event('team_grant' if action == 'grant' else 'team_remove', done)
        flash(done + '.', 'success')
    return redirect(url_for('admin_dashboard') + '#team-access')

def set_admin(username, password, make_admin=True):
    """Give or remove team (admin) access from the command line on the computer running the app (needed once, for the
    first team member), using that account's password. After that, team members can use the System dashboard."""
    with db() as conn:
        user = conn.execute('SELECT id,password_hash FROM users WHERE LOWER(username)=LOWER(?)', (username,)).fetchone()
        if not user: return 'No such registered username.'
        if not verify(password, user['password_hash']): return 'Incorrect password; no changes made.'
        if make_admin:
            conn.execute('INSERT INTO team_admins(user_id, granted_at) VALUES(?,?) ON CONFLICT(user_id) DO NOTHING',
                         (user['id'], datetime.now(timezone.utc).isoformat(timespec='seconds')))
        else:
            conn.execute('DELETE FROM team_admins WHERE user_id=?', (user['id'],))
    return f'Admin access {"enabled" if make_admin else "removed"} for: {username}'

def to_local(value):
    """Times are stored in UTC (correct for a database shared across places); show them in this computer's time zone."""
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone()

@app.template_filter('localtime')
def localtime_filter(value, fmt='%Y-%m-%d %H:%M'):
    moment = to_local(value)
    return moment.strftime(fmt) if moment else (value or '—')

def _secs(ms):
    if ms is None: return '—'
    return f'{ms / 1000:.2f} s' if ms < 1000 else f'{ms / 1000:.1f} s'

def _percentile(values, p):
    if not values: return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]

def system_stats(conn):
    """Statistics for the team's System dashboard. Counts and timings only: no health readings, no question text."""
    now = datetime.now(timezone.utc)
    week = (now - timedelta(days=7)).isoformat(timespec='seconds')
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0
    s = {
        'users': one('SELECT COUNT(*) FROM users'),
        'admins': one('SELECT COUNT(*) FROM team_admins'),
        'new_users_7d': one('SELECT COUNT(*) FROM users WHERE created_at >= ?', week),
        'signed_in_users': one("SELECT COUNT(DISTINCT user_id) FROM audit_events WHERE action='login'"),
        'active_users_7d': one("SELECT COUNT(DISTINCT user_id) FROM audit_events WHERE action='login' AND created_at >= ?", week),
        'sign_ins': one("SELECT COUNT(*) FROM audit_events WHERE action='login'"),
        'sign_ins_7d': one("SELECT COUNT(*) FROM audit_events WHERE action='login' AND created_at >= ?", week),
        'failed_7d': one("SELECT COUNT(*) FROM audit_events WHERE action='login_failed' AND created_at >= ?", week),
        'users_with_records': one("SELECT COUNT(DISTINCT user_id) FROM documents WHERE kind='patient'"),
        'patient_workbooks': one("SELECT COUNT(*) FROM documents WHERE kind='patient'"),
        'supporting_files': one("SELECT COUNT(*) FROM documents WHERE kind='document'"),
        'storage': one('SELECT SUM(size) FROM documents'),
        'events': one('SELECT COUNT(*) FROM audit_events'),
    }
    s['never_uploaded'] = max(0, s['users'] - s['users_with_records'])
    chats = conn.execute('SELECT mode, total_ms, cached FROM chat_metrics').fetchall()
    times = [r['total_ms'] for r in chats if r['total_ms'] is not None]
    ai_times = [r['total_ms'] for r in chats if r['mode'] == 'llm' and not r['cached'] and r['total_ms'] is not None]
    modes = {m: sum(1 for r in chats if r['mode'] == m) for m in ('llm', 'fallback', 'offline', 'screened')}
    s['chat'] = {'questions': len(chats), 'avg': _secs(sum(times) / len(times)) if times else '—',
                 'median': _secs(_percentile(times, 50)), 'p90': _secs(_percentile(times, 90)), 'slowest': _secs(max(times) if times else None),
                 'ai_avg': _secs(sum(ai_times) / len(ai_times)) if ai_times else '—',
                 'modes': modes, 'cached': sum(1 for r in chats if r['cached'])}
    # Days are counted in this computer's time zone (stored times are UTC), so a sign-in at 10 pm in Toronto counts today.
    today = now.astimezone().date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    since = (now - timedelta(days=15)).isoformat(timespec='seconds')
    def per_day(sql):
        counts = {}
        for r in conn.execute(sql, (since,)):
            moment = to_local(r[0])
            if moment:
                counts[moment.date().isoformat()] = counts.get(moment.date().isoformat(), 0) + 1
        return counts
    logins = per_day("SELECT created_at FROM audit_events WHERE action='login' AND created_at >= ?")
    signups = per_day("SELECT created_at FROM users WHERE created_at >= ?")
    uploads = per_day("SELECT created_at FROM audit_events WHERE action='upload' AND created_at >= ?")
    s['activity'] = {'days': [d[5:] for d in days], 'sign_ins': [logins.get(d, 0) for d in days],
                     'sign_ups': [signups.get(d, 0) for d in days], 'uploads': [uploads.get(d, 0) for d in days]}
    return s

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
def home():
    if not session.get('uid'): return redirect(url_for('login'))
    return redirect(url_for('admin_dashboard' if is_team_admin(session['uid']) else 'summary'))

@app.route('/register',methods=['GET','POST'])

def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        error = None

        if not re.fullmatch(r'[A-Za-z0-9_]{3,30}', username):
            error = 'Username must be 3–30 letters, numbers or underscores.'

        elif not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            error = 'Enter a valid email.'

        elif not 10 <= len(password) <= 128:
            error = 'Password must be 10–128 characters.'

        elif not re.search(r'[A-Z]', password):
            error = 'Password must contain at least one uppercase letter.'

        elif not re.search(r'[a-z]', password):
            error = 'Password must contain at least one lowercase letter.'

        elif not re.search(r'[0-9]', password):
            error = 'Password must contain at least one number.'

        elif not re.search(r'[^A-Za-z0-9]', password):
            error = 'Password must contain at least one special character.'

        elif password != confirm:
            error = 'Passwords do not match.'

        if error:
            flash(error, 'error')
        else:
            try:
                with db() as conn:
                    conn.execute(
                        'INSERT INTO users(username,email,password_hash,created_at) VALUES(?,?,?,?)',
                        (
                            username,
                            email,
                            hash_password(password),
                            datetime.now(timezone.utc).isoformat()
                        )
                    )

                audit_event('register', 'Account created', username=username)
                flash('Account created. Please sign in.', 'success')
                return redirect(url_for('login'))

            except sqlite3.IntegrityError:
                flash('Username or email already exists.', 'error')

        # Re-show the form with the username and email already filled in, so only the passwords need retyping.
        return render_template('register.html', form={'username': username, 'email': email})

    return render_template('register.html', form={})


@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        identity=request.form.get('identity','').strip()
        password=request.form.get('password','')
        with db() as conn:
            user=conn.execute('SELECT * FROM users WHERE LOWER(username)=LOWER(?) OR LOWER(email)=LOWER(?)',(identity,identity)).fetchone()
        if user and verify(password,user['password_hash']):
            session.clear()
            session['uid']=user['id']; session['username']=user['username']
            state = load_state(user['id'])                       # reopen the report this user viewed last, on any computer
            if state: session['active'] = state
            audit_event('login', 'Successful sign-in')
            # The role is decided by the database: team members go to the System dashboard, patients to their Summary.
            return redirect(url_for('admin_dashboard' if is_team_admin(user['id']) else 'summary'))
        audit_event('login_failed', 'Invalid credentials')
        flash('Incorrect email/username or password.','error')
        return render_template('login.html', identity=identity)
    return render_template('login.html', identity='')

@app.route('/logout',methods=['POST'])
@login_required
def logout():
    audit_event('logout', 'Signed out')
    session.clear()
    return redirect(url_for('login'))

def user_upload_dir():
    """Folder where older versions of the app saved this user's files (read only for migration)."""
    return UPLOADS / str(int(session['uid']))

def legacy_path(row):
    """On-disk location used by older versions for this document, or None if it would leave the user's folder."""
    base = user_upload_dir().resolve()
    path = (base / ('docs' if row['kind'] == 'document' else '') / row['stored_name']).resolve()
    return path if base in path.parents else None

def migrate_disk_uploads():
    """Move files saved on disk by older versions into the database, so they work on any computer.
    Workbooks found on disk but missing from the table are added; rows without content get it from disk."""
    folder = user_upload_dir()
    with db() as conn:
        if folder.exists():
            known = {r['patient_id'] for r in conn.execute("SELECT patient_id FROM documents WHERE user_id=? AND kind='patient'", (session['uid'],))}
            for path in sorted(folder.glob('*.xlsx')):
                if path.is_file() and path.stem not in known:
                    st = path.stat()
                    conn.execute('INSERT INTO documents(user_id,original_name,stored_name,kind,patient_id,size,origin,created_at,content) VALUES(?,?,?,?,?,?,?,?,?)',
                                 (session['uid'], path.name, path.name, 'patient', path.stem, st.st_size, 'Computer',
                                  datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec='seconds'), path.read_bytes()))
        for row in conn.execute('SELECT id,user_id,original_name,stored_name,kind,patient_id,size,origin,created_at FROM documents WHERE user_id=? AND content IS NULL', (session['uid'],)).fetchall():
            path = legacy_path(row)
            if path and path.is_file():
                conn.execute('UPDATE documents SET content=? WHERE id=?', (path.read_bytes(), row['id']))

def backfill_records():
    """Documents analysed by older versions have no rows in health_records yet: read them once."""
    with db() as conn:
        todo = conn.execute("""SELECT d.id, d.original_name, d.patient_id, d.content FROM documents d WHERE d.user_id=? AND d.kind='patient'
                               AND NOT EXISTS (SELECT 1 FROM health_records r WHERE r.document_id=d.id)""", (session['uid'],)).fetchall()
        for row in todo:
            ex = extract(row['original_name'] if row['original_name'].lower().endswith(('.xlsx', '.xlsm')) else row['original_name'] + '.xlsx',
                         bytes(row['content'] or b''))
            conn.execute('UPDATE documents SET doc_type=?, readings=?, note=?, sha256=? WHERE id=?',
                         (ex.kind, ex.readings, ex.note, hashlib.sha256(bytes(row['content'] or b'')).hexdigest(), row['id']))
            conn.execute('INSERT INTO patients(user_id, patient_code, created_at) VALUES(?,?,?) ON CONFLICT(user_id, patient_code) DO NOTHING',
                         (session['uid'], row['patient_id'], datetime.now(timezone.utc).isoformat(timespec='seconds')))
            for sheet, when, data in records_of(ex):
                conn.execute('INSERT INTO health_records(user_id, patient_code, document_id, sheet, record_date, data) VALUES(?,?,?,?,?,?)',
                             (session['uid'], row['patient_id'], row['id'], sheet, when, data))
        for row in conn.execute("SELECT id, content FROM documents WHERE user_id=? AND sha256 IS NULL", (session['uid'],)).fetchall():
            conn.execute('UPDATE documents SET sha256=? WHERE id=?', (hashlib.sha256(bytes(row['content'] or b'')).hexdigest(), row['id']))
    backfill_facts()

def backfill_facts():
    """Documents stored by earlier versions: write their observations, Patient IDs and sex / date of birth once,
    from the readings already in health_records."""
    with db() as conn:
        todo = conn.execute('SELECT id, kind, patient_id FROM documents WHERE user_id=? AND facts_done IS NULL', (session['uid'],)).fetchall()
    for row in todo:
        books = patient_documents(session['uid'], row['patient_id'], row['id']) if row['kind'] == 'patient' and row['patient_id'] else []
        if row['kind'] == 'patient' and not books:
            continue                                   # readings not read yet (e.g. file still on disk): try next visit
        with db() as conn:
            if books:
                store_facts(conn, row['id'], row['patient_id'], books[0])
            conn.execute('UPDATE documents SET facts_done=1 WHERE id=?', (row['id'],))

def user_patients():
    """{patient id: [document ids, oldest first]} for the signed-in user's analysed documents (stored in the database).
    A patient's record is built document by document (this month's blood work, last month's vitals...)."""
    migrate_disk_uploads()
    backfill_records()
    with db() as conn:
        rows = conn.execute("SELECT id, patient_id FROM documents WHERE user_id=? AND kind='patient' ORDER BY id", (session['uid'],)).fetchall()
    patients = {}
    for r in rows:
        patients.setdefault(r['patient_id'], []).append(r['id'])
    return patients

def document_content(doc_id):
    with db() as conn:
        row = conn.execute('SELECT content FROM documents WHERE id=? AND user_id=?', (doc_id, session['uid'])).fetchone()
    return row['content'] if row else None

def csrf_token():
    return session.setdefault('upload_csrf', secrets.token_urlsafe(24))

def csrf_ok(value):
    return bool(value) and hmac.compare_digest(str(value), session.get('upload_csrf', ''))

@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    return resp

# ---------------------------------------------------------------- file ingestion (Documents page)

def base_name(name):
    """Return a safe filename without directory components."""
    normalized = str(name).replace("\\", "/")
    basename = PurePosixPath(normalized).name

    # Remove control characters
    basename = re.sub(r'[\x00-\x1f\x7f]', '', basename)

    # Sanitize filename
    basename = secure_filename(basename)

    return basename[:120] or 'file'


def clean_stem(name):
    """Patient id taken from a workbook name. 'PC0101 (1).xlsx' -> 'PC0101_1'."""
    stem=re.sub(r'[^A-Za-z0-9_-]+','_',Path(name).stem).strip('_')[:60]
    if not stem: raise ValueError(f'"{name}" has no usable name. Rename it using letters and numbers.')
    return stem

def workbook_entries(filename, raw):
    """An .xlsx/.xlsm file, or the workbooks inside a .zip. Returns [(name, bytes)] or raises ValueError."""
    if Path(filename).suffix.lower() in ('.xlsx', '.xlsm'):
        if len(raw) > MAX_BOOK: raise ValueError(f'{filename}: each workbook must be 6 MB or smaller.')
        return [(filename, raw)]
    found = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = [e for e in archive.infolist() if not e.is_dir() and e.filename.lower().endswith(('.xlsx', '.xlsm'))
                   and not Path(e.filename).name.startswith('~$') and '__MACOSX' not in e.filename]
        if not entries or len(entries) > MAX_FILES: raise ValueError(f'{filename}: ZIP must contain 1–{MAX_FILES} XLSX patient workbooks.')
        for e in entries:
            with archive.open(e) as fh: data = fh.read(MAX_BOOK + 1)   # read at most the limit, whatever the header claims
            if len(data) > MAX_BOOK: raise ValueError(f'{filename}: "{Path(e.filename).name}" exceeds the 6 MB limit.')
            found.append((Path(e.filename).name, data))
    return found

def patient_for(name, ex, existing, several_allowed=False):
    """Whose record a document belongs to. The Patient ID written inside the document decides, not the file name,
    so 'bloodwork_march.pdf' and 'PC0101 (1).xlsx' both join patient PC0101.

    One account = one patient: the account's first document sets its record; a later document must carry the same
    Patient ID, or none (device exports have none: they join the record). A document for anyone else is refused.
    Team accounts (several_allowed) may hold several records, to test the provided 900-patient dataset."""
    written = clean_stem(ex.patient_id + '.x') if ex.patient_id else ''
    if existing and not several_allowed:
        if written and written not in existing:
            raise ValueError(f'{name}: belongs to patient {written}, not your record '
                             f'{", ".join(sorted(existing))}. Not added.')
        if written:
            return written
        if len(existing) == 1:
            return next(iter(existing))
    if written:
        return written
    if ex.kind == 'workbook' and 'Patient Profile' in ex.sheets:   # older HealthLens workbooks were named after the patient
        return clean_stem(name)
    if len(existing) == 1:
        return next(iter(existing))
    if not existing:
        return clean_stem((session.get('username') or 'patient') + '.x')
    raise ValueError(f'{name}: no Patient ID was found and your account has several patients ({", ".join(sorted(existing))}), '
                     'so HealthLens cannot tell whose record it is. Add the Patient ID to the document.')

def _jsonable(v):
    """A cell value as plain JSON (dates as YYYY-MM-DD, empty cells as null)."""
    if isinstance(v, np.generic): v = v.item()
    if v is None: return None
    if isinstance(v, (pd.Timestamp, datetime)):
        if pd.isna(v): return None
        return v.strftime('%Y-%m-%d') if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat()
    if isinstance(v, float) and v != v: return None
    if isinstance(v, (str, int, float, bool)): return v
    try:
        if pd.isna(v): return None
    except (TypeError, ValueError):
        pass
    return str(v)

def records_of(ex):
    """One row per reading: (sheet, date of the reading, values as JSON)."""
    out = []
    for sheet, frame in ex.sheets.items():
        if frame is None or not len(frame): continue
        date_col = next((c for c in ('Date', 'Start Date', 'Week Start', 'Month') if c in frame.columns), None)
        for row in frame.dropna(how='all').to_dict('records'):
            data = {str(k): _jsonable(v) for k, v in row.items()}
            when = data.get(date_col) if date_col else None
            out.append((sheet, str(when)[:10] if when is not None else None, json.dumps(data, ensure_ascii=False)))
    return out

def store_upload(conn, name, raw, kind, ex, pid, origin, note=None):
    """Save the original file (so it can be downloaded). For an accepted document ('patient') also every reading found
    in it (health_records, the raw layer) and the facts it states (observations, patient IDs, sex / date of birth),
    all linked to the patient and to this document."""
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    ext = Path(name).suffix.lower() or '.bin'
    doc_id = conn.execute('INSERT INTO documents(user_id,original_name,stored_name,kind,patient_id,size,origin,created_at,content,'
                          'doc_type,readings,note,sha256,facts_done) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1) RETURNING id',
                          (session['uid'], name, secrets.token_hex(12) + ext, kind, pid, len(raw), origin, now, raw,
                           ex.kind, ex.readings, ex.note if note is None else note, hashlib.sha256(raw).hexdigest())).fetchone()[0]
    if pid and kind == 'patient':
        store_readings(conn, doc_id, pid, ex)
    return doc_id

def store_readings(conn, doc_id, pid, ex, confirmed_id=False):
    """Add one accepted document's readings and facts to the patient's record."""
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    conn.execute('INSERT INTO patients(user_id, patient_code, created_at) VALUES(?,?,?) '
                 'ON CONFLICT(user_id, patient_code) DO NOTHING', (session['uid'], pid, now))
    for sheet, when, data in records_of(ex):
        conn.execute('INSERT INTO health_records(user_id, patient_code, document_id, sheet, record_date, data) VALUES(?,?,?,?,?,?)',
                     (session['uid'], pid, doc_id, sheet, when, data))
    store_facts(conn, doc_id, pid, ex.sheets, ex.patient_id, confirmed_id)

def ensure_catalog(conn):
    """Fill the measures catalogue (and which risk categories use each measure) when the database does not have it yet.
    Checked in the database itself, not remembered in memory, so a fill undone with a refused upload is simply redone."""
    if conn.execute('SELECT COUNT(*) FROM measures').fetchone()[0] >= len(METRICS):
        return
    for code, (label, _sheet, _col, unit, kind) in METRICS.items():
        conn.execute('INSERT INTO measures(code, name, unit, kind) VALUES(?,?,?,?) ON CONFLICT(code) DO NOTHING', (code, label, unit, kind))
    for category, codes in risk_model.CATEGORY_INPUTS.items():
        for code in codes:
            conn.execute('INSERT INTO measure_categories(measure_code, category) VALUES(?,?) ON CONFLICT(measure_code, category) DO NOTHING',
                         (code, category))

def store_facts(conn, doc_id, pid, sheets, patient_id='', confirmed_id=False):
    """Observations (one row per measurement), the Patient ID and the name / sex / date of birth this document states."""
    ensure_catalog(conn)
    uid, now = session['uid'], datetime.now(timezone.utc).isoformat(timespec='seconds')
    for o in record_rules.observations_of(sheets):
        conn.execute('INSERT INTO observations(user_id, patient_code, document_id, measure_code, value, unit, measured_at, ref_low, ref_high) '
                     'VALUES(?,?,?,?,?,?,?,?,?)', (uid, pid, doc_id, o['measure'], o['value'], o['unit'], o['measured_at'], o['ref_low'], o['ref_high']))
    facts = record_rules.facts_of(sheets, patient_id)
    if facts.patient_id:
        conn.execute('INSERT INTO patient_identifiers(user_id, patient_code, value, source_document_id, confirmed, created_at) '
                     'VALUES(?,?,?,?,?,?) ON CONFLICT(user_id, patient_code, value) DO NOTHING',
                     (uid, pid, clean_stem(facts.patient_id + '.x'), doc_id, int(confirmed_id), now))
        if confirmed_id:
            conn.execute('UPDATE patient_identifiers SET confirmed=1 WHERE user_id=? AND patient_code=? AND value=?',
                         (uid, pid, clean_stem(facts.patient_id + '.x')))
    for name, value in (('name', facts.name), ('sex', facts.sex), ('birth_date', facts.birth_date)):
        if value:
            conn.execute('INSERT INTO patient_attributes(user_id, patient_code, name, value, source, source_document_id, recorded_at) '
                         "VALUES(?,?,?,?,'document',?,?)", (uid, pid, name, value, doc_id, now))

def forget_document_facts(conn, doc_id, pid):
    """Remove what one document contributed. A Patient ID no remaining document shows, and that the patient never
    confirmed, goes too (so a wrong first upload does not tie the record to someone else's ID)."""
    uid = session['uid']
    conn.execute('DELETE FROM health_records WHERE document_id=? AND user_id=?', (doc_id, uid))
    conn.execute('DELETE FROM observations WHERE document_id=? AND user_id=?', (doc_id, uid))
    conn.execute('DELETE FROM patient_attributes WHERE source_document_id=? AND user_id=?', (doc_id, uid))
    shown = {clean_stem(_profile_value([json.loads(r['data'])], 'patient id') + '.x')
             for r in _record_rows(conn, pid) if r['sheet'] == 'Patient Profile' and _profile_value([json.loads(r['data'])], 'patient id')}
    for r in conn.execute('SELECT id, value FROM patient_identifiers WHERE user_id=? AND patient_code=? AND confirmed=0', (uid, pid)).fetchall():
        if r['value'] not in shown:
            conn.execute('DELETE FROM patient_identifiers WHERE id=?', (r['id'],))

def known_facts(conn, pid):
    """Name, sex and date of birth already on the record, as the first document that stated each one gave them."""
    rows = conn.execute('SELECT a.name, a.value, a.source, d.original_name FROM patient_attributes a '
                        'LEFT JOIN documents d ON d.id = a.source_document_id WHERE a.user_id=? AND a.patient_code=? ORDER BY a.id',
                        (session['uid'], pid)).fetchall()
    known = {}
    for name in ('name', 'sex', 'birth_date'):
        mine = [r for r in rows if r['name'] == name]
        user = [r for r in mine if r['source'] == 'user']          # only from databases made by an earlier version
        if user:
            known[name] = record_rules.Known(user[-1]['value'], True, 'your record')
        elif mine:
            known[name] = record_rules.Known(mine[0]['value'], False, mine[0]['original_name'] or '')
    return known

def identifiers_of(conn, pid):
    return {r['value'] for r in conn.execute('SELECT value FROM patient_identifiers WHERE user_id=? AND patient_code=?',
                                             (session['uid'], pid)).fetchall()}

def account_record_key():
    """The key of a patient account's record: set by the account, never by a document, so it never changes."""
    return clean_stem((session.get('username') or 'patient') + '.x')

def check_document(name, raw):
    ext=Path(name).suffix.lower()
    if len(raw)>MAX_DOC: raise ValueError(f'{name}: files must be 15 MB or smaller.')
    if not raw: raise ValueError(f'{name}: the file is empty.')
    def docx():
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z: return 'word/document.xml' in z.namelist()
        except zipfile.BadZipFile: return False
    looks_ok={'.pdf':raw.startswith(b'%PDF'),'.png':raw.startswith(b'\x89PNG\r\n\x1a\n'),'.jpg':raw[:3]==b'\xff\xd8\xff',
              '.jpeg':raw[:3]==b'\xff\xd8\xff','.docx':docx(),'.txt':b'\x00' not in raw[:4096],'.csv':b'\x00' not in raw[:4096]}[ext]
    if not looks_ok: raise ValueError(f'{name}: this does not look like a valid {DOC_TYPES[ext]} file.')

def _record_rows(conn, pid):
    return conn.execute('SELECT sheet, data FROM health_records WHERE user_id=? AND patient_code=?',
                        (session['uid'], pid)).fetchall()

def _profile_value(rows, field):
    for row in rows:
        rec = row if isinstance(row, dict) else json.loads(row['data'])
        if str(rec.get('Field', '')).strip().casefold() == field and str(rec.get('Value') or '').strip():
            return str(rec['Value']).strip()
    return ''

def check_matches_record(conn, name, ex, pid):
    """Refuse a document that cannot belong to this record: a different sex, or period data for a male record."""
    rows = [r for r in _record_rows(conn, pid) if r['sheet'] == 'Patient Profile']
    rec_sex = _profile_value(rows, 'sex')[:1].upper()
    prof = ex.sheets.get('Patient Profile')
    doc_sex = _profile_value(prof.to_dict('records'), 'sex')[:1].upper() if prof is not None else ''
    if rec_sex and doc_sex and rec_sex != doc_sex:
        raise ValueError(f'{name}: sex in the document does not match record {pid}. Not added.')
    cycles = ex.sheets.get('Women Health')
    has_cycles = cycles is not None and 'Cycle Length' in cycles and pd.to_numeric(cycles['Cycle Length'], errors='coerce').gt(0).any()
    if (rec_sex or doc_sex) == 'M' and has_cycles:
        raise ValueError(f'{name}: period data does not match record {pid} (male). Not added.')

def adds_nothing(conn, ex, pid):
    """True when every reading in the document is already in the patient's record."""
    canon = lambda sheet, data: (sheet, json.dumps(json.loads(data), sort_keys=True))
    have = {canon(r['sheet'], r['data']) for r in _record_rows(conn, pid)}
    new = {canon(sheet, data) for sheet, _, data in records_of(ex)}
    return bool(new) and new <= have

def ingest(name, raw, origin):
    """Store one uploaded file. Every file is read for health values (healthlens.extract): a document with values is
    added to the patient's record; any other supported file is stored only.
    Returns [(patient id or None, outcome, file name, note)] with outcome 'new', 'added', 'duplicate' or 'stored'.
    Raises ValueError for a file that cannot be accepted."""
    name = base_name(name); ext = Path(name).suffix.lower()
    if ext in ('.xlsx', '.xlsm', '.zip'):
        try: items = workbook_entries(name, raw)
        except zipfile.BadZipFile as exc: raise ValueError(f'{name} is not a valid ZIP archive.') from exc
        except RuntimeError as exc: raise ValueError(f'{name} is password-protected.') from exc
        source = origin + (' · ZIP' if ext == '.zip' else '')
    elif ext in DOC_TYPES:
        check_document(name, raw)
        items, source = [(name, raw)], origin
    else:
        raise ValueError(f'{name}: unsupported file type. Use .xlsx, .zip, .pdf, .docx, .txt, .csv, .png or .jpg.')
    extracted = []
    for item_name, data in items:
        ex = extract(item_name, data)
        left_out = record_rules.drop_implausible(ex.sheets)      # impossible single readings; the rest is kept
        if left_out:
            ex.note = (ex.note.rstrip('. ') + '. ' if ex.note else '') + \
                      f'{left_out} impossible reading{"s" if left_out != 1 else ""} left out.'
        if Path(item_name).suffix.lower() in ('.xlsx', '.xlsm'):
            if ex.kind == 'other': raise ValueError(f'{item_name}: The file is not a readable XLSX workbook.')
            if not ex.readings: raise ValueError(f'{item_name}: no health readings were found (lab results, vitals or wearable data).')
        extracted.append((base_name(item_name), data, ex))
    results = []
    with db() as conn:
        known = {r['sha256'] for r in conn.execute('SELECT sha256 FROM documents WHERE user_id=? AND sha256 IS NOT NULL', (session['uid'],))}
        existing = {r['patient_id'] for r in conn.execute("SELECT DISTINCT patient_id FROM documents WHERE user_id=? AND kind='patient'",
                                                           (session['uid'],))}
        several_allowed = is_team_admin(session['uid'])   # patient accounts hold one record
        for item_name, data, ex in extracted:
            digest = hashlib.sha256(data).hexdigest()
            if digest in known:
                results.append((None, 'duplicate', item_name, '')); continue
            if ex.readings and not several_allowed and len(existing) <= 1:
                # Patient account: the record is the account's own. What the document says (Patient ID, sex,
                # date of birth) is compared with what the record already knows (healthlens.record_rules.judge).
                pid = next(iter(existing)) if existing else account_record_key()
                facts = record_rules.facts_of(ex.sheets, ex.patient_id)
                if facts.patient_id: facts.patient_id = clean_stem(facts.patient_id + '.x')
                verdict = record_rules.judge(facts, known_facts(conn, pid), identifiers_of(conn, pid), item_name)
                if verdict.action == 'reject':
                    results.append((None, 'refused', item_name, verdict.reason)); continue
                if pid in existing and adds_nothing(conn, ex, pid):
                    results.append((None, 'duplicate', item_name, '')); continue
                outcome = 'added' if pid in existing else 'new'
                store_upload(conn, item_name, data, 'patient', ex, pid, source)
                existing.add(pid)
            elif ex.readings:
                # Team accounts (several patients from the provided dataset): the Patient ID decides the record.
                try:
                    pid = patient_for(item_name, ex, existing, several_allowed)
                    check_matches_record(conn, item_name, ex, pid)
                except ValueError as exc:                  # this file is refused; the others in the upload still count
                    results.append((None, 'refused', item_name, str(exc))); continue
                if pid in existing and adds_nothing(conn, ex, pid):   # same values as earlier uploads (e.g. a re-saved copy)
                    results.append((None, 'duplicate', item_name, '')); continue
                outcome = 'added' if pid in existing else 'new'
                store_upload(conn, item_name, data, 'patient', ex, pid, source)
                existing.add(pid)
            else:
                pid, outcome = None, 'stored'
                store_upload(conn, item_name, data, 'document', ex, None, source)
            known.add(digest)
            results.append((pid, outcome, item_name, ex.note))
    for key in [k for k in _analysis_cache if k.startswith(f'u{session["uid"]}:')]: _analysis_cache.pop(key, None)
    return results

def run_ingest(items, origin):
    """Process several files independently (one bad file does not block the others) and report the outcome."""
    before = set(user_patients())
    results, errors = [], []
    for name, raw in items:
        try: results += ingest(name, raw, origin)
        except ValueError as exc: errors.append(str(exc))
    analysed = [r for r in results if r[1] in ('new', 'added')]
    stored = [r for r in results if r[1] == 'stored']
    if analysed:                                         # Summary and Profile now show that patient
        session['active'] = {'patient': analysed[-1][0], 'span': current_span()}
        save_state(session['uid'], analysed[-1][0], current_span())
    parts = []
    if analysed: parts.append(f'{len(analysed)} document{"s" if len(analysed) != 1 else ""} analysed')
    if stored: parts.append(f'{len(stored)} document{"s" if len(stored) != 1 else ""} stored')
    if parts:
        audit_event('upload', f'{len(analysed)} analysed; {len(stored)} stored only; {len(errors)} errors')
        flash(' · '.join(parts) + '.', 'success')
    notes = []
    for pid, outcome, name, note in analysed[:3]:
        if pid in before or not before:
            where, cat = '', 'success'
        else:
            where, cat = f' (new record {pid})', 'warning'
        notes.append((f'{name}: {note.rstrip(".")}{where}.', cat))
    for _, _, name, note in stored[:2]:
        notes.append((f'{name}: {note}', 'warning'))
    for _, _, name, _ in [r for r in results if r[1] == 'duplicate'][:2]:
        notes.append((f'{name}: already uploaded.', 'warning'))
    errors = [r[3] for r in results if r[1] == 'refused'] + errors
    for msg, cat in notes[:5]: flash(msg, cat)
    for msg in errors[:5]: flash(msg, 'error')          # flashes live in the cookie, so keep the list short
    if len(errors) > 5: flash(f'…and {len(errors) - 5} more file(s) could not be imported.', 'error')

# ---- Google Drive import (public "anyone with the link" files only; no Google sign-in is involved)

def drive_download_url(link):
    """Share link -> (direct download URL, file id). Only builds URLs on Google's own hosts."""
    u=urllib.parse.urlparse((link or '').strip())
    if u.scheme not in ('http','https') or u.hostname not in ('drive.google.com','docs.google.com'):
        raise ValueError('Paste a Google Drive or Google Sheets share link.')
    if '/folders/' in u.path: raise ValueError('Folder links are not supported. Share a single file, or put the workbooks in a ZIP.')
    m=re.search(r'/(?:file|spreadsheets|document|presentation)/d/([A-Za-z0-9_-]{10,})',u.path)
    fid=m.group(1) if m else (urllib.parse.parse_qs(u.query).get('id') or [''])[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,}',fid): raise ValueError('Could not find a file ID in that link.')
    if u.hostname=='docs.google.com':
        if '/spreadsheets/' not in u.path: raise ValueError('From Google Docs, only Google Sheets can be imported. Upload other files to Drive and share those.')
        return f'https://docs.google.com/spreadsheets/d/{fid}/export?format=xlsx', fid
    return f'https://drive.google.com/uc?export=download&id={fid}', fid

class _GoogleOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Drive redirects to a googleusercontent host; refuse to follow a redirect anywhere else."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p=urllib.parse.urlparse(newurl); host=p.hostname or ''
        if p.scheme!='https' or not (host=='google.com' or host.endswith(('.google.com','.googleusercontent.com'))):
            raise urllib.error.HTTPError(newurl,code,'Redirect to a non-Google host was blocked',headers,fp)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def sniff_extension(raw):
    if raw.startswith(b'%PDF'): return '.pdf'
    if raw.startswith(b'\x89PNG'): return '.png'
    if raw[:3]==b'\xff\xd8\xff': return '.jpg'
    if raw[:2]==b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z: names=z.namelist()
            return '.xlsx' if 'xl/workbook.xml' in names else '.docx' if 'word/document.xml' in names else '.zip'
        except zipfile.BadZipFile: return ''
    return '.txt'

def fetch_drive_file(link):
    url,fid=drive_download_url(link)
    opener=urllib.request.build_opener(_GoogleOnlyRedirects)
    try:
        with opener.open(urllib.request.Request(url,headers={'User-Agent':'HealthLens/1.0'}),timeout=30) as resp:
            ctype=resp.headers.get_content_type(); disposition=resp.headers.get('Content-Disposition','')
            raw=resp.read(MAX_REMOTE+1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401,403,404): raise ValueError('Google Drive refused the download. Check the file exists and sharing is set to "Anyone with the link".') from exc
        raise ValueError(f'Google Drive returned an error ({exc.code}).') from exc
    except (urllib.error.URLError,TimeoutError,OSError) as exc:
        raise ValueError('Could not reach Google Drive. Check your internet connection and try again.') from exc
    if len(raw)>MAX_REMOTE: raise ValueError('That file is larger than 60 MB.')
    if ctype=='text/html' or raw.lstrip()[:15].lower().startswith((b'<!doctype html',b'<html')):
        raise ValueError('Google returned a web page instead of the file. Set sharing to "Anyone with the link" and try again.')
    m=Message(); m['Content-Disposition']=disposition
    name=base_name(m.get_filename() or f'drive-{fid}')
    if Path(name).suffix.lower() not in ('.xlsx','.zip',*DOC_TYPES): name+=sniff_extension(raw)
    return name, raw

def document_rows():
    """The signed-in user's stored files (older on-disk uploads are moved into the database first)."""
    migrate_disk_uploads()
    with db() as conn:
        return conn.execute('SELECT id,user_id,original_name,stored_name,kind,patient_id,size,origin,created_at,doc_type,readings,note '
                            'FROM documents WHERE user_id=? ORDER BY created_at DESC, id DESC',(session['uid'],)).fetchall()

def own_document(doc_id):
    with db() as conn:
        row=conn.execute('SELECT id,user_id,original_name,stored_name,kind,patient_id,size,origin,created_at FROM documents WHERE id=? AND user_id=?',(doc_id,session['uid'])).fetchone()
    if not row: abort(404)
    return row

@app.template_filter('human_size')
def human_size(n):
    n=float(n or 0)
    for unit in ('B','KB','MB'):
        if n<1024 or unit=='MB': return f'{n:.0f} {unit}' if unit=='B' else f'{n:.1f} {unit}'
        n/=1024

@app.errorhandler(413)
def too_large(_):
    flash('That upload is too large. The limit is 65 MB per upload.','error')
    return redirect(url_for('documents'))

# ---------------------------------------------------------------- reading a patient workbook

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

def _cell_text(v):
    if v is None or (not isinstance(v,(str,pd.Timestamp)) and pd.isna(v)): return ''
    if isinstance(v,pd.Timestamp): return v.strftime('%Y-%m-%d')
    if isinstance(v,float) and v.is_integer(): return str(int(v))
    return str(v).strip()

def medical_history(book):
    """Recorded history entries (conditions, findings, visits), newest first. Displayed as recorded; never inferred."""
    events=[]
    for sheet_name,frame in book.items():
        if not any(term in sheet_name.lower() for term in ('diagnos','condition','medical history','problem')): continue
        for _,row in prepare(frame).head(100).iterrows():
            fields={str(k):_cell_text(v) for k,v in row.items()}
            fields={k:v for k,v in fields.items() if v}
            if not fields: continue
            used=set()
            def pick(*names):
                for n in names:
                    if fields.get(n): used.add(n); return fields[n]
                return ''
            date=pick('Date','Diagnosis Date')
            title=pick('Finding / Event','Diagnosis','Condition','Problem','Finding')
            kind=pick('Encounter / Type','Type'); status=pick('Status'); notes=pick('Notes','Note')
            if not title:
                rest=[(k,v) for k,v in fields.items() if k not in used]
                title=rest[0][1] if rest else 'Entry'; used.add(rest[0][0] if rest else '')
            extra=' · '.join(f'{k}: {v}' for k,v in fields.items() if k not in used)
            events.append({'date':date[:10],'title':title,'kind':kind,'status':status,'notes':notes,'extra':extra[:300]})
    return sorted(events,key=lambda x:x['date'],reverse=True)[:100]

def profile_fields(info):
    out=[]
    for k,v in info.items():
        text=_cell_text(v)
        if text: out.append((str(k),text))
    return out

def active_medications(book):
    meds=book.get('Medication History')
    if meds is None or 'Status' not in meds.columns: return 0
    return int(meds['Status'].astype(str).str.strip().str.lower().eq('active').sum())

# ---------------------------------------------------------------- analysis engine access

VITAL_RULES = {'sbp': (90, 119), 'dbp': (60, 79), 'rhr': (60, 100)}  # illustrative adult ranges, not clinical rules
GROUP_LABELS = {'Lab Results': 'Labs', 'Vitals Daily': 'Vitals', 'Wearable Daily': 'Activity & sleep', 'Women Health': 'Cycle'}

# Time span analysed. Default: the latest 3 months of each record.
SPANS = {'3m': 3, 'all': None}
SPAN_LABELS = {'3m': 'Last 3 months', 'all': 'Full record'}
DEFAULT_SPAN = '3m'

# The three categories in the left panel (same keys as the engine's scenarios) and the measures each one shows.
CATEGORIES = [
    {'key': 'metabolic', 'name': 'Metabolic', 'desc': 'Blood sugar, weight and activity',
     'metrics': ['glucose', 'hba1c', 'weight', 'bmi', 'steps', 'active', 'triglycerides', 'hdl']},
    {'key': 'cardiovascular', 'name': 'Cardiovascular', 'desc': 'Blood pressure, heart rate, cholesterol, stress and sleep',
     'metrics': ['sbp', 'dbp', 'rhr', 'hrv', 'ldl', 'total_chol', 'stress', 'sleep']},
    {'key': 'pcos', 'name': "Women's health (PCOS)", 'desc': 'Cycle regularity, weight, glucose and lipids',
     'metrics': ['cycle_length', 'weight', 'bmi', 'glucose', 'hba1c', 'triglycerides', 'hdl']},
]
CATEGORY_KEYS = {c['key'] for c in CATEGORIES}

def current_span():
    return (session.get('active') or {}).get('span', DEFAULT_SPAN)

def patient_documents(uid, pid, doc_id=None):
    """The patient's readings from health_records, one workbook-like dict per document (oldest first).
    With doc_id: only that document's readings."""
    with db() as conn:
        if doc_id is None:
            rows = conn.execute('SELECT document_id, sheet, data FROM health_records WHERE user_id=? AND patient_code=? ORDER BY document_id, id',
                                (uid, pid)).fetchall()
        else:
            rows = conn.execute('SELECT document_id, sheet, data FROM health_records WHERE user_id=? AND patient_code=? AND document_id=? '
                                'ORDER BY id', (uid, pid, doc_id)).fetchall()
    docs = {}
    for r in rows:
        docs.setdefault(r['document_id'], {}).setdefault(r['sheet'], []).append(json.loads(r['data']))
    books = []
    for sheets in docs.values():
        book = {}
        for sheet, records in sheets.items():
            columns = list(dict.fromkeys(k for rec in records for k in rec))
            book[sheet] = pd.DataFrame.from_records(records, columns=columns)
        books.append(book)
    return books

def get_book(pid, patients=None):
    """Return (loader, cache key) for one of the signed-in user's patients, or (None, None).
    All uploads for the patient are combined into one record (healthlens.data.merge_workbooks)."""
    patients = user_patients() if patients is None else patients
    if pid not in patients: return None, None
    doc_ids = patients[pid]
    uid = session['uid']
    mine = confirmed_details(pid)
    def load():
        books = patient_documents(uid, pid)
        if mine:                                   # the patient's own answers count as the newest source
            books.append({'Patient Profile': pd.DataFrame(list(mine.items()), columns=['Field', 'Value'])})
        return merge_workbooks(books)
    stamp = hashlib.sha256(json.dumps(mine, sort_keys=True, default=str).encode()).hexdigest()[:8] if mine else '0'
    return load, f'u{session["uid"]}:{pid}:{"-".join(map(str, doc_ids))}:{stamp}'

def confirmed_details(pid):
    """Profile fields the patient confirmed on the Profile page (they override what documents say)."""
    with db() as conn:
        known = known_facts(conn, pid)
    out = {}
    if 'sex' in known and known['sex'].confirmed:
        out['Sex'] = known['sex'].value
    if 'birth_date' in known and known['birth_date'].confirmed:
        out['Date of Birth'] = known['birth_date'].value
        out['Age'] = record_rules.age_on(known['birth_date'].value, None)
    return out

def patient_ids_shown(pid):
    """Patient IDs on the record, for display (the record key itself is internal)."""
    with db() as conn:
        rows = conn.execute('SELECT value, confirmed FROM patient_identifiers WHERE user_id=? AND patient_code=? ORDER BY id',
                            (session['uid'], pid)).fetchall()
    return [r['value'] for r in rows]

_analysis_cache = {}

def get_analysis(pid, span=None, patients=None):
    """(full workbook, analysis). The analysis covers the chosen span (default: latest 3 months)."""
    span = span if span in SPANS else current_span()
    loader, key = get_book(pid, patients)
    if loader is None: return None, None
    key = f'{key}|{span}'
    if key not in _analysis_cache:
        book = loader()
        if len(_analysis_cache) > 64: _analysis_cache.pop(next(iter(_analysis_cache)))
        _analysis_cache[key] = (book, analyze_book(book, SPANS[span]))
    return _analysis_cache[key]

def trained_scores(pid, patients=None):
    """Trained risk model scores for the patient, from all their documents (healthlens/risk_model.json)."""
    loader, key = get_book(pid, patients)
    if loader is None: return {}
    key = f'{key}|model'
    if key not in _analysis_cache:
        book, _ = get_analysis(pid, 'all', patients)
        try:
            _analysis_cache[key] = risk_model.score(book)
        except Exception:                                  # no or unreadable model file: the rule-based level is used
            app.logger.exception('Risk model could not score this record')
            _analysis_cache[key] = {}
        save_risk_snapshot(pid, (patients or user_patients()).get(pid, []), _analysis_cache[key])
    return _analysis_cache[key]

def save_risk_snapshot(pid, doc_ids, scores):
    """Keep each risk result once per set of documents, with the model version, so the history can be explained
    ("Cardiovascular went from 8 to 13 after the fitness export was added")."""
    if not scores: return
    docs = '-'.join(map(str, doc_ids))
    model = risk_model.load_model() or {}
    version = f'{model.get("trained", "?")} (seed {model.get("seed", "?")})'
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    try:
        with db() as conn:
            last = conn.execute('SELECT documents FROM risk_assessments WHERE user_id=? AND patient_code=? ORDER BY id DESC LIMIT 1',
                                (session['uid'], pid)).fetchone()
            if last and last['documents'] == docs: return
            for category, r in scores.items():
                sources = sorted({risk_model.SOURCE_OF[k] for k in risk_model.CATEGORY_INPUTS[category]})
                conn.execute('INSERT INTO risk_assessments(user_id, patient_code, category, score, level, model_version, inputs_used, '
                             'inputs_missing, factors, documents, computed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                             (session['uid'], pid, category, r['score'], r['level'], version, json.dumps(r['sources_used']),
                              json.dumps([x for x in sources if x not in r['sources_used']]), json.dumps(r['raising']), docs, now))
    except sqlite3.Error:
        app.logger.exception('Could not save the risk snapshot')

def health_flags_v2(analysis):
    """Latest-value review flags: lab values use the workbook's own reference range; vitals use illustrative ranges."""
    flags = []
    for f in analysis['lab_flags']:
        if f['state'] == 'within': continue
        flags.append({'key': f['metric'], 'metric': f['label'], 'value': f'{f["latest"]:g}', 'unit': f['unit'], 'date': f['date'],
                      'reference': f['reference'], 'severity': 'Review',
                      'message': f'Latest result is {f["state"]} the reference range listed in the workbook ({f["outside"]} of {f["draws"]} draws outside). Discuss with a qualified clinician; interpretation depends on your health context.'})
    for key, (low, high) in VITAL_RULES.items():
        c = analysis['charts'].get(key)
        if not c or not c['points']: continue
        last = c['points'][-1]
        if last['value'] < low or last['value'] > high:
            flags.append({'key': key, 'metric': c['label'], 'value': f'{last["value"]:g}', 'unit': c['unit'], 'date': last['date'],
                          'reference': f'{low}–{high}', 'severity': 'Review',
                          'message': 'Latest daily reading is outside an illustrative adult range. A single reading varies day to day; see the Trends section below for the longer pattern.'})
    return flags

def key_measures(analysis):
    """Latest value of the main health measures, with the sustained trend when the engine found one."""
    charts = analysis['charts']; trends = {t['metric']: t for t in analysis['trends']}
    def last(key):
        c = charts.get(key)
        return c['points'][-1] if c and c['points'] else None
    def card(key, label, value, unit, date, trend):
        direction = trend['direction'] if trend and trend['sustained'] else 'stable'
        return {'key': key, 'label': label, 'value': value, 'unit': unit, 'date': date, 'direction': direction,
                'change': f'{trend["change_pct"]:+.1f}%' if trend and trend['sustained'] else ''}
    out = []
    sbp, dbp = last('sbp'), last('dbp')
    if sbp and dbp: out.append(card('sbp', 'Blood pressure', f'{sbp["value"]:g}/{dbp["value"]:g}', 'mmHg', sbp['date'], trends.get('sbp')))
    for key in ('rhr', 'weight', 'glucose', 'hba1c', 'ldl', 'steps', 'sleep'):
        p = last(key)
        if p: out.append(card(key, charts[key]['label'], f'{round(p["value"], 1):g}', charts[key]['unit'], p['date'], trends.get(key)))
    return out

def chart_payload(analysis, keys=None):
    """Chart data for the Trends section (the engine's unusual-reading markers are left out).
    keys limits the charts to one category's measures."""
    trends = {t['metric']: t for t in analysis['trends']}
    charts = {}
    for key, c in analysis['charts'].items():
        if keys is not None and key not in keys: continue
        t = trends.get(key)
        charts[key] = {'label': c['label'], 'unit': c['unit'], 'group': GROUP_LABELS.get(c['group'], c['group']),
                       'points': c['points'], 'trendline': c['trendline'], 'direction': c['direction'], 'ref': c['ref'],
                       'trend': {'sustained': t['sustained'], 'direction': t['direction'], 'baseline': t['baseline'], 'recent': t['recent'],
                                 'change_pct': t['change_pct'], 'slope_per_month': t['slope_per_month'], 'kind': t['kind']} if t else None}
    return {'charts': charts, 'order': list(charts.keys())}

# ---------------------------------------------------------------- active patient + shared page context

def resolve_active():
    """Which of the user's own patients the pages show. Kept in the session (so Summary, Profile and the chatbot agree)
    and in the database (so the same report opens at the next sign-in, on any computer). There is no demo data."""
    saved = session.get('active') or load_state(session['uid'])
    patients = user_patients()
    ids = sorted(patients)
    wanted = request.args.get('patient') or saved.get('patient')
    selected = wanted if wanted in patients else (ids[0] if ids else '')
    span = request.args.get('span') or saved.get('span') or DEFAULT_SPAN
    if span not in SPANS: span = DEFAULT_SPAN
    active = {'patient': selected, 'span': span}
    if active != {'patient': saved.get('patient', ''), 'span': saved.get('span', DEFAULT_SPAN)}:
        save_state(session['uid'], selected, span)
    session['active'] = active
    return ids, selected, patients

@app.context_processor
def layout_context():
    if not session.get('uid'): return {'account': None, 'chat': None}
    with db() as conn:
        user = conn.execute('SELECT username, email FROM users WHERE id=?', (session['uid'],)).fetchone()
    active = session.get('active') or {}
    st = llm.status()
    if active.get('patient'): llm.warm_up()          # load the local model in the background before the first question
    return {'account': dict(user) if user else None,
            'chat': {'patient': active.get('patient', ''), 'csrf': csrf_token(),
                     'connected': st['provider'] != 'offline', 'display': st['display']}}

def api_auth(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if not session.get('uid'): return jsonify({'error': 'Not signed in'}), 401
        if request.method == 'POST' and request.headers.get('X-CSRF-Token') != session.get('upload_csrf'):
            return jsonify({'error': 'CSRF check failed. Refresh the page.'}), 400
        return fn(*args, **kwargs)
    return inner

# ---------------------------------------------------------------- pages

@app.route('/dashboard')
@login_required
def dashboard():
    """Old address of the Summary page, kept so bookmarks and links still work."""
    return redirect(url_for('summary', **request.args.to_dict()))

def summary_context(book, analysis, model_scores, category, full=None):
    """What the Summary page shows for one workbook: stats, risk per category, flags, measures and charts."""
    info = dict(zip(book.get('Patient Profile', pd.DataFrame()).get('Field', []), book.get('Patient Profile', pd.DataFrame()).get('Value', [])))
    flags, measures = health_flags_v2(analysis), key_measures(analysis)
    scen = {s['key']: s for s in analysis['scenarios']}
    full = full or scen
    def missing_here(key):
        here = {c['signal'] for c in (scen.get(key) or {}).get('checks', []) if c['status'] == 'missing'}
        there = {c['signal'] for c in (full.get(key) or {}).get('checks', []) if c['status'] == 'missing'}
        return here - there
    categories = [{**c, 'scenario': scen.get(c['key']),
                   'risk': risk.assess(scen.get(c['key']), missing_here(c['key']), model_scores.get(c['key']))}
                  for c in CATEGORIES]
    active_cat = next((c for c in categories if c['key'] == category), None)
    keys = set(active_cat['metrics']) if active_cat else None
    if keys is not None:
        flags = [f for f in flags if f['key'] in keys]
        measures = [m for m in measures if m['key'] in keys]
    return dict(age=_cell_text(info.get('Age')) or '—', sex=_cell_text(info.get('Sex')) or '—',
                months=round((analysis['data_quality']['window']['days'] or 0) / 30.4),
                flags=flags, measures=measures, meds_active=active_medications(book),
                chart_data=chart_payload(analysis, keys), categories=categories, category=category,
                active_cat=active_cat, span_info=analysis['span'])

@app.route('/documents/<int:doc_id>/summary')
@login_required
def document_summary(doc_id):
    """Summary of one uploaded report on its own (the Summary page combines all reports)."""
    row = own_document(doc_id)
    ids, _, patients = resolve_active()
    pid = row['patient_id']
    if row['kind'] != 'patient' or pid not in patients:
        flash(f'{row["original_name"]} has no health values to summarise.', 'warning')
        return redirect(url_for('documents'))
    category = request.args.get('category', 'all')
    if category not in CATEGORY_KEYS: category = 'all'
    book = merge_workbooks(patient_documents(session['uid'], pid, doc_id))
    whole, _ = get_analysis(pid, 'all', patients)
    if 'Patient Profile' in whole:                       # age and sex belong to the patient, not to one report
        book['Patient Profile'] = whole['Patient Profile']
    analysis = analyze_book(book, SPANS['all'])
    try:
        model_scores = risk_model.score(book)
    except Exception:
        app.logger.exception('Risk model could not score this report')
        model_scores = {}
    with db() as conn:
        reports = conn.execute("SELECT COUNT(*) FROM documents WHERE user_id=? AND kind='patient' AND patient_id=?",
                               (session['uid'], pid)).fetchone()[0]
    ctx = summary_context(book, analysis, model_scores, category)
    return render_template('summary.html', patients=ids, selected=pid, empty=False, report=dict(row), reports=reports,
                           here='document_summary', here_args={'doc_id': doc_id},
                           span='all', span_label='This report', spans={}, **ctx)

@app.route('/summary')
@login_required
def summary():
    ids, selected, patients = resolve_active()
    span = current_span()
    category = request.args.get('category', 'all')
    if category not in CATEGORY_KEYS: category = 'all'
    if not selected:      # first visit, or nothing uploaded yet: a welcome message instead of data
        return render_template('summary.html', patients=ids, selected='', empty=True)
    book, analysis = get_analysis(selected, span, patients)
    full = None
    if span != 'all':                        # measures missing in this period but present in the full record
        _, whole = get_analysis(selected, 'all', patients)
        full = {s['key']: s for s in whole['scenarios']}
    ctx = summary_context(book, analysis, trained_scores(selected, patients), category, full)
    return render_template('summary.html', patients=ids, selected=selected, empty=False, reports=len(patients.get(selected, [])),
                           patient_ids=patient_ids_shown(selected),
                           here='summary', here_args={},
                           span=span, span_label=SPAN_LABELS[span], spans=SPAN_LABELS, **ctx)

@app.route('/profile')
@login_required
def profile():
    ids, selected, patients = resolve_active()
    with db() as conn:
        user = conn.execute('SELECT username, email, created_at FROM users WHERE id=?', (session['uid'],)).fetchone()
        doc_count = conn.execute('SELECT COUNT(*) FROM documents WHERE user_id=?', (session['uid'],)).fetchone()[0]
    joined = to_local(user['created_at'])
    member_since = joined.strftime('%B %d, %Y') if joined else '—'
    ctx = dict(patients=ids, selected=selected, user=dict(user), member_since=member_since, doc_count=doc_count,
               fields=[], history=[], meds={'columns': [], 'rows': []})
    with db() as conn:
        known = known_facts(conn, selected or account_record_key())
    ctx['details'] = {k: {'value': v.value, 'confirmed': v.confirmed, 'source': v.source} for k, v in known.items()}
    ctx['patient_ids'] = patient_ids_shown(selected) if selected else []
    if selected:
        book, _ = get_analysis(selected, patients=patients)
        info = dict(zip(book.get('Patient Profile', pd.DataFrame()).get('Field', []), book.get('Patient Profile', pd.DataFrame()).get('Value', [])))
        if 'name' in known: info['Name'] = known['name'].value            # the record's own name and all its IDs,
        if ctx['patient_ids']: info['Patient ID'] = ', '.join(ctx['patient_ids'])   # not just the newest document's
        ctx.update(fields=profile_fields(info),
                   history=medical_history(book), meds=safe_rows(book.get('Medication History', pd.DataFrame())))
    return render_template('profile.html', **ctx)

@app.route('/documents')
@login_required
def documents():
    resolve_active()
    rows = document_rows()
    ids = {pid: patient_ids_shown(pid) for pid in {r['patient_id'] for r in rows if r['kind'] == 'patient' and r['patient_id']}}
    return render_template('documents.html', docs=rows, doc_types=DOC_TYPES, max_doc=MAX_DOC // 1024 // 1024,
                           record_ids=ids, team=is_team_admin(session['uid']))

@app.route('/documents/upload', methods=['POST'])
@login_required
def documents_upload():
    if not csrf_ok(request.form.get('csrf')):
        flash('Upload verification failed. Refresh the page and try again.', 'error')
        return redirect(url_for('documents'))
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        flash('Choose at least one file.', 'error')
    elif len(files) > MAX_PER_UPLOAD:
        flash(f'Upload at most {MAX_PER_UPLOAD} files at a time. For many patient workbooks, put them in one ZIP.', 'error')
    else:
        run_ingest([(f.filename, f.read()) for f in files], 'Computer')
    return redirect(url_for('documents'))

@app.route('/documents/drive', methods=['POST'])
@login_required
def documents_drive():
    if not csrf_ok(request.form.get('csrf')):
        flash('Upload verification failed. Refresh the page and try again.', 'error')
        return redirect(url_for('documents'))
    try:
        name, raw = fetch_drive_file(request.form.get('link', ''))
    except ValueError as exc:
        flash(str(exc), 'error')
    else:
        run_ingest([(name, raw)], 'Google Drive')
    return redirect(url_for('documents'))

@app.route('/documents/<int:doc_id>/download')
@login_required
def document_download(doc_id):
    row = own_document(doc_id)
    content = document_content(doc_id)
    if content is None: abort(404)
    return send_file(io.BytesIO(content), as_attachment=True, download_name=row['original_name'])

@app.route('/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
def document_delete(doc_id):
    if not csrf_ok(request.form.get('csrf')):
        flash('Verification failed. Refresh the page and try again.', 'error')
        return redirect(url_for('documents'))
    row = own_document(doc_id)
    old = legacy_path(row)                         # a copy left on disk by an older version, if any
    if old: old.unlink(missing_ok=True)
    with db() as conn:
        conn.execute('DELETE FROM documents WHERE id=? AND user_id=?', (doc_id, session['uid']))
        forget_document_facts(conn, doc_id, row['patient_id'])
    message = f'Deleted {row["original_name"]}.'
    if row['kind'] == 'patient':
        for key in [k for k in _analysis_cache if k.startswith(f'u{session["uid"]}:{row["patient_id"]}:')]: _analysis_cache.pop(key, None)
        left = len(user_patients().get(row['patient_id'], []))
        if left:
            whose = f'{row["patient_id"]}\'s' if is_team_admin(session['uid']) else 'Your'
            message += f' {whose} summary now uses the remaining {left} upload{"s" if left != 1 else ""}.'
    audit_event('file_delete', f'{row["kind"]} document id {doc_id}')
    flash(message, 'success')
    return redirect(url_for('documents'))

# ---------------------------------------------------------------- REST API (used by the chatbot)

@app.route('/api/patients')
@api_auth
def api_patients():
    return jsonify({'patients': sorted(user_patients())})

@app.route('/api/patients/<pid>/analysis')
@api_auth
def api_analysis(pid):
    _, analysis = get_analysis(pid)
    if analysis is None: return jsonify({'error': 'Patient not found'}), 404
    return jsonify(analysis)

@app.route('/api/patients/<pid>/observations')
@api_auth
def api_observations(pid):
    """Every measurement on the record, one row each, with the document it came from. ?measure=ldl filters."""
    if pid not in user_patients(): return jsonify({'error': 'Patient not found'}), 404
    base = ('SELECT o.measure_code, m.name, o.value, o.unit, o.measured_at, o.ref_low, o.ref_high, d.original_name '
            'FROM observations o JOIN measures m ON m.code = o.measure_code JOIN documents d ON d.id = o.document_id '
            'WHERE o.user_id=? AND o.patient_code=? AND (? = \'\' OR o.measure_code = ?) ORDER BY o.measure_code, o.measured_at')
    measure = request.args.get('measure', '')
    with db() as conn:
        rows = conn.execute(base, (session['uid'], pid, measure, measure)).fetchall()
    return jsonify({'observations': [{'measure': r['measure_code'], 'name': r['name'], 'value': r['value'], 'unit': r['unit'],
                                      'measured_at': r['measured_at'], 'ref_low': r['ref_low'], 'ref_high': r['ref_high'],
                                      'document': r['original_name']} for r in rows]})

def record_chat_time(pid, result):
    """Store how long the chatbot took (no question text, no health values)."""
    t = result.get('timing', {})
    try:
        with db() as conn:
            conn.execute('INSERT INTO chat_metrics(user_id,patient,mode,total_ms,llm_ms,attempts,cached,prompt_chars,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                         (session['uid'], pid, result.get('mode'), t.get('total_ms'), t.get('llm_ms'), t.get('attempts'),
                          int(bool(t.get('cached'))), t.get('prompt_chars'), datetime.now(timezone.utc).isoformat(timespec='seconds')))
    except sqlite3.Error:
        app.logger.exception('Unable to record chatbot timing')

@app.route('/api/patients/<pid>/ask', methods=['POST'])
@api_auth
def api_ask(pid):
    t0 = time.perf_counter()
    body = request.get_json(silent=True) or {}
    question = str(body.get('question', '')).strip()
    if not 3 <= len(question) <= 500: return jsonify({'error': 'Ask a question between 3 and 500 characters.'}), 400
    _, analysis = get_analysis(pid)
    if analysis is None: return jsonify({'error': 'Patient not found'}), 404
    analysis_ms = int((time.perf_counter() - t0) * 1000)
    result = llm.ask(analysis, question)
    result.setdefault('timing', {})
    result['timing'].update(analysis_ms=analysis_ms, total_ms=int((time.perf_counter() - t0) * 1000))
    llm.audit(session['uid'], pid, 'ask', result)
    record_chat_time(pid, result)
    return jsonify(result)

if __name__=='__main__':
    print('HealthLens database: local file users.sqlite3')
    if len(sys.argv) == 3 and sys.argv[1] in ('--make-admin', '--remove-admin'):
        message = set_admin(sys.argv[2], getpass.getpass('Confirm this user\'s password: '), sys.argv[1] == '--make-admin')
        if not message.startswith('Admin access'): sys.exit(message)
        print(message)
    else:
        app.run(debug=True,host='127.0.0.1',port=5000)

