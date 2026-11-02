"""Access, second factors, bounded uploads and operational policies.

Kept separate from request routing so these controls can be tested independently.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import struct
import time
from pathlib import Path
from urllib.parse import quote

def totp(secret, timestamp=None):
    counter=int((time.time() if timestamp is None else timestamp)//30)
    key=base64.b32decode(secret+'='*((8-len(secret)%8)%8))
    digest=hmac.new(key,struct.pack('>Q',counter),hashlib.sha1).digest()
    offset=digest[-1]&15
    return f'{(struct.unpack(">I",digest[offset:offset+4])[0]&0x7fffffff)%1000000:06}'

def valid_step(secret,code,last_step=-1,timestamp=None):
    clock=time.time() if timestamp is None else timestamp
    if not isinstance(code,str) or len(code)!=6 or not code.isdigit(): return None
    for step in (int(clock//30),int(clock//30)-1,int(clock//30)+1):
        if step>last_step and hmac.compare_digest(totp(secret,step*30),code):return step
    return None

class Enterprise:
    def upgrade(self):
        self.enforce_acl=os.environ.get('RECON_DATASET_ACL','0' if self.demo else '1')=='1'
        (self.data/'uploads').mkdir(exist_ok=True,mode=0o700)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS dataset_access(dataset TEXT,user_id TEXT REFERENCES users(id),permission TEXT CHECK(permission IN ('read','write')),PRIMARY KEY(dataset,user_id));
            CREATE TABLE IF NOT EXISTS factors(user_id TEXT PRIMARY KEY REFERENCES users(id),secret TEXT,pending TEXT,last_step INTEGER DEFAULT -1);
            CREATE TABLE IF NOT EXISTS recovery_codes(user_id TEXT REFERENCES users(id),digest TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS reset_tokens(token TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),expires REAL);
            CREATE TABLE IF NOT EXISTS mail_outbox(id TEXT PRIMARY KEY,user_id TEXT,recipient TEXT,body TEXT,state TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,created REAL);
            CREATE TABLE IF NOT EXISTS upload_sessions(id TEXT PRIMARY KEY,actor TEXT REFERENCES users(id),name TEXT,size INTEGER DEFAULT 0,sha256 TEXT,state TEXT DEFAULT 'uploading',created REAL);
            CREATE TABLE IF NOT EXISTS run_connectors(run_id TEXT PRIMARY KEY REFERENCES runs(id),alias TEXT,prepared INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS retention(run_id TEXT PRIMARY KEY REFERENCES runs(id),legal_hold INTEGER DEFAULT 0,archived TEXT);
            CREATE TABLE IF NOT EXISTS worker_heartbeats(id TEXT PRIMARY KEY,updated REAL,state TEXT);
            INSERT OR IGNORE INTO migrations VALUES(2,CURRENT_TIMESTAMP);
            ''')

    def cipher(self):
        from cryptography.fernet import Fernet
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('mfa:'+os.environ.get('RECON_MFA_KEY',self.secret)).encode()).digest()))

    def reset_request(self,payload,address):
        from server import APIError,now
        if not os.environ.get('RECON_SMTP_HOST'):raise APIError(503,'Password recovery email is not configured. Contact your administrator.')
        email=str(payload.get('email','')).lower().strip()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            key='reset:'+address+':'+hashlib.sha256(email.encode()).hexdigest()
            prior=db.execute('SELECT * FROM login_attempts WHERE address=?',(key,)).fetchone()
            if prior and prior['failures']>=5 and time.time()-prior['last']<900:raise APIError(429,'Please wait before requesting another recovery email.')
            count=prior['failures']+1 if prior and time.time()-prior['last']<900 else 1
            db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(key,count,time.time()))
            row=db.execute('SELECT id FROM users WHERE email=? AND active=1',(email,)).fetchone()
            if row:
                token=secrets.token_urlsafe(32);digest=self.token_hash(token)
                db.execute('DELETE FROM reset_tokens WHERE user_id=? OR expires<?',(row['id'],time.time()))
                db.execute('INSERT INTO reset_tokens VALUES(?,?,?)',(digest,row['id'],time.time()+900))
                self.audit(db,'recovery','password.reset_requested',row['id'])
                link=os.environ.get('RECON_PUBLIC_URL','').rstrip('/')+'/#reset='+quote(token)
                db.execute("DELETE FROM mail_outbox WHERE user_id=? AND state IN ('pending','failed')",(row['id'],))
                body=self.cipher().encrypt(link.encode()).decode()
                db.execute('INSERT INTO mail_outbox(id,user_id,recipient,body,created) VALUES(?,?,?,?,?)',(secrets.token_hex(16),row['id'],email,body,time.time()))
        return {'message':'If the account is eligible, a recovery link has been sent.'}

    @staticmethod
    def deliver_reset(email,link):
        import smtplib,ssl
        from email.message import EmailMessage
        message=EmailMessage();message['From']=os.environ['RECON_SMTP_FROM'];message['To']=email;message['Subject']='Reset your Reconcile password'
        message.set_content('A password reset was requested for your Reconcile account. This link expires in 15 minutes:\n\n'+link+'\n\nIf you did not request this, ignore this email. Your authenticator remains required if enabled.')
        with smtplib.SMTP(os.environ['RECON_SMTP_HOST'],int(os.environ.get('RECON_SMTP_PORT','587')),timeout=10) as smtp:
            if not (os.environ.get('RECON_LOCAL_STAGE')=='1' and os.environ['RECON_SMTP_HOST']=='mailpit'):smtp.starttls(context=ssl.create_default_context())
            if os.environ.get('RECON_SMTP_USER'):smtp.login(os.environ['RECON_SMTP_USER'],os.environ['RECON_SMTP_PASSWORD'])
            smtp.send_message(message)

    def reset_complete(self,payload):
        from server import APIError,password_hash
        token=str(payload.get('token',''));password=str(payload.get('password',''))
        self.enforce_rate('reset-complete:'+str(payload.get('_address','unknown'))+':'+self.token_hash(token))
        if not 12<=len(password)<=200:raise APIError(400,'Use a password of 12–200 characters.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT * FROM reset_tokens WHERE token=? AND expires>?',(self.token_hash(token),time.time())).fetchone()
            if not row or not self.factor_valid(db,row['user_id'],str(payload.get('code',''))):raise APIError(400,'Recovery token or authenticator code is invalid or expired.')
            db.execute('UPDATE users SET password=? WHERE id=?',(password_hash(password),row['user_id']));db.execute('DELETE FROM sessions WHERE user_id=?',(row['user_id'],));db.execute('DELETE FROM reset_tokens WHERE user_id=?',(row['user_id'],))
            self.audit(db,'recovery','password.reset_completed',row['user_id'])
        return {'ok':True}

    def enforce_rate(self,key,limit=10):
        from server import APIError
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT * FROM login_attempts WHERE address=?',(key,)).fetchone()
            count=row['failures'] if row and time.time()-row['last']<900 else 0
            if count>=limit:raise APIError(429,'Too many attempts. Try again in 15 minutes.')
            db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(key,count+1,time.time()))

    def dispatch_mail_once(self):
        with self.db() as db:
            db.execute("UPDATE mail_outbox SET state='expired',body='' WHERE state='pending' AND created<?",(time.time()-900,))
            row=db.execute("SELECT * FROM mail_outbox WHERE state='pending' AND next_attempt<=? ORDER BY created LIMIT 1",(time.time(),)).fetchone()
            if not row:return False
            db.execute("UPDATE mail_outbox SET state='sending',attempts=attempts+1 WHERE id=?",(row['id'],))
        try:
            link=self.cipher().decrypt(row['body'].encode()).decode();self.deliver_reset(row['recipient'],link)
            with self.db() as db:db.execute("UPDATE mail_outbox SET state='sent',body='' WHERE id=?",(row['id'],))
        except Exception:
            with self.db() as db:
                state='failed' if row['attempts']+1>=3 else 'pending'
                db.execute('UPDATE mail_outbox SET state=?,next_attempt=? WHERE id=?',(state,time.time()+30,row['id']))
        return True

    def mail_loop(self):
        import logging
        while not self.stop.is_set():
            try:self.dispatch_mail_once()
            except Exception:logging.getLogger('reconcile').warning('Recovery outbox is temporarily unavailable')
            self.stop.wait(2)

    def heartbeat_loop(self):
        while not self.stop.wait(10):
            if not self.thread.is_alive():return
            try:
                with self.db() as db:db.execute('UPDATE worker_heartbeats SET updated=?',(time.time(),))
            except Exception:pass

    def factor_valid(self,db,uid,code):
        row=db.execute('SELECT * FROM factors WHERE user_id=?',(uid,)).fetchone()
        if not row or not row['secret']:return True
        if isinstance(code,str) and len(code)==24:
            digest=hashlib.sha256(code.upper().encode()).hexdigest()
            return db.execute('DELETE FROM recovery_codes WHERE user_id=? AND digest=? RETURNING digest',(uid,digest)).fetchone() is not None
        secret=self.cipher().decrypt(row['secret'].encode()).decode()
        step=valid_step(secret,code,row['last_step'])
        if step is None:
            digest=hashlib.sha256(str(code).upper().encode()).hexdigest()
            recovery=db.execute('DELETE FROM recovery_codes WHERE user_id=? AND digest=? RETURNING digest',(uid,digest)).fetchone()
            return recovery is not None
        db.execute('UPDATE factors SET last_step=? WHERE user_id=?',(step,uid));return True

    def dataset_allowed(self,db,user,dataset,write=False):
        if user['role']=='admin' or not self.enforce_acl:return True
        row=db.execute('SELECT permission FROM dataset_access WHERE dataset=? AND user_id=?',(dataset,user['id'])).fetchone()
        return bool(row and (not write or row['permission']=='write'))

    def require_dataset(self,db,user,dataset,write=False):
        from server import APIError
        if not self.dataset_allowed(db,user,dataset,write):raise APIError(403,'You do not have access to this dataset.')

    def metrics(self):
        with self.db() as db:
            counts={r[0]:r[1] for r in db.execute('SELECT state,COUNT(*) FROM runs GROUP BY state')}
            pending=db.execute("SELECT created FROM runs WHERE state='queued' OR phase='Waiting for remote worker' ORDER BY created LIMIT 1").fetchone()
            durations=db.execute('SELECT COALESCE(SUM(elapsed),0),COUNT(elapsed) FROM runs').fetchone()
            heartbeats=[dict(r) for r in db.execute('SELECT * FROM worker_heartbeats')]
        free=shutil.disk_usage(self.data).free
        result=''
        for state in ('queued','running','completed','failed','archived'):
            result+=f'reconcile_runs{{state="{state}"}} {counts.get(state,0)}\n'
        result+=f'reconcile_disk_free_bytes {free}\nreconcile_run_duration_seconds_sum {durations[0]}\nreconcile_run_duration_seconds_count {durations[1]}\n'
        result+=f'reconcile_worker_heartbeat_age_seconds {max([time.time()-r["updated"] for r in heartbeats] or [0])}\n'
        from datetime import datetime
        age=max(0,time.time()-datetime.fromisoformat(pending[0]).timestamp()) if pending else 0
        result+=f'reconcile_oldest_queued_seconds {age}\n'
        return result

    def enterprise_route(self,path,method,payload,user,env,query):
        from server import APIError,now,password_matches,password_hash
        from engine import canonical
        result=lambda body,status=200:(status,'application/json',body,[])
        if path=='/api/operations' and method=='GET':
            self.authorize(user,'admin')
            with self.db() as db:
                counts={r[0]:r[1] for r in db.execute('SELECT state,COUNT(*) FROM runs GROUP BY state')}
                workers=[dict(r) for r in db.execute('SELECT * FROM worker_heartbeats')]
            free=shutil.disk_usage(self.data).free
            alerts=[]
            if counts.get('failed',0):alerts.append({'severity':'warning','message':f'{counts["failed"]} failed runs require review.'})
            if free<1024**3:alerts.append({'severity':'critical','message':'Free data-volume storage is below 1 GiB.'})
            if any(time.time()-w['updated']>120 for w in workers):alerts.append({'severity':'critical','message':'Worker heartbeat is stale.'})
            with self.db() as db:mail_failed=db.execute("SELECT COUNT(*) FROM mail_outbox WHERE state='failed'").fetchone()[0]
            if mail_failed:alerts.append({'severity':'warning','message':f'{mail_failed} recovery messages could not be delivered.'})
            return result({'runs':counts,'workers':workers,'free_bytes':free,'alerts':alerts,'acl_enforced':self.enforce_acl})
        if path=='/api/retention' and method=='GET':
            self.authorize(user,'admin')
            with self.db() as db:
                rows=[dict(r) for r in db.execute('SELECT runs.id,runs.dataset,runs.created,runs.state,COALESCE(retention.legal_hold,0) AS legal_hold,retention.archived IS NOT NULL AS archived FROM runs LEFT JOIN retention ON runs.id=retention.run_id ORDER BY runs.created DESC LIMIT 200')]
            return result({'runs':rows})
        if path=='/api/access':
            self.authorize(user,'admin')
            with self.db() as db:
                if method=='GET':return result({'grants':[dict(r) for r in db.execute('SELECT dataset_access.*,users.email FROM dataset_access JOIN users ON users.id=dataset_access.user_id')]})
                if method=='POST':
                    dataset=str(payload.get('dataset','')).strip();uid=payload.get('user_id');permission=payload.get('permission')
                    if not dataset or len(dataset)>120 or permission not in ('read','write','revoke'):raise APIError(400,'Choose a dataset, member, and read/write/revoke permission.')
                    if not db.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone():raise APIError(404,'Member not found.')
                    if permission=='revoke':db.execute('DELETE FROM dataset_access WHERE dataset=? AND user_id=?',(dataset,uid))
                    else:db.execute('INSERT OR REPLACE INTO dataset_access VALUES(?,?,?)',(dataset,uid,permission))
                    self.audit(db,user['id'],'dataset.access_changed',dataset,{'member':uid,'permission':permission})
                    return result({'ok':True})
        if path=='/api/security' and method=='GET':
            with self.db() as db:row=db.execute('SELECT secret FROM factors WHERE user_id=?',(user['id'],)).fetchone()
            return result({'mfa_enabled':bool(row and row['secret']),'sso_enabled':bool(os.environ.get('RECON_SSO_PROXY_SECRET'))})
        if path=='/api/security/enroll' and method=='POST':
            self.enforce_rate('mfa-enroll:'+user['id'],5)
            with self.db() as db:
                row=db.execute('SELECT password FROM users WHERE id=?',(user['id'],)).fetchone()
                if not password_matches(str(payload.get('password','')),row['password']):raise APIError(401,'Re-enter your current password.')
                existing=db.execute('SELECT secret FROM factors WHERE user_id=?',(user['id'],)).fetchone()
                if existing and existing['secret']:raise APIError(409,'A second factor is already enrolled.')
                secret=base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
                encrypted=self.cipher().encrypt(secret.encode()).decode()
                db.execute('INSERT INTO factors(user_id,pending) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET pending=excluded.pending',(user['id'],encrypted))
                self.audit(db,user['id'],'mfa.enrollment_started',user['id'])
            return result({'secret':secret,'uri':f'otpauth://totp/Reconcile:{quote(user["email"])}?secret={secret}&issuer=Reconcile&digits=6&period=30'})
        if path=='/api/security/confirm' and method=='POST':
            self.enforce_rate('mfa-confirm:'+user['id'])
            with self.db() as db:
                row=db.execute('SELECT * FROM factors WHERE user_id=?',(user['id'],)).fetchone()
                if not row or not row['pending']:raise APIError(409,'Start enrollment first.')
                secret=self.cipher().decrypt(row['pending'].encode()).decode();step=valid_step(secret,str(payload.get('code','')))
                if step is None:raise APIError(400,'Authenticator code is incorrect or expired.')
                db.execute('UPDATE factors SET secret=pending,pending=NULL,last_step=? WHERE user_id=?',(step,user['id']))
                codes=[secrets.token_hex(12).upper() for _ in range(10)]
                db.execute('DELETE FROM recovery_codes WHERE user_id=?',(user['id'],))
                for code in codes:db.execute('INSERT INTO recovery_codes VALUES(?,?)',(user['id'],hashlib.sha256(code.encode()).hexdigest()))
                self.audit(db,user['id'],'mfa.enabled',user['id'])
            return result({'ok':True,'recovery_codes':codes})
        if path=='/api/security/password' and method=='POST':
            self.enforce_rate('password-change:'+user['id'],5)
            with self.db() as db:
                row=db.execute('SELECT password FROM users WHERE id=?',(user['id'],)).fetchone();new=str(payload.get('new_password',''))
                if not 12<=len(new)<=200:raise APIError(400,'Use a password of 12–200 characters.')
                if not password_matches(str(payload.get('password','')),row['password']) or not self.factor_valid(db,user['id'],str(payload.get('code',''))):raise APIError(401,'Password or authenticator code is incorrect.')
                db.execute('UPDATE users SET password=? WHERE id=?',(password_hash(new),user['id']));db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
                self.audit(db,user['id'],'user.password_changed',user['id'])
            return result({'ok':True,'sign_in_required':True})
        if path=='/api/security/disable' and method=='POST':
            self.enforce_rate('mfa-replace:'+user['id'],5)
            with self.db() as db:
                stored=db.execute('SELECT password FROM users WHERE id=?',(user['id'],)).fetchone()[0]
                factor=db.execute('SELECT secret FROM factors WHERE user_id=?',(user['id'],)).fetchone()
                if not factor or not factor[0]:raise APIError(409,'No authenticator is enrolled.')
                if not password_matches(str(payload.get('password','')),stored) or not self.factor_valid(db,user['id'],str(payload.get('code',''))):raise APIError(401,'Current password and an authenticator or unused recovery code are required.')
                db.execute('DELETE FROM factors WHERE user_id=?',(user['id'],));db.execute('DELETE FROM recovery_codes WHERE user_id=?',(user['id'],));db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
                self.audit(db,user['id'],'mfa.removed_for_replacement',user['id'])
            return result({'ok':True,'sign_in_required':True})
        if path=='/api/uploads' and method=='POST':
            self.authorize(user,'analyst')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT COUNT(*) FROM upload_sessions WHERE actor=? AND state='uploading'",(user['id'],)).fetchone()[0]>=4:raise APIError(429,'Complete existing uploads before starting another.')
                uid=secrets.token_hex(16)
                db.execute('INSERT INTO upload_sessions(id,actor,name,created) VALUES(?,?,?,?)',(uid,user['id'],str(payload.get('name','dataset.csv'))[:200],time.time()))
            return result({'id':uid,'chunk_bytes':1024*1024,'max_bytes':int(os.environ.get('RECON_MAX_UPLOAD_BYTES',1024**3))},201)
        import re
        hold=re.fullmatch(r'/api/runs/([a-f0-9]{32})/hold',path)
        if hold and method=='POST':
            self.authorize(user,'admin');rid=hold.group(1)
            if not isinstance(payload.get('hold'),bool):raise APIError(400,'Hold must be true or false.')
            if not 10<=len(str(payload.get('reason','')).strip())<=2000:raise APIError(400,'Provide a legal-hold rationale of 10–2,000 characters.')
            with self.db() as db:
                if not db.execute('SELECT 1 FROM runs WHERE id=?',(rid,)).fetchone():raise APIError(404,'Run not found.')
                db.execute('INSERT INTO retention(run_id,legal_hold) VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET legal_hold=excluded.legal_hold',(rid,int(payload['hold'])))
                self.audit(db,user['id'],'run.legal_hold_changed',rid,{'hold':payload['hold'],'reason':str(payload['reason'])[:2000]})
            return result({'ok':True})
        match=re.fullmatch(r'/api/uploads/([a-f0-9]{32})(?:/(complete))?',path)
        if match:
            self.authorize(user,'analyst');uid,action=match.groups();file=self.data/'uploads'/uid
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT * FROM upload_sessions WHERE id=? AND actor=?',(uid,user['id'])).fetchone()
                if not row:raise APIError(404,'Upload not found.')
                if method=='GET':return result({k:row[k] for k in ('id','size','state','sha256')})
                if row['state']!='uploading':raise APIError(409,'This upload is sealed.')
                if action=='complete' and method=='POST':
                    if not file.exists() or not row['size']:raise APIError(400,'Upload contains no data.')
                    with file.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    if payload.get('sha256') and payload['sha256']!=digest:raise APIError(409,'Checksum differs from the uploaded content.')
                    # Validate UTF-8 incrementally, preserving original bytes.
                    try:
                        with file.open(encoding='utf-8-sig') as stream:
                            while stream.read(1024*1024):pass
                    except UnicodeError:raise APIError(400,'Upload must use UTF-8.') from None
                    db.execute("UPDATE upload_sessions SET state='sealed',sha256=? WHERE id=?",(digest,uid));self.audit(db,user['id'],'upload.sealed',uid,{'size':row['size'],'sha256':digest})
                    return result({'id':uid,'sha256':digest,'bytes':row['size']})
                if method=='POST' and not action:
                    try:block=base64.b64decode(payload.get('data',''),validate=True)
                    except Exception:raise APIError(400,'Invalid upload chunk.') from None
                    if not block or len(block)>1024*1024:raise APIError(413,'Chunk must contain 1–1,048,576 bytes.')
                    if payload.get('sha256') and hashlib.sha256(block).hexdigest()!=payload['sha256']:raise APIError(409,'Chunk checksum mismatch.')
                    if payload.get('offset')!=row['size']:raise APIError(409,'Chunk offset does not match the saved upload position.')
                    if row['size']+len(block)>int(os.environ.get('RECON_MAX_UPLOAD_BYTES',1024**3)):raise APIError(413,'Upload exceeds the configured limit.')
                    used=db.execute('SELECT COALESCE(SUM(size),0) FROM upload_sessions WHERE actor=?',(user['id'],)).fetchone()[0]
                    if used+len(block)>int(os.environ.get('RECON_UPLOAD_QUOTA_BYTES',4*1024**3)):raise APIError(413,'Your temporary upload-storage quota has been reached.')
                    if shutil.disk_usage(self.data).free<len(block)+256*1024*1024:raise APIError(503,'Insufficient free disk storage.')
                    with file.open('ab') as stream:
                        stream.truncate(row['size']);stream.write(block);stream.flush();os.fsync(stream.fileno())
                    db.execute('UPDATE upload_sessions SET size=size+? WHERE id=?',(len(block),uid))
                    return result({'offset':row['size']+len(block)})
        return None

    def distributed_loop(self):
        import logging
        from distributed import Broker,Artifacts
        from server import now
        from engine import canonical
        broker=artifacts=None;last_cleanup=0
        while not self.stop.is_set():
            try:
                if time.time()-last_cleanup>300:self.cleanup_uploads();last_cleanup=time.time()
                if broker is None:broker=Broker();artifacts=Artifacts()
                self.broker_ready=True
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO worker_heartbeats VALUES(?,?,?)',('coordinator',time.time(),'available'))
                    runs=[dict(r) for r in db.execute("SELECT * FROM runs WHERE state IN ('queued','running') ORDER BY created")]
                for run in runs:
                    job=broker.get(run['id'])
                    if job is None:
                        try:self.prepare_run_inputs(run)
                        except Exception:
                            with self.db() as db:
                                db.execute("UPDATE runs SET state='failed',phase='Database ingestion failed',error='Could not read the configured database snapshots. Check connection settings and server logs.' WHERE id=?",(run['id'],))
                                self.audit(db,'coordinator','run.failed',run['id'],{'stage':'ingestion'})
                            continue
                        keys=[];hashes=[]
                        for side in ('source','destination'):
                            file=self.uploads/run[side+'_file']
                            with file.open('rb') as stream:hashes.append(hashlib.file_digest(stream,'sha256').hexdigest())
                            keys.append(artifacts.put(file,f'inputs/{run["id"]}/{side}.csv'))
                        broker.submit(run['id'],{'source':keys[0],'destination':keys[1],'source_sha256':hashes[0],'destination_sha256':hashes[1],'rules':json.loads(run['rule_json'])})
                    elif job['state']=='completed':
                        output=self.data/(run['id']+'.remote.jsonl')
                        try:
                            artifacts.get(job['result']['events'],output)
                            with output.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                            if digest!=job['result']['events_sha256']:raise ValueError('Remote exception artifact checksum mismatch.')
                            summary=job['result']['summary']
                            if summary['source_sha256']!=job['payload']['source_sha256'] or summary['destination_sha256']!=job['payload']['destination_sha256']:raise ValueError('Remote result refers to different inputs.')
                            with self.db() as db:
                                db.execute('DELETE FROM issues WHERE run_id=?',(run['id'],));count=0
                                with output.open(encoding='utf-8') as stream:
                                    for line in stream:
                                        event=json.loads(line);count+=1
                                        db.execute('INSERT INTO issues(run_id,category,key,col,severity,source,destination,detail) VALUES(?,?,?,?,?,?,?,?)',(run['id'],event['category'],event['key'],event['column'],event['severity'],canonical(event['source']),canonical(event['destination']),event['detail']))
                                if count!=summary['exception_count']:raise ValueError('Remote exception count does not match the summary.')
                                elapsed=job['result']['worker_seconds']
                                db.execute("UPDATE runs SET state='completed',phase='Remote comparison complete',summary=?,elapsed=? WHERE id=?",(canonical(summary),elapsed,run['id']))
                                self.audit(db,'coordinator','run.completed',run['id'],{'remote':True,'attempts':job['attempts'],'fingerprint':summary['fingerprint']})
                        finally:output.unlink(missing_ok=True)
                    elif job['state']=='failed':
                        with self.db() as db:
                            db.execute("UPDATE runs SET state='failed',phase='Remote processing failed',error=? WHERE id=?",(job['error'],run['id']));self.audit(db,'coordinator','run.failed',run['id'],{'remote':True})
                    else:
                        with self.db() as db:db.execute("UPDATE runs SET state='running',phase=? WHERE id=?",('Remote worker comparing records' if job['state']=='running' else 'Waiting for remote worker',run['id']))
            except Exception as exc:
                self.broker_ready=False
                logging.getLogger('reconcile').warning('External job coordinator retry (%s)',type(exc).__name__)
                broker=None;self.stop.wait(10)
            self.stop.wait(1)

    def cleanup_uploads(self):
        with self.db() as db:
            expired=[r[0] for r in db.execute('SELECT id FROM upload_sessions WHERE created<?',(time.time()-86400,))]
            db.execute('DELETE FROM upload_sessions WHERE created<?',(time.time()-86400,))
        for uid in expired:(self.data/'uploads'/uid).unlink(missing_ok=True)
        return len(expired)

    def prepare_run_inputs(self,run):
        from server import APIError
        from engine import canonical
        with self.db() as db:connector=db.execute('SELECT * FROM run_connectors WHERE run_id=? AND prepared=0',(run['id'],)).fetchone()
        if not connector:return
        try:import psycopg
        except ImportError:raise APIError(503,'PostgreSQL support is not installed.') from None
        from psycopg import sql
        import csv
        spec=json.loads(os.environ.get('RECON_CONNECTIONS') or '{}').get(connector['alias'])
        if not spec:raise ValueError('The configured connection alias is unavailable.')
        temporary=[];schemas={}
        try:
            for side in ('source','destination'):
                cfg=spec[side];target=self.uploads/run[side+'_file'];partial=target.with_suffix('.ingesting');temporary.append((partial,target))
                if not self.demo and psycopg.conninfo.conninfo_to_dict(os.environ[cfg['dsn_env']]).get('sslmode')!='verify-full':raise ValueError('Production source and destination connections require sslmode=verify-full.')
                with psycopg.connect(os.environ[cfg['dsn_env']],connect_timeout=10) as connection,connection.transaction():
                    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    connection.execute("SET LOCAL statement_timeout='60s'")
                    schemas[side]=dict(connection.execute('SELECT a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod) FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON a.attrelid=c.oid JOIN pg_catalog.pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum',(cfg.get('schema','public'),cfg['table'])).fetchall())
                    with connection.cursor(name='reconcile_ingestion') as cursor,partial.open('w',encoding='utf-8',newline='') as stream:
                        cursor.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(cfg.get('schema','public')),sql.Identifier(cfg['table'])))
                        rows=cursor.fetchmany(1000);writer=csv.writer(stream);writer.writerow([c.name for c in cursor.description]);count=0
                        while rows:
                            writer.writerows(rows);count+=len(rows)
                            if stream.tell()>int(os.environ.get('RECON_MAX_UPLOAD_BYTES',1024**3)) or count>int(os.environ.get('RECON_MAX_RECORDS',5000000)):raise ValueError('Database snapshot exceeds the configured ingestion limit.')
                            rows=cursor.fetchmany(1000)
            for partial,target in temporary:os.replace(partial,target)
            rules=json.loads(run['rule_json']);rules['schemas']=schemas;run['rule_json']=canonical(rules)
            with self.db() as db:
                db.execute('UPDATE runs SET rule_json=? WHERE id=?',(run['rule_json'],run['id']));db.execute('UPDATE run_connectors SET prepared=1 WHERE run_id=?',(run['id'],))
                self.audit(db,'ingestion','snapshots.captured',run['id'],{'alias':connector['alias'],'schemas':schemas})
        except Exception as exc:
            if isinstance(exc,ValueError):raise
            raise ValueError('Configured PostgreSQL ingestion failed; verify server settings and read-only privileges.') from None
        finally:
            for partial,target in temporary:partial.unlink(missing_ok=True)
