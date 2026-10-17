import io, zipfile, sqlite3, secrets, hashlib, hmac, re, os
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.environ.get('HEALTHLENS_SECRET_KEY') or secrets.token_hex(32)  # local demo; resets on restart
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
DB = ROOT / 'users.sqlite3'
DATA = ROOT / 'patient_data.zip'

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

@app.route('/logout',methods=['POST'])
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

def patient_members():
    if not DATA.exists(): return {}
    with zipfile.ZipFile(DATA) as z:
        return {Path(n).stem:n for n in z.namelist() if n.lower().endswith('.xlsx') and not n.startswith('__MACOSX')}

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

def chart_series(book):
    output={}
    labs=prepare(book.get('Lab Results',pd.DataFrame()))
    vitals=prepare(book.get('Vitals Daily',pd.DataFrame()))
    wear=prepare(book.get('Wearable Daily',pd.DataFrame()))
    if {'Test Name','Result','Date'}.issubset(labs.columns):
        for name in ['Fasting glucose','HbA1c','LDL','HDL']:
            sub=labs[labs['Test Name'].astype(str).str.casefold()==name.casefold()]
            output['Labs · '+name]=series(sub,'Result')
    for group,frame,cols in [('Vitals',vitals,['Systolic BP','Diastolic BP','Weight kg','Resting HR']),('Wearables',wear,['Steps','Sleep Hours','Active Minutes'])]:
        for col in cols:
            if col in frame.columns: output[group+' · '+col]=series(frame,col)
    return {k:v for k,v in output.items() if v}

def series(frame,col):
    if 'Date' not in frame.columns or col not in frame.columns: return []
    sub=frame[['Date',col]].copy()
    sub[col]=pd.to_numeric(sub[col],errors='coerce')
    sub=sub.dropna().sort_values('Date')
    return [{'date':row['Date'].strftime('%Y-%m-%d'),'value':float(row[col])} for _,row in sub.iterrows()]

def observation(label,values,unit=''):
    if len(values)<3: return None
    a,b=values[0],values[-1]
    delta=b['value']-a['value']
    verb='increased' if delta>0 else 'decreased' if delta<0 else 'was unchanged'
    return f'{label} {verb} from {a["value"]:,.1f} to {b["value"]:,.1f} {unit} between {a["date"]} and {b["date"]} ({len(values)} observations).'

def safe_rows(frame,limit=150):
    clean=frame.dropna(how='all').head(limit).copy()
    clean=clean.where(pd.notna(clean),None)
    return {'columns':[str(x) for x in clean.columns],'rows':[[str(v) if v is not None else '' for v in row] for row in clean.itertuples(index=False,name=None)]}

@app.route('/dashboard')
@login_required
def dashboard():
    members=patient_members()
    if not members: return render_template('dashboard.html',username=session['username'],error='patient_data.zip not found or contains no .xlsx workbooks',patients=[],selected='',profile={},charts={},observations=[],meds={'columns':[],'rows':[]},sheets={})
    ids=sorted(members)
    selected=request.args.get('patient','PC0101')
    if selected not in members: selected=ids[0]
    book=load_book(members[selected])
    profile=book.get('Patient Profile',pd.DataFrame())
    info=dict(zip(profile.get('Field',[]),profile.get('Value',[])))
    charts=chart_series(book)
    units={'Labs · Fasting glucose':'mg/dL','Vitals · Systolic BP':'mmHg','Vitals · Weight kg':'kg','Wearables · Steps':'steps','Wearables · Sleep Hours':'hours'}
    chosen=['Labs · Fasting glucose','Vitals · Systolic BP','Vitals · Weight kg','Wearables · Steps','Wearables · Sleep Hours']
    observations=[observation(key.split(' · ')[-1],charts.get(key,[]),units.get(key,'')) for key in chosen]
    observations=[x for x in observations if x]
    return render_template('dashboard.html',username=session['username'],error=None,patients=ids,selected=selected,profile=info,charts=charts,observations=observations,meds=safe_rows(book.get('Medication History',pd.DataFrame())),sheets={k:safe_rows(v) for k,v in book.items()})

if __name__=='__main__':
    app.run(debug=True,host='127.0.0.1',port=5000)
