"""WSGI application: authenticated API, persistent queue, audit and static UI.

Deployment contract: one application process, threaded WSGI server, local persistent
disk. A process lock prevents accidental competing workers against the same store.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from engine import canonical, reconcile, validate_rules
from synthetic import DEFAULT_RULES, generate
from enterprise import Enterprise

LOG = logging.getLogger('reconcile')
ROOT = Path(__file__).resolve().parent
MAX_BODY = 32 * 1024 * 1024
ROLES = {'viewer':0, 'analyst':1, 'admin':2}
DUMMY_PASSWORD_HASH='scrypt$32768$8$3$'+'0'*32+'$'+'0'*128

def now(): return datetime.now(timezone.utc).isoformat()
def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3,maxmem=64*1024*1024).hex()
    return 'scrypt$32768$8$3$' + salt + '$' + digest
def password_matches(password, stored):
    if ':' in stored:  # Compatibility with early local builds.
        salt,digest=stored.split(':')
        actual=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
        return hmac.compare_digest(actual,digest)
    return hmac.compare_digest(password_hash(password,stored.split('$')[4]),stored)

class APIError(Exception):
    def __init__(self,status,message): self.status,self.message=status,message

class FileBody:
    def __init__(self,path,temporary=False):
        self.path=Path(path);self.length=self.path.stat().st_size;self.temporary=temporary;self.stream=None
    def __iter__(self):
        try:
            self.stream=self.path.open('rb')
            for block in iter(lambda:self.stream.read(65536),b''):yield block
        finally:self.close()
    def close(self):
        if self.stream:self.stream.close();self.stream=None
        if self.temporary:self.path.unlink(missing_ok=True)

class Application(Enterprise):
    def __init__(self, data_dir=None, demo=False, start_worker=True):
        self.demo=demo
        self.data=Path(data_dir or os.environ.get('RECON_DATA_DIR', ROOT/'runtime')).resolve()
        self.data.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.uploads=self.data/'snapshots';self.uploads.mkdir(exist_ok=True,mode=0o700)
        (self.data/'tmp').mkdir(exist_ok=True,mode=0o700)
        self.secret=os.environ.get('RECON_SECRET','') or (secrets.token_urlsafe(48) if demo else '')
        if not demo and len(self.secret)<32:
            raise RuntimeError('Set RECON_SECRET to at least 32 random characters.')
        if not demo and self.secret.startswith('replace-with-'):
            raise RuntimeError('Replace the example secret before production startup.')
        mfa_key=os.environ.get('RECON_MFA_KEY')
        if not demo and mfa_key and (len(mfa_key)<32 or mfa_key.startswith('replace-with-')):raise RuntimeError('Set a strong, independent MFA encryption secret.')
        if not demo and os.environ.get('RECON_SSO_ONLY')=='1' and len(os.environ.get('RECON_SSO_PROXY_SECRET',''))<32:raise RuntimeError('SSO-only mode requires a strong trusted-proxy secret.')
        self.db_path=self.data/'reconcile.sqlite'
        self.stop=threading.Event();self.wake=threading.Event()
        self.broker_ready=False
        self.lock_file=None
        if start_worker: self.acquire_lock()
        self.initialize()
        self.upgrade()
        if start_worker:
            with self.db() as db:
                db.execute('DELETE FROM worker_heartbeats')
                db.execute("UPDATE mail_outbox SET state='pending' WHERE state='sending'")
                db.execute("UPDATE runs SET state='queued',phase='Recovered after restart' WHERE state='running'")
            self.thread=threading.Thread(target=self.worker,name='reconcile-worker',daemon=True);self.thread.start()
            self.mail_thread=threading.Thread(target=self.mail_loop,name='recovery-mail-worker',daemon=True);self.mail_thread.start()
            self.heartbeat_thread=threading.Thread(target=self.heartbeat_loop,name='service-heartbeat',daemon=True);self.heartbeat_thread.start()

    def acquire_lock(self):
        self.lock_file=open(self.data/'worker.lock','a+b')
        self.lock_file.seek(0);self.lock_file.write(b'0');self.lock_file.flush();self.lock_file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise RuntimeError('Another coordinator is using this data directory. Run exactly one application process.') from None

    @contextmanager
    def db(self):
        conn=sqlite3.connect(self.db_path,timeout=30)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON');conn.execute('PRAGMA busy_timeout=30000')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback();raise
        finally: conn.close()

    def initialize(self):
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, applied TEXT);
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','analyst','viewer')),active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),csrf TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS login_attempts(address TEXT PRIMARY KEY,failures INTEGER,last REAL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,dataset TEXT NOT NULL,created TEXT NOT NULL,actor TEXT REFERENCES users(id),state TEXT NOT NULL,phase TEXT,rule_json TEXT NOT NULL,source_file TEXT,destination_file TEXT,summary TEXT,error TEXT,elapsed REAL,idempotency TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS issues(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT REFERENCES runs(id),category TEXT,key TEXT,col TEXT,severity TEXT,source TEXT,destination TEXT,detail TEXT,status TEXT DEFAULT 'open');
            CREATE INDEX IF NOT EXISTS issue_filter ON issues(run_id,category,severity,status);
            CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY,issue_id INTEGER REFERENCES issues(id),actor TEXT REFERENCES users(id),decision TEXT,note TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,created TEXT,actor TEXT,action TEXT,resource TEXT,metadata TEXT,previous TEXT,hash TEXT);
            CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'Append-only audit'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'Append-only audit'); END;
            ''')
            db.execute('INSERT OR IGNORE INTO migrations VALUES(1,?)',(now(),))
            if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0:
                email='demo@reconcile.local' if self.demo else os.environ.get('RECON_ADMIN_EMAIL','')
                password=secrets.token_urlsafe(32) if self.demo else os.environ.get('RECON_ADMIN_PASSWORD','')
                if not email or len(password)<12:
                    raise RuntimeError('First start requires RECON_ADMIN_EMAIL and RECON_ADMIN_PASSWORD (12+ characters).')
                db.execute('INSERT INTO users(id,email,password,role) VALUES(?,?,?,?)',('admin',email.lower(),password_hash(password),'admin'))
                self.audit(db,'system','user.bootstrap','admin',{'email':email})

    def audit(self,db,actor,action,resource,metadata=None):
        # A write reservation serializes chain updates across concurrent requests.
        db.execute('UPDATE migrations SET version=version WHERE version=1')
        last=db.execute('SELECT hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
        previous=last[0] if last else '0'*64
        timestamp=now();meta=canonical(metadata or {})
        digest=hashlib.sha256(canonical([timestamp,actor,action,resource,meta,previous]).encode()).hexdigest()
        db.execute('INSERT INTO audit(created,actor,action,resource,metadata,previous,hash) VALUES(?,?,?,?,?,?,?)',(timestamp,actor,action,resource,meta,previous,digest))

    def session(self,environ):
        cookie=environ.get('HTTP_COOKIE','')
        token=next((v.partition('=')[2] for v in cookie.split(';') if v.strip().startswith('recon_session=')),None)
        if not token: return None
        with self.db() as db:
            row=db.execute('SELECT users.id,users.email,users.role,sessions.csrf FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token=? AND sessions.expires>? AND users.active=1',(self.token_hash(token),time.time())).fetchone()
        return dict(row) if row else None

    def token_hash(self,token):
        return hmac.new(self.secret.encode(),token.encode(),hashlib.sha256).hexdigest()

    def authorize(self,user,role='viewer'):
        if not user: raise APIError(401,'Sign in to continue.')
        if ROLES[user['role']]<ROLES[role]: raise APIError(403,'Your role cannot perform this action.')

    def enqueue(self,user,payload,idem=None):
        name=payload.get('dataset','').strip()
        if not name or len(name)>120: raise APIError(400,'Dataset name must contain 1–120 characters.')
        rules=validate_rules(payload.get('rules',DEFAULT_RULES))
        if shutil.disk_usage(self.data).free<256*1024*1024: raise APIError(503,'Insufficient free storage. An administrator must free disk space.')
        large_paths=None;pending_connection=None
        if payload.get('source_upload') or payload.get('destination_upload'):
            with self.db() as db:
                uploads=[db.execute("SELECT * FROM upload_sessions WHERE id=? AND actor=? AND state='sealed' AND created>?",(payload.get(side+'_upload'),user['id'],time.time()-86400)).fetchone() for side in ('source','destination')]
                if any(row is None for row in uploads):raise APIError(400,'Both uploads must be sealed and owned by you.')
                large_paths=[self.data/'uploads'/row['id'] for row in uploads]
            source=dest='uploaded'
        elif payload.get('demo'):
            if not self.demo: raise APIError(403,'Synthetic demo is disabled in production.')
            source,dest,_=generate(corrected=bool(payload.get('corrected')))
        elif payload.get('connection'):
            self.validate_connection_payload(payload,user);pending_connection=payload['connection'];source=dest='Snapshot pending'
        else:
            source,dest=payload.get('source_csv'),payload.get('destination_csv')
        if not isinstance(source,str) or not isinstance(dest,str) or not source or not dest:
            raise APIError(400,'Provide both source and destination CSV files.')
        if max(len(source.encode()),len(dest.encode()))>12*1024*1024:
            raise APIError(413,'Each CSV snapshot is limited to 12 MiB. Use external partitions for larger tables.')
        if idem and not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',idem): raise APIError(400,'Invalid idempotency key.')
        idem_key=f"{user['id']}:{idem}" if idem else None
        rid=uuid.uuid4().hex
        paths=[self.uploads/f'{rid}-source.csv',self.uploads/f'{rid}-destination.csv']
        with self.db() as db:
            # Reserve SQLite write ownership before checking queue and idempotency.
            db.execute('BEGIN IMMEDIATE')
            existing_dataset=db.execute('SELECT 1 FROM runs WHERE dataset=? LIMIT 1',(name,)).fetchone()
            if existing_dataset:self.require_dataset(db,user,name,True)
            required=sum(path.stat().st_size for path in large_paths) if large_paths else sum(len(text.encode()) for text in (source,dest))
            if shutil.disk_usage(self.data).free<required+256*1024*1024:raise APIError(503,'Insufficient space to preserve both input snapshots.')
            if idem_key:
                existing=db.execute('SELECT id FROM runs WHERE idempotency=?',(idem_key,)).fetchone()
                if existing: return existing['id']
            if db.execute("SELECT COUNT(*) FROM runs WHERE state IN ('queued','running')").fetchone()[0]>=10:
                raise APIError(429,'The queue is full. Wait for an existing run to finish.')
            try:
                for index,(path,text) in enumerate(zip(paths,(source,dest))):
                    if large_paths:
                        with large_paths[index].open('rb') as incoming,path.open('xb') as outgoing:shutil.copyfileobj(incoming,outgoing,1024*1024)
                    else:
                        with path.open('x',encoding='utf-8',newline='') as file: file.write(text)
                db.execute('INSERT INTO runs(id,dataset,created,actor,state,phase,rule_json,source_file,destination_file,idempotency) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (rid,name,now(),user['id'],'queued','Waiting for worker',canonical(rules),paths[0].name,paths[1].name,idem_key))
                self.audit(db,user['id'],'run.created',rid,{'dataset':name,'rules':rules})
                if not existing_dataset:db.execute('INSERT OR IGNORE INTO dataset_access VALUES(?,?,?)',(name,user['id'],'write'))
                if pending_connection:db.execute('INSERT INTO run_connectors(run_id,alias) VALUES(?,?)',(rid,pending_connection))
            except Exception:
                for path in paths: path.unlink(missing_ok=True)
                raise
        self.wake.set()
        return rid

    def validate_connection_payload(self,payload,user=None):
        connections=json.loads(os.environ.get('RECON_CONNECTIONS') or '{}')
        if payload.get('connection') not in connections: raise APIError(400,'Unknown database connection alias.')
        if user and self.enforce_acl and user['role']!='admin':
            bound=connections[payload['connection']].get('dataset')
            if not bound or payload.get('dataset')!=bound:raise APIError(403,'This connection requires its administrator-approved dataset binding.')
            with self.db() as db:self.require_dataset(db,user,bound,True)

    def worker(self):
        if os.environ.get('RECON_JOB_DATABASE_URL'):
            self.distributed_loop();return
        last_cleanup=0
        while not self.stop.is_set():
            if time.time()-last_cleanup>300:self.cleanup_uploads();last_cleanup=time.time()
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO worker_heartbeats VALUES(?,?,?)',('local',time.time(),'available'))
                row=db.execute("SELECT * FROM runs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
                if row: db.execute("UPDATE runs SET state='running',phase='Preparing snapshots' WHERE id=?",(row['id'],))
            if row:
                self.execute_run(dict(row))
            else:
                self.wake.wait(1);self.wake.clear()

    def execute_run(self,run):
        rid=run['id'];start=time.perf_counter();stage=self.data/f'{rid}.stage.sqlite'
        stage.unlink(missing_ok=True)
        # Unpublished events stay in a separate spool; failed runs never expose partial results.
        spool_path=self.data/f'{rid}.events.jsonl'
        try:
            self.prepare_run_inputs(run)
            with spool_path.open('w',encoding='utf-8') as spool:
                event_count=0
                def emit(event):
                    nonlocal event_count
                    event_count+=1
                    if event_count>int(os.environ.get('RECON_MAX_EXCEPTIONS','2000000')): raise ValueError('Run exceeds the configured exception-event limit. Partition the input datasets.')
                    if event_count%1000==0 and shutil.disk_usage(self.data).free<64*1024*1024:raise ValueError('Processing stopped because storage is nearly full.')
                    spool.write(canonical(event)+'\n')
                def progress(phase,count):
                    with self.db() as db: db.execute('UPDATE runs SET phase=? WHERE id=?',(phase,rid))
                summary=reconcile(self.uploads/run['source_file'],self.uploads/run['destination_file'],json.loads(run['rule_json']),stage,emit,progress)
            with self.db() as db:
                db.execute('DELETE FROM issues WHERE run_id=?',(rid,))
                with spool_path.open(encoding='utf-8') as spool:
                    for line in spool:
                        e=json.loads(line)
                        db.execute('INSERT INTO issues(run_id,category,key,col,severity,source,destination,detail) VALUES(?,?,?,?,?,?,?,?)',
                                   (rid,e['category'],e['key'],e['column'],e['severity'],canonical(e['source']),canonical(e['destination']),e['detail']))
                elapsed=time.perf_counter()-start
                db.execute("UPDATE runs SET state='completed',phase='Complete',summary=?,elapsed=? WHERE id=?",(canonical(summary),elapsed,rid))
                self.audit(db,'worker','run.completed',rid,{'fingerprint':summary['fingerprint'],'exceptions':summary['exception_count']})
        except Exception as exc:
            message=str(exc)[:500] if isinstance(exc,(ValueError,UnicodeError,csv.Error)) else 'Processing failed. Check server logs using the run ID.'
            LOG.error('Run %s failed (%s)',rid,type(exc).__name__)
            stage.unlink(missing_ok=True);spool_path.unlink(missing_ok=True)
            with self.db() as db:
                db.execute("UPDATE runs SET state='failed',phase='Failed',error=?,elapsed=? WHERE id=?",(message,time.perf_counter()-start,rid))
                self.audit(db,'worker','run.failed',rid,{'error':message})
        finally:
            stage.unlink(missing_ok=True);spool_path.unlink(missing_ok=True)

    def __call__(self,environ,start_response):
        request_id=uuid.uuid4().hex
        headers=[('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','same-origin'),
                 ('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                 ('Cache-Control','no-store'),('X-Request-ID',request_id)]
        if not self.demo: headers.append(('Strict-Transport-Security','max-age=31536000; includeSubDomains'))
        try:
            status,content_type,body,extra=self.route(environ)
            headers.extend(extra)
            if not isinstance(body,(bytes,FileBody)): body=canonical(body).encode()
        except APIError as exc:
            status,content_type,body=exc.status,'application/json',canonical({'error':exc.message,'request_id':request_id}).encode()
        except (ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
            status,content_type,body=400,'application/json',canonical({'error':str(exc)[:300],'request_id':request_id}).encode()
        except Exception:
            LOG.error('Request %s failed',request_id,exc_info=True)
            status,content_type,body=500,'application/json',canonical({'error':'Unexpected server error. Contact support with the request ID.','request_id':request_id}).encode()
        labels={200:'OK',201:'Created',202:'Accepted',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',409:'Conflict',413:'Payload Too Large',415:'Unsupported Media Type',429:'Too Many Requests',500:'Internal Server Error',502:'Bad Gateway',503:'Service Unavailable'}
        start_response(f'{status} {labels.get(status,"Error")}',headers+[('Content-Type',content_type),('Content-Length',str(body.length if isinstance(body,FileBody) else len(body)))])
        return body if isinstance(body,FileBody) else [body]

    def route(self,env):
        path=env.get('PATH_INFO','/');method=env.get('REQUEST_METHOD','GET')
        query=parse_qs(env.get('QUERY_STRING',''));extra=[]
        user=self.session(env)
        if path=='/metrics' and method=='GET':
            expected=os.environ.get('RECON_METRICS_TOKEN','')
            supplied=env.get('HTTP_AUTHORIZATION','').removeprefix('Bearer ')
            if len(expected)<32 or not hmac.compare_digest(supplied,expected):raise APIError(403,'Metrics authentication required.')
            return 200,'text/plain; version=0.0.4',self.metrics().encode(),[]
        payload={}
        if method in ('POST','PATCH','DELETE'):
            if env.get('CONTENT_TYPE','').split(';')[0]!='application/json': raise APIError(415,'Use application/json.')
            length=int(env.get('CONTENT_LENGTH') or 0)
            limit=MAX_BODY if path=='/api/runs' else 2*1024*1024 if re.fullmatch(r'/api/uploads/[a-f0-9]{32}',path) else 65536
            if length<0 or length>limit: raise APIError(413,'Request exceeds the endpoint size limit.')
            try: payload=json.loads(env['wsgi.input'].read(length) or b'{}')
            except (json.JSONDecodeError,UnicodeError): raise APIError(400,'Invalid JSON request.') from None
            if not isinstance(payload,dict): raise APIError(400,'Request body must be an object.')
            if path not in ('/api/login','/api/demo-login','/api/sso-login','/api/password-reset/request','/api/password-reset/complete'):
                self.authorize(user)
                if not hmac.compare_digest(str(env.get('HTTP_X_CSRF_TOKEN','')),user['csrf']): raise APIError(403,'Session verification failed. Refresh and try again.')
            origin=env.get('HTTP_ORIGIN')
            expected=('http' if self.demo else 'https')+'://'+env.get('HTTP_HOST','')
            allowed=os.environ.get('RECON_PUBLIC_URL',expected).rstrip('/')
            if origin and origin!=allowed: raise APIError(403,'Cross-origin request refused.')
        if path=='/healthz':
            with self.db() as db: db.execute('SELECT 1').fetchone()
            if hasattr(self,'thread') and not self.thread.is_alive(): raise APIError(503,'The reconciliation worker is unavailable.')
            if os.environ.get('RECON_JOB_DATABASE_URL') and not self.broker_ready:raise APIError(503,'The external job database or artifact storage is unavailable.')
            return 200,'application/json',{'status':'ok','version':'2.1.0'},extra
        if path=='/api/config' and method=='GET':
            return 200,'application/json',{'demo':self.demo,'default_rules':DEFAULT_RULES,'sso_enabled':bool(os.environ.get('RECON_SSO_PROXY_SECRET')),'sso_only':os.environ.get('RECON_SSO_ONLY')=='1','require_mfa':os.environ.get('RECON_REQUIRE_MFA')=='1' and os.environ.get('RECON_SSO_ONLY')!='1','max_upload_bytes':int(os.environ.get('RECON_MAX_UPLOAD_BYTES',1024**3))},extra
        if path=='/api/password-reset/request' and method=='POST':
            if os.environ.get('RECON_SSO_ONLY')=='1':raise APIError(403,'Use your identity provider for account recovery.')
            return 200,'application/json',self.reset_request(payload,env.get('REMOTE_ADDR','unknown')),extra
        if path=='/api/password-reset/complete' and method=='POST':
            if os.environ.get('RECON_SSO_ONLY')=='1':raise APIError(403,'Use your identity provider for account recovery.')
            return 200,'application/json',self.reset_complete({**payload,'_address':env.get('REMOTE_ADDR','unknown')}),extra
        if path in ('/api/login','/api/demo-login','/api/sso-login') and method=='POST':
            identity=str(payload.get('email','')).lower().strip()
            address='login:'+env.get('REMOTE_ADDR','unknown')+':'+hashlib.sha256(identity.encode()).hexdigest()
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                attempts=db.execute('SELECT * FROM login_attempts WHERE address=?',(address,)).fetchone()
                if attempts and attempts['failures']>=10 and time.time()-attempts['last']<900: raise APIError(429,'Too many attempts. Try again in 15 minutes.')
                if path=='/api/sso-login':
                    expected_secret=os.environ.get('RECON_SSO_PROXY_SECRET','')
                    if len(expected_secret)<32 or not hmac.compare_digest(env.get('HTTP_X_RECON_PROXY_SECRET',''),expected_secret):raise APIError(403,'Trusted identity proxy verification failed.')
                    email=env.get('HTTP_X_FORWARDED_EMAIL','').lower().strip()
                    record=db.execute('SELECT * FROM users WHERE email=? AND active=1',(email,)).fetchone()
                    if not record:raise APIError(403,'Your identity is not provisioned in this workspace.')
                elif path=='/api/demo-login':
                    if not self.demo: raise APIError(404,'Not found.')
                    record=db.execute('SELECT * FROM users WHERE id=? AND active=1',('admin',)).fetchone()
                else:
                    if os.environ.get('RECON_SSO_ONLY')=='1':raise APIError(403,'Use your organization single sign-on.')
                    record=db.execute('SELECT * FROM users WHERE email=? AND active=1',(str(payload.get('email','')).lower().strip(),)).fetchone()
                    password=str(payload.get('password',''))
                    if len(password)>200: raise APIError(400,'Password is too long.')
                    valid=password_matches(password,record['password'] if record else DUMMY_PASSWORD_HASH)
                    if valid and record:valid=self.factor_valid(db,record['id'],str(payload.get('code','')))
                    if not record or not valid:
                        failures=attempts['failures']+1 if attempts and time.time()-attempts['last']<900 else 1
                        db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(address,failures,time.time()))
                        # Commit failed attempts before returning the authentication error.
                        db.commit()
                        raise APIError(401,'Email or password is incorrect.')
                db.execute('DELETE FROM login_attempts WHERE address=?',(address,))
                token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
                db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
                db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(self.token_hash(token),record['id'],csrf,time.time()+28800))
                self.audit(db,record['id'],'session.created',record['id'],{'method':path.rsplit('/',1)[-1]})
            extra.append(('Set-Cookie',f'recon_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'+('' if self.demo else '; Secure')))
            return 200,'application/json',{'id':record['id'],'email':record['email'],'role':record['role'],'csrf':csrf},extra
        if path.startswith('/api/'):
            self.authorize(user)
            if os.environ.get('RECON_REQUIRE_MFA')=='1' and os.environ.get('RECON_SSO_ONLY')!='1' and path not in ('/api/me','/api/logout','/api/security','/api/security/enroll','/api/security/confirm'):
                with self.db() as db:factor=db.execute('SELECT secret FROM factors WHERE user_id=?',(user['id'],)).fetchone()
                if not factor or not factor['secret']:raise APIError(403,'Enroll an authenticator under Account security before accessing workspace data.')
            extended=self.enterprise_route(path,method,payload,user,env,query)
            if extended:return extended
            with self.db() as db:
                if path=='/api/me': return 200,'application/json',user,extra
                if path=='/api/logout' and method=='POST':
                    token=next((v.strip().partition('=')[2] for v in env.get('HTTP_COOKIE','').split(';') if v.strip().startswith('recon_session=')),'')
                    db.execute('DELETE FROM sessions WHERE token=?',(self.token_hash(token),))
                    self.audit(db,user['id'],'session.revoked',user['id'])
                    return 200,'application/json',{'ok':True},[('Set-Cookie','recon_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')]
                if path=='/api/connections' and method=='GET':
                    configured=json.loads(os.environ.get('RECON_CONNECTIONS') or '{}')
                    aliases=[alias for alias,spec in configured.items() if user['role']=='admin' or not self.enforce_acl or (spec.get('dataset') and self.dataset_allowed(db,user,spec['dataset']))]
                    return 200,'application/json',{'aliases':sorted(aliases)},extra
                if path=='/api/runs' and method=='POST':
                    self.authorize(user,'analyst')
                    rid=self.enqueue(user,payload,env.get('HTTP_IDEMPOTENCY_KEY'))
                    return 202,'application/json',{'id':rid},extra
                if path=='/api/runs' and method=='GET':
                    rows=db.execute('SELECT id,dataset,created,state,phase,summary,error,elapsed,rule_json FROM runs ORDER BY created DESC LIMIT 200').fetchall()
                    return 200,'application/json',{'runs':[self.run_json(r) for r in rows if self.dataset_allowed(db,user,r['dataset'])]},extra
                match=re.fullmatch(r'/api/runs/([a-f0-9]{32})(?:/(issues|export|snapshot))?',path)
                if match and method=='GET':
                    rid,action=match.groups();run=db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
                    if not run: raise APIError(404,'Run not found.')
                    self.require_dataset(db,user,run['dataset'])
                    if action is None: return 200,'application/json',self.run_json(run),extra
                    if action=='snapshot':
                        self.authorize(user,'admin')
                        side=query.get('side',['source'])[0]
                        if side not in ('source','destination'): raise APIError(400,'Unknown side.')
                        file=self.uploads/run[side+'_file']
                        self.audit(db,user['id'],'snapshot.downloaded',rid,{'side':side})
                        if run['state']=='archived':raise APIError(409,'Evidence is in the governed archive. Contact an administrator.')
                        return 200,'text/csv; charset=utf-8',FileBody(file),[('Content-Disposition',f'attachment; filename="{rid}-{side}.csv"')]
                    if run['state'] not in ('completed','archived'): raise APIError(409,'Results are available after a run completes.')
                    where=['run_id=?'];params=[rid]
                    for field in ('category','severity','status'):
                        if query.get(field,[''])[0]: where.append(field+'=?');params.append(query[field][0])
                    if query.get('q',[''])[0]:
                        term=query['q'][0][:200];where.append('(instr(lower(key),lower(?)) OR instr(lower(col),lower(?)) OR instr(lower(source),lower(?)) OR instr(lower(destination),lower(?)))');params.extend([term]*4)
                    clause=' AND '.join(where)
                    if action=='export':
                        self.audit(db,user['id'],'exceptions.exported',rid)
                        db.commit()
                        rows=db.execute('SELECT id,category,key,col,severity,source,destination,status,detail FROM issues WHERE '+clause+' ORDER BY id',params)  # nosec B608 - predicates are hard-coded above; every request value is bound in params.
                        export_headers=['id','category','key','column','severity','source','destination','status','detail']
                        if query.get('format',['csv'])[0]=='xlsx':
                            try: from openpyxl import Workbook
                            except ImportError: raise APIError(503,'Excel export support is not installed.') from None
                            workbook=Workbook(write_only=True);sheet=workbook.create_sheet('Exceptions');sheet.append(export_headers);row_index=1;sheet_index=1
                            from openpyxl.cell import WriteOnlyCell
                            for row in rows:
                                if row_index>=1048576:
                                    sheet_index+=1;sheet=workbook.create_sheet('Exceptions '+str(sheet_index));sheet.append(export_headers);row_index=1
                                cells=[]
                                for value in row:
                                    text=str(value)
                                    if len(text)>32767:text='[Value exceeds the Excel cell limit. Download CSV for complete evidence.]'
                                    cell=WriteOnlyCell(sheet,value=text);cell.data_type='s';cells.append(cell)
                                sheet.append(cells);row_index+=1
                            export=self.data/(uuid.uuid4().hex+'.export.xlsx');workbook.save(export)
                            return 200,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',FileBody(export,True),[('Content-Disposition',f'attachment; filename="exceptions-{rid[:8]}.xlsx"')]
                        export=self.data/(uuid.uuid4().hex+'.export.csv')
                        with export.open('x',encoding='utf-8-sig',newline='') as out:
                            writer=csv.writer(out);writer.writerow(export_headers)
                            for row in rows:
                                writer.writerow(["'"+str(v) if str(v).lstrip().startswith(('=','+','-','@','\t','\r')) else v for v in row])
                        return 200,'text/csv; charset=utf-8',FileBody(export,True),[('Content-Disposition',f'attachment; filename="exceptions-{rid[:8]}.csv"')]
                    page=max(1,min(100000,int(query.get('page',['1'])[0])))
                    total=db.execute('SELECT COUNT(*) FROM issues WHERE '+clause,params).fetchone()[0]  # nosec B608 - only fixed predicates enter clause; request values remain bound parameters.
                    rows=db.execute('SELECT * FROM issues WHERE '+clause+' ORDER BY id LIMIT 50 OFFSET ?',params+[(page-1)*50]).fetchall()  # nosec B608 - fixed SQL predicates and bound pagination/filter values.
                    issues=[]
                    for row in rows:
                        item=dict(row);item['source']=json.loads(item['source']);item['destination']=json.loads(item['destination'])
                        item['reviews']=[dict(r) for r in db.execute('SELECT reviews.*,users.email FROM reviews JOIN users ON reviews.actor=users.id WHERE issue_id=? ORDER BY reviews.id',(row['id'],))]
                        issues.append(item)
                    return 200,'application/json',{'issues':issues,'total':total,'page':page},extra
                match=re.fullmatch(r'/api/issues/(\d+)/review',path)
                if match and method=='POST':
                    self.authorize(user,'analyst');iid=int(match.group(1));decision=payload.get('decision');note=str(payload.get('note','')).strip()
                    if decision not in ('investigating','accepted','resolved','open') or not 10<=len(note)<=2000: raise APIError(400,'Choose a valid decision and provide a note of 10–2,000 characters.')
                    if decision=='accepted': self.authorize(user,'admin')
                    record=db.execute('SELECT issues.id,runs.dataset FROM issues JOIN runs ON runs.id=issues.run_id WHERE issues.id=?',(iid,)).fetchone()
                    if not record: raise APIError(404,'Exception not found.')
                    self.require_dataset(db,user,record['dataset'],True)
                    db.execute('INSERT INTO reviews(issue_id,actor,decision,note,created) VALUES(?,?,?,?,?)',(iid,user['id'],decision,note,now()))
                    db.execute('UPDATE issues SET status=? WHERE id=?',(decision,iid))
                    self.audit(db,user['id'],'issue.'+decision,str(iid),{'note_sha256':hashlib.sha256(note.encode()).hexdigest()})
                    return 200,'application/json',{'ok':True},extra
                if path=='/api/audit' and method=='GET':
                    self.authorize(user,'admin')
                    return 200,'application/json',{'events':[dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 200')]},extra
                if path=='/api/users' and method=='GET':
                    self.authorize(user,'admin')
                    return 200,'application/json',{'users':[dict(r) for r in db.execute('SELECT id,email,role,active FROM users ORDER BY email')]},extra
                if path=='/api/users' and method=='POST':
                    self.authorize(user,'admin')
                    email=str(payload.get('email','')).lower().strip();password=str(payload.get('password',''));role=payload.get('role')
                    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(email)>200 or not 12<=len(password)<=200 or role not in ROLES:
                        raise APIError(400,'Provide an email, a 12–200 character password, and a valid role.')
                    if db.execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone(): raise APIError(409,'A user with this email exists.')
                    uid=uuid.uuid4().hex;db.execute('INSERT INTO users(id,email,password,role) VALUES(?,?,?,?)',(uid,email,password_hash(password),role))
                    self.audit(db,user['id'],'user.created',uid,{'email':email,'role':role})
                    return 201,'application/json',{'id':uid},extra
                match=re.fullmatch(r'/api/users/([a-z0-9]+)',path)
                if match and method=='PATCH':
                    self.authorize(user,'admin');uid=match.group(1)
                    if uid==user['id']: raise APIError(409,'Ask another administrator to change your own access.')
                    if payload.get('active') not in (0,1): raise APIError(400,'Active must be 0 or 1.')
                    if not db.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone(): raise APIError(404,'Member not found.')
                    db.execute('UPDATE users SET active=? WHERE id=?',(payload['active'],uid));db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
                    self.audit(db,user['id'],'user.access_changed',uid,{'active':payload['active']})
                    return 200,'application/json',{'ok':True},extra
            raise APIError(404,'Endpoint not found.')
        if method!='GET': raise APIError(404,'Not found.')
        files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/enterprise.js':('enterprise.js','text/javascript; charset=utf-8'),'/styles.css':('styles.css','text/css; charset=utf-8')}
        if path not in files: raise APIError(404,'Not found.')
        file,kind=files[path]
        return 200,kind,(ROOT/'static'/file).read_bytes(),extra

    @staticmethod
    def run_json(row):
        item=dict(row)
        for key in ('source_file','destination_file','idempotency'): item.pop(key,None)
        item['summary']=json.loads(item['summary']) if item.get('summary') else None
        item['rules']=json.loads(item.pop('rule_json'))
        return item

    def close(self):
        self.stop.set();self.wake.set()
        if hasattr(self,'thread'): self.thread.join(timeout=10)
        if hasattr(self,'mail_thread'):self.mail_thread.join(timeout=12)
        if hasattr(self,'heartbeat_thread'):self.heartbeat_thread.join(timeout=2)
        if self.lock_file and (not hasattr(self,'thread') or not self.thread.is_alive()): self.lock_file.close()

def create_app():
    if os.environ.get('RECON_LOCAL_STAGE')=='1':raise RuntimeError('The disposable local staging flag cannot be used with the production factory.')
    logging.basicConfig(level=os.environ.get('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(name)s %(message)s')
    return Application(demo=False)
