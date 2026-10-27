import io, zipfile, sqlite3, secrets, hashlib, hmac, re, os
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from email.message import Message
from functools import wraps
from pathlib import Path
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort, send_file

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
MAX_BOOK = 6*1024*1024          # one patient workbook
MAX_FILES = 120                 # workbooks inside one ZIP
MAX_DOC = 15*1024*1024          # one supporting document (PDF, Word, image ...)
MAX_REMOTE = 60*1024*1024       # one file imported from Google Drive
MAX_PER_UPLOAD = 20             # files in one upload request
# Supporting documents are stored and can be downloaded, but are not analysed.
DOC_TYPES = {'.pdf': 'PDF', '.docx': 'Word', '.txt': 'Text', '.csv': 'CSV', '.png': 'Image', '.jpg': 'Image', '.jpeg': 'Image'}

def db():
    conn=sqlite3.connect(DB)
    conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL, email TEXT UNIQUE COLLATE NOCASE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, original_name TEXT NOT NULL, stored_name TEXT NOT NULL, kind TEXT NOT NULL, patient_id TEXT, size INTEGER NOT NULL, origin TEXT NOT NULL, created_at TEXT NOT NULL)')
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
def home(): return redirect(url_for('summary' if session.get('uid') else 'login'))

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
            return redirect(url_for('summary'))
        flash('Incorrect email/username or password.','error')
    return render_template('login.html')

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

def csrf_token():
    return session.setdefault('upload_csrf', secrets.token_urlsafe(24))

def csrf_ok(value):
    return bool(value) and hmac.compare_digest(str(value), session.get('upload_csrf', ''))

def _source(value):
    return value if value in ('demo', 'uploaded') else 'demo'

@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    return resp

# ---------------------------------------------------------------- file ingestion (Documents page)

def validate_workbook(raw):
    if len(raw)>MAX_BOOK: raise ValueError('Each workbook must be 6 MB or smaller.')
    try:
        book=pd.read_excel(io.BytesIO(raw),sheet_name=None,engine='openpyxl')
    except Exception as exc:
        raise ValueError('The file is not a readable XLSX workbook.') from exc
    if 'Patient Profile' not in book: raise ValueError('Workbook must include a Patient Profile sheet.')
    if not any(k in book for k in ('Lab Results','Vitals Daily','Wearable Daily')):
        raise ValueError('Workbook must include Lab Results, Vitals Daily, or Wearable Daily.')
    return book

def base_name(name):
    """Bare file name for display: no folders, no control characters."""
    name=re.sub(r'[\x00-\x1f\x7f]','',str(name)).replace('\\','/').split('/')[-1].strip()
    return name[:120] or 'file'

def clean_stem(name):
    """Patient id taken from a workbook name. 'PC0101 (1).xlsx' -> 'PC0101_1'."""
    stem=re.sub(r'[^A-Za-z0-9_-]+','_',Path(name).stem).strip('_')[:60]
    if not stem: raise ValueError(f'"{name}" has no usable name. Rename it using letters and numbers.')
    return stem

def read_patient_workbooks(filename, raw):
    """Validate an .xlsx or a .zip of .xlsx files. Returns [(patient_id, bytes)] or raises ValueError."""
    if Path(filename).suffix.lower()=='.xlsx':
        candidates=[(filename,raw)]
    else:
        candidates=[]
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries=[e for e in archive.infolist() if not e.is_dir() and e.filename.lower().endswith('.xlsx')
                     and not Path(e.filename).name.startswith('~$') and '__MACOSX' not in e.filename]
            if not entries or len(entries)>MAX_FILES: raise ValueError(f'{filename}: ZIP must contain 1–{MAX_FILES} XLSX patient workbooks.')
            for e in entries:
                with archive.open(e) as fh: data=fh.read(MAX_BOOK+1)   # read at most the limit, whatever the header claims
                if len(data)>MAX_BOOK: raise ValueError(f'{filename}: "{Path(e.filename).name}" exceeds the 6 MB limit.')
                candidates.append((Path(e.filename).name,data))
    prepared=[]
    for name,data in candidates:
        try: validate_workbook(data)
        except ValueError as exc: raise ValueError(f'{name}: {exc}') from exc
        prepared.append((clean_stem(name),data))
    if len({stem for stem,_ in prepared})!=len(prepared): raise ValueError(f'{filename}: two workbooks have the same patient name.')
    return prepared

