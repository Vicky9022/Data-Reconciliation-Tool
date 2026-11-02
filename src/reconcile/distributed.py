"""PostgreSQL leases + S3 artifacts. Remote workers never access the UI database.

All published result keys include an attempt identifier. A lost lease cannot
complete a job, preventing late workers from replacing the winning result.
"""
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from .engine import canonical,reconcile

class Broker:
    def __init__(self,migrate=None):
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg=psycopg;self.row_factory=dict_row
        migrate=migrate if migrate is not None else os.environ.get('RECON_LOCAL_STAGE')=='1' or os.environ.get('RECON_JOB_MIGRATE')=='1'
        if not migrate:
            with self.connect() as db:db.execute('SELECT id FROM reconcile_jobs LIMIT 0')
            return
        with self.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(63950101)')
            db.execute('''CREATE TABLE IF NOT EXISTS reconcile_jobs(
                id TEXT PRIMARY KEY,payload JSONB NOT NULL,state TEXT NOT NULL DEFAULT 'queued',
                owner TEXT,lease_until DOUBLE PRECISION DEFAULT 0,result JSONB,error TEXT,
                created DOUBLE PRECISION NOT NULL,attempts INTEGER DEFAULT 0)''')
            db.execute('CREATE INDEX IF NOT EXISTS reconcile_jobs_claim ON reconcile_jobs(state,lease_until,created)')
    def connect(self):
        dsn=os.environ['RECON_JOB_DATABASE_URL']
        if os.environ.get('RECON_LOCAL_STAGE')!='1' and self.psycopg.conninfo.conninfo_to_dict(dsn).get('sslmode')!='verify-full':raise ValueError('Remote job-database connections require sslmode=verify-full.')
        return self.psycopg.connect(dsn,row_factory=self.row_factory,connect_timeout=10)
    def get(self,rid):
        with self.connect() as db:return db.execute('SELECT * FROM reconcile_jobs WHERE id=%s',(rid,)).fetchone()
    def submit(self,rid,payload):
        with self.connect() as db:db.execute('INSERT INTO reconcile_jobs(id,payload,created) VALUES(%s,%s::jsonb,%s) ON CONFLICT(id) DO NOTHING',(rid,canonical(payload),time.time()))
    def claim(self):
        owner=secrets.token_hex(16);clock=time.time()
        with self.connect() as db:
            return db.execute('''WITH next AS (SELECT id FROM reconcile_jobs WHERE state='queued' OR (state='running' AND lease_until<%s)
                ORDER BY created FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE reconcile_jobs j SET state='running',owner=%s,lease_until=%s,attempts=attempts+1
                FROM next WHERE j.id=next.id RETURNING j.*''',(clock,owner,clock+120)).fetchone()
    def renew(self,rid,owner):
        with self.connect() as db:return db.execute("UPDATE reconcile_jobs SET lease_until=%s WHERE id=%s AND owner=%s AND state='running' AND lease_until>%s RETURNING id",(time.time()+120,rid,owner,time.time())).fetchone() is not None
    def finish(self,rid,owner,result=None,error=None):
        with self.connect() as db:
            return db.execute("UPDATE reconcile_jobs SET state=%s,result=%s::jsonb,error=%s WHERE id=%s AND owner=%s AND state='running' AND lease_until>%s RETURNING id",('failed' if error else 'completed',canonical(result) if result else None,error,rid,owner,time.time())).fetchone() is not None

class Artifacts:
    def __init__(self):
        import boto3
        self.client=boto3.client('s3',endpoint_url=os.environ.get('RECON_S3_ENDPOINT') or None)
        self.bucket=os.environ['RECON_WORK_BUCKET']
        self.client.head_bucket(Bucket=self.bucket)
    def put(self,file,key):
        extra={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':os.environ['RECON_WORK_KMS_KEY']} if os.environ.get('RECON_WORK_KMS_KEY') else {'ServerSideEncryption':'AES256'}
        if os.environ.get('RECON_LOCAL_STAGE')=='1' and os.environ.get('RECON_S3_ENDPOINT','').startswith('http://minio:'):extra={}
        self.client.upload_file(str(file),self.bucket,key,ExtraArgs=extra);return key
    def get(self,key,file):self.client.download_file(self.bucket,key,str(file))

def execute_claim(broker,artifacts,job):
    start=time.perf_counter();owner=job['owner'];rid=job['id'];stop=threading.Event();lost=threading.Event()
    def heartbeat():
        while not stop.wait(20):
            try:
                if not broker.renew(rid,owner):lost.set();return
            except Exception:lost.set();return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='reconcile-worker-') as folder:
            root=Path(folder);payload=job['payload'];a=root/'source.csv';b=root/'destination.csv';events=root/'events.jsonl'
            artifacts.get(payload['source'],a);artifacts.get(payload['destination'],b)
            import hashlib
            for path,expected in ((a,payload['source_sha256']),(b,payload['destination_sha256'])):
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=expected:raise ValueError('Input artifact checksum verification failed.')
            with events.open('w',encoding='utf-8') as spool:
                count=0
                def emit(event):
                    nonlocal count
                    count+=1
                    if lost.is_set():raise RuntimeError('Lease ownership lost')
                    if count>int(os.environ.get('RECON_MAX_EXCEPTIONS','2000000')):raise ValueError('Exception-event limit exceeded.')
                    spool.write(canonical(event)+'\n')
                def progress(phase,count):
                    if lost.is_set():raise RuntimeError('Lease ownership lost')
                summary=reconcile(a,b,payload['rules'],root/'index.sqlite',emit,progress)
            if lost.is_set():return False
            key=artifacts.put(events,f'results/{rid}/{owner}.jsonl')
            with events.open('rb') as stream:events_hash=hashlib.file_digest(stream,'sha256').hexdigest()
            return broker.finish(rid,owner,{'summary':summary,'events':key,'events_sha256':events_hash,'worker_seconds':time.perf_counter()-start})
    except Exception as exc:
        # Do not expose infrastructure exceptions, credentials, or object URLs.
        message=str(exc)[:300] if isinstance(exc,ValueError) else 'Remote processing failed; inspect worker logs by job ID.'
        return broker.finish(rid,owner,error=message)
    finally:stop.set();thread.join(timeout=2)

def run_worker(once=False):
    broker=Broker();artifacts=Artifacts()
    while True:
        job=broker.claim()
        if job:execute_claim(broker,artifacts,job)
        elif once:return
        else:time.sleep(1)

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--migrate',action='store_true');args=parser.parse_args()
    if args.migrate:Broker(migrate=True)
    else:run_worker()