def store_patients(prepared, origin):
    folder=user_upload_dir(); folder.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    with db() as conn:
        for stem,data in prepared:
            (folder/(stem+'.xlsx')).write_bytes(data)
            conn.execute("DELETE FROM documents WHERE user_id=? AND kind='patient' AND patient_id=?",(session['uid'],stem))
            conn.execute('INSERT INTO documents(user_id,original_name,stored_name,kind,patient_id,size,origin,created_at) VALUES(?,?,?,?,?,?,?,?)',
                         (session['uid'],stem+'.xlsx',stem+'.xlsx','patient',stem,len(data),origin,now))
    for key in [k for k in _analysis_cache if k.startswith(f'u{session["uid"]}:')]: _analysis_cache.pop(key,None)

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

def store_document(name, raw, origin):
    ext=Path(name).suffix.lower()
    folder=user_upload_dir()/'docs'; folder.mkdir(parents=True,exist_ok=True)
    stored=secrets.token_hex(12)+ext              # random on-disk name: the uploaded name is never used as a path
    (folder/stored).write_bytes(raw)
    with db() as conn:
        conn.execute('INSERT INTO documents(user_id,original_name,stored_name,kind,patient_id,size,origin,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (session['uid'],name,stored,'document',None,len(raw),origin,datetime.now(timezone.utc).isoformat(timespec='seconds')))

def ingest(name, raw, origin):
    """Store one file. Returns list of new patient ids (empty for a supporting document). Raises ValueError."""
    name=base_name(name); ext=Path(name).suffix.lower()
    if ext in ('.xlsx','.zip'):
        try: prepared=read_patient_workbooks(name,raw)
        except zipfile.BadZipFile as exc: raise ValueError(f'{name} is not a valid ZIP archive.') from exc
        except RuntimeError as exc: raise ValueError(f'{name} is password-protected.') from exc
        store_patients(prepared, origin+(' · ZIP' if ext=='.zip' else ''))
        return [stem for stem,_ in prepared]
    if ext in DOC_TYPES:
        check_document(name,raw); store_document(name,raw,origin); return []
    raise ValueError(f'{name}: unsupported file type. Use .xlsx, .zip, .pdf, .docx, .txt, .csv, .png or .jpg.')

def run_ingest(items, origin):
    """Process several files independently (one bad file does not block the others) and report the outcome."""
    patients, documents, errors = [], 0, []
    for name,raw in items:
        try:
            new=ingest(name,raw,origin)
            if new: patients+=new
            else: documents+=1
        except ValueError as exc: errors.append(str(exc))
    if patients: session['active']={'source':'uploaded','patient':patients[0]}   # Summary and Profile now show the new patient
    parts=[]
    if patients: parts.append(f'{len(patients)} patient workbook{"s" if len(patients)!=1 else ""} imported and analysed')
    if documents: parts.append(f'{documents} document{"s" if documents!=1 else ""} stored')
    if parts: flash(' · '.join(parts)+'.','success')
    for msg in errors[:5]: flash(msg,'error')          # flashes live in the cookie, so keep the list short
    if len(errors)>5: flash(f'…and {len(errors)-5} more file(s) could not be imported.','error')

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
    """The signed-in user's stored files. Workbooks found on disk but missing from the table (older uploads) are added first."""
    with db() as conn:
        known={r['patient_id'] for r in conn.execute("SELECT patient_id FROM documents WHERE user_id=? AND kind='patient'",(session['uid'],))}
        for stem,path in uploaded_members().items():
            if stem not in known:
                st=path.stat()
                conn.execute('INSERT INTO documents(user_id,original_name,stored_name,kind,patient_id,size,origin,created_at) VALUES(?,?,?,?,?,?,?,?)',
                             (session['uid'],path.name,path.name,'patient',stem,st.st_size,'Computer',datetime.fromtimestamp(st.st_mtime,timezone.utc).isoformat(timespec='seconds')))
        return conn.execute('SELECT * FROM documents WHERE user_id=? ORDER BY created_at DESC, id DESC',(session['uid'],)).fetchall()

def own_document(doc_id):
    with db() as conn:
        row=conn.execute('SELECT * FROM documents WHERE id=? AND user_id=?',(doc_id,session['uid'])).fetchone()
    if not row: abort(404)
    return row

def stored_path(row):
    base=user_upload_dir().resolve()
    path=(base/('docs' if row['kind']=='document' else '')/row['stored_name']).resolve()
    if base not in path.parents: abort(404)
    return path

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

def get_book(source, pid):
    """Return (loader, cache key) for a demo or uploaded patient, or (None, None)."""
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
                      'reference': f['reference'], 'severity': 'Review',
                      'message': f'Latest result is {f["state"]} the reference range listed in the workbook ({f["outside"]} of {f["draws"]} draws outside). Discuss with a qualified clinician; interpretation depends on your health context.'})
    for key, (low, high) in VITAL_RULES.items():
        c = analysis['charts'].get(key)
        if not c or not c['points']: continue
        last = c['points'][-1]
        if last['value'] < low or last['value'] > high:
            flags.append({'metric': c['label'], 'value': f'{last["value"]:g}', 'unit': c['unit'], 'date': last['date'],
                          'reference': f'{low}–{high}', 'severity': 'Review',
                          'message': 'Latest daily reading is outside an illustrative adult range. A single reading varies day to day; see the Trends section below for the longer pattern.'})
    return flags

def key_measures(analysis):
    """Latest value of the main health measures, with the sustained trend when the engine found one."""
    charts = analysis['charts']; trends = {t['metric']: t for t in analysis['trends']}
    def last(key):
        c = charts.get(key)
        return c['points'][-1] if c and c['points'] else None
    def card(label, value, unit, date, trend):
        direction = trend['direction'] if trend and trend['sustained'] else 'stable'
        return {'label': label, 'value': value, 'unit': unit, 'date': date, 'direction': direction,
                'change': f'{trend["change_pct"]:+.1f}%' if trend and trend['sustained'] else ''}
    out = []
    sbp, dbp = last('sbp'), last('dbp')
    if sbp and dbp: out.append(card('Blood pressure', f'{sbp["value"]:g}/{dbp["value"]:g}', 'mmHg', sbp['date'], trends.get('sbp')))
    for key in ('rhr', 'weight', 'glucose', 'hba1c', 'ldl', 'steps', 'sleep'):
        p = last(key)
        if p: out.append(card(charts[key]['label'], f'{round(p["value"], 1):g}', charts[key]['unit'], p['date'], trends.get(key)))
    return out

def chart_payload(analysis):
    """Chart data for the Trends section (the engine's unusual-reading markers are left out)."""
    trends = {t['metric']: t for t in analysis['trends']}
    charts = {}
    for key, c in analysis['charts'].items():
        t = trends.get(key)
        charts[key] = {'label': c['label'], 'unit': c['unit'], 'group': GROUP_LABELS.get(c['group'], c['group']),
                       'points': c['points'], 'trendline': c['trendline'], 'direction': c['direction'], 'ref': c['ref'],
                       'trend': {'sustained': t['sustained'], 'direction': t['direction'], 'baseline': t['baseline'], 'recent': t['recent'],
                                 'change_pct': t['change_pct'], 'slope_per_month': t['slope_per_month'], 'kind': t['kind']} if t else None}
    return {'charts': charts, 'order': list(charts.keys())}

# ---------------------------------------------------------------- active patient + shared page context

def resolve_active():
    """Which patient the pages show. Remembered in the session so Summary, Profile and the chatbot always agree."""
    saved = session.get('active') or {}
    source = _source(request.args.get('source') or saved.get('source') or 'demo')
    members = uploaded_members() if source == 'uploaded' else patient_members()
    ids = sorted(members)
    wanted = request.args.get('patient') or (saved.get('patient') if saved.get('source') == source else None)
    if wanted in members: selected = wanted
    elif 'PC0101' in members: selected = 'PC0101'
    else: selected = ids[0] if ids else ''
    session['active'] = {'source': source, 'patient': selected}
    return source, ids, selected

@app.context_processor
def layout_context():
    if not session.get('uid'): return {'account': None, 'chat': None}
    with db() as conn:
        user = conn.execute('SELECT username, email FROM users WHERE id=?', (session['uid'],)).fetchone()
    active = session.get('active') or {}
    st = llm.status()
    return {'account': dict(user) if user else None,
            'chat': {'patient': active.get('patient', ''), 'source': active.get('source', 'demo'), 'csrf': csrf_token(),
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

@app.route('/summary')
@login_required
def summary():
    source, ids, selected = resolve_active()
    if not selected:
        return render_template('summary.html', source=source, patients=ids, selected='', empty=True)
    book, analysis = get_analysis(source, selected)
    info = dict(zip(book.get('Patient Profile', pd.DataFrame()).get('Field', []), book.get('Patient Profile', pd.DataFrame()).get('Value', [])))
    flags = health_flags_v2(analysis)
    return render_template('summary.html', source=source, patients=ids, selected=selected, empty=False,
                           age=_cell_text(info.get('Age')) or '—', sex=_cell_text(info.get('Sex')) or '—',
                           months=round((analysis['data_quality']['window']['days'] or 0) / 30.4),
                           flags=flags, measures=key_measures(analysis), meds_active=active_medications(book),
                           chart_data=chart_payload(analysis))

@app.route('/profile')
@login_required
def profile():
    source, ids, selected = resolve_active()
    with db() as conn:
        user = conn.execute('SELECT username, email, created_at FROM users WHERE id=?', (session['uid'],)).fetchone()
        doc_count = conn.execute('SELECT COUNT(*) FROM documents WHERE user_id=?', (session['uid'],)).fetchone()[0]
    try: member_since = datetime.fromisoformat(user['created_at']).strftime('%B %d, %Y')
    except (ValueError, TypeError): member_since = '—'
    ctx = dict(source=source, patients=ids, selected=selected, user=dict(user), member_since=member_since, doc_count=doc_count,
               fields=[], history=[], meds={'columns': [], 'rows': []})
    if selected:
        book, _ = get_analysis(source, selected)
        ctx.update(fields=profile_fields(dict(zip(book.get('Patient Profile', pd.DataFrame()).get('Field', []), book.get('Patient Profile', pd.DataFrame()).get('Value', [])))),
                   history=medical_history(book), meds=safe_rows(book.get('Medication History', pd.DataFrame())))
    return render_template('profile.html', **ctx)

@app.route('/documents')
@login_required
def documents():
    resolve_active()
    return render_template('documents.html', docs=document_rows(), doc_types=DOC_TYPES, max_doc=MAX_DOC // 1024 // 1024)

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
    path = stored_path(row)
    if not path.is_file(): abort(404)
    return send_file(path, as_attachment=True, download_name=row['original_name'])

@app.route('/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
def document_delete(doc_id):
    if not csrf_ok(request.form.get('csrf')):
        flash('Verification failed. Refresh the page and try again.', 'error')
        return redirect(url_for('documents'))
    row = own_document(doc_id)
    stored_path(row).unlink(missing_ok=True)
    with db() as conn: conn.execute('DELETE FROM documents WHERE id=?', (doc_id,))
    if row['kind'] == 'patient':
        for key in [k for k in _analysis_cache if k.startswith(f'u{session["uid"]}:{row["patient_id"]}:')]: _analysis_cache.pop(key, None)
    flash(f'Deleted {row["original_name"]}.', 'success')
    return redirect(url_for('documents'))

# ---------------------------------------------------------------- REST API (used by the chatbot)

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
