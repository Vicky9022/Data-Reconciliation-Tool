"""Verified encrypted archives and explicit retention. No purge occurs by default."""
import base64
import hashlib
import json
import os
import tempfile
import zipfile
from datetime import datetime,timezone,timedelta
from pathlib import Path

MAGIC=b'RECON-ARCHIVE-1\n'

def archive_key():
    text=os.environ.get('RECON_ARCHIVE_KEY','')
    try:key=base64.urlsafe_b64decode(text)
    except Exception:raise ValueError('RECON_ARCHIVE_KEY must be a base64-encoded 32-byte key.') from None
    if len(key)!=32:raise ValueError('Set a separate 32-byte RECON_ARCHIVE_KEY before archiving.')
    return key

def encrypt_file(source,destination):
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    nonce=os.urandom(12);encryptor=Cipher(algorithms.AES(archive_key()),modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    with open(source,'rb') as incoming,open(destination,'xb') as outgoing:
        os.chmod(destination,0o600)
        outgoing.write(MAGIC+nonce)
        for block in iter(lambda:incoming.read(1024*1024),b''):outgoing.write(encryptor.update(block))
        outgoing.write(encryptor.finalize());outgoing.write(encryptor.tag);outgoing.flush();os.fsync(outgoing.fileno())

def decrypt_file(source,destination):
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    with open(source,'rb') as incoming:
        if incoming.read(len(MAGIC))!=MAGIC:raise ValueError('Unknown archive format.')
        nonce=incoming.read(12);incoming.seek(-16,2);tag=incoming.read(16)
        remaining=incoming.tell()-len(MAGIC)-12-16;incoming.seek(len(MAGIC)+12)
        decryptor=Cipher(algorithms.AES(archive_key()),modes.GCM(nonce,tag)).decryptor();decryptor.authenticate_additional_data(MAGIC)
        with open(destination,'wb') as outgoing:
            while remaining:
                block=incoming.read(min(1024*1024,remaining))
                if not block:raise ValueError('Truncated encrypted archive.')
                remaining-=len(block);outgoing.write(decryptor.update(block))
            outgoing.write(decryptor.finalize())

def locked_s3_copy(path,retention_days):
    """Optional independent COMPLIANCE-mode object-lock copy; no local fallback."""
    import boto3
    bucket=os.environ['RECON_ARCHIVE_S3_BUCKET']
    client=boto3.client('s3')
    state=client.get_object_lock_configuration(Bucket=bucket)
    if state.get('ObjectLockConfiguration',{}).get('ObjectLockEnabled')!='Enabled':raise ValueError('Archive bucket must have Object Lock enabled.')
    if path.stat().st_size>5*1024**3:raise ValueError('Archive exceeds the supported single-object 5 GiB limit.')
    with path.open('rb') as stream:
        md5=hashlib.file_digest(stream,'md5').digest();stream.seek(0)
        response=client.put_object(Bucket=bucket,Key='reconcile/'+path.name,Body=stream,
          ContentMD5=base64.b64encode(md5).decode(),ServerSideEncryption='aws:kms',SSEKMSKeyId=os.environ['RECON_ARCHIVE_KMS_KEY'],
          ObjectLockMode='COMPLIANCE',ObjectLockRetainUntilDate=datetime.now(timezone.utc)+timedelta(days=retention_days))
    version=response.get('VersionId')
    if not version:raise ValueError('Object storage did not return an immutable version identifier.')
    return {'bucket':bucket,'key':'reconcile/'+path.name,'version':version}

def retain(app,archive_dir,days=90,apply=False,purge=False):
    if days<1:raise ValueError('Retention age must be at least one day.')
    if purge and not apply:raise ValueError('Purging requires explicit --apply.')
    directory=Path(archive_dir).resolve()
    if directory==app.data or app.data in directory.parents:raise ValueError('Archive destination must be outside the application data directory.')
    cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat();report=[]
    with app.db() as db:
        candidates=[dict(r) for r in db.execute("SELECT runs.*,COALESCE(retention.legal_hold,0) AS legal_hold,retention.archived FROM runs LEFT JOIN retention ON retention.run_id=runs.id WHERE runs.created<? AND runs.state IN ('completed','failed')",(cutoff,))]
    if apply:
        if app.lock_file is None:app.acquire_lock()
        directory.mkdir(parents=True,exist_ok=True,mode=0o700);archive_key()
    for run in candidates:
        rid=run['id']
        if run['legal_hold']:report.append({'run':rid,'action':'held'});continue
        if not apply:report.append({'run':rid,'action':'would_archive','purge':purge});continue
        target=directory/(rid+'.recon');partial=directory/(rid+'.partial')
        if target.exists():raise ValueError('Archive already exists; verify it before retrying retention.')
        try:
            with tempfile.TemporaryDirectory(dir=app.data) as tmp:
                plain=Path(tmp)/'evidence.zip'
                with zipfile.ZipFile(plain,'x',zipfile.ZIP_DEFLATED) as archive,app.db() as db:
                    archive.writestr('run.json',json.dumps(run,default=str))
                    for side in ('source','destination'):archive.write(app.uploads/run[side+'_file'],side+'.csv')
                    for name,sql in [('issues','SELECT * FROM issues WHERE run_id=?'),('reviews','SELECT reviews.* FROM reviews JOIN issues ON reviews.issue_id=issues.id WHERE issues.run_id=?')]:
                        with archive.open(name+'.jsonl','w') as stream:
                            for row in db.execute(sql,(rid,)):stream.write((json.dumps(dict(row),default=str)+'\n').encode())
                    head=db.execute('SELECT hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
                    archive.writestr('audit-head.txt',head[0] if head else '')
                encrypt_file(plain,partial);decrypt_file(partial,os.devnull);os.replace(partial,target)
            remote=locked_s3_copy(target,max(days,1)) if os.environ.get('RECON_ARCHIVE_S3_BUCKET') else None
            with app.db() as db:
                db.execute('BEGIN IMMEDIATE')
                held=db.execute('SELECT legal_hold FROM retention WHERE run_id=?',(rid,)).fetchone()
                if held and held[0]:raise ValueError('Run was placed on legal hold during archival; originals retained.')
                with target.open('rb') as stream:archive_hash=hashlib.file_digest(stream,'sha256').hexdigest()
                metadata=json.dumps({'path':str(target),'sha256':archive_hash,'s3':remote})
                db.execute('INSERT INTO retention(run_id,archived) VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET archived=excluded.archived',(rid,metadata))
                if purge:
                    if not remote and os.environ.get('RECON_REQUIRE_OBJECT_LOCK','0' if app.demo else '1')=='1':raise ValueError('A verified object-lock copy is required before purge.')
                    db.execute("UPDATE issues SET key='',source='null',destination='null',detail='Evidence moved to governed archive' WHERE run_id=?",(rid,))
                    db.execute("UPDATE reviews SET note='Review evidence moved to governed archive' WHERE issue_id IN (SELECT id FROM issues WHERE run_id=?)",(rid,))
                    db.execute("UPDATE runs SET state='archived',phase='Evidence archived' WHERE id=?",(rid,))
                app.audit(db,'retention','run.archived',rid,{'sha256':json.loads(metadata)['sha256'],'purged':purge,'object_version':remote})
            if purge:
                for side in ('source','destination'):(app.uploads/run[side+'_file']).unlink(missing_ok=True)
            report.append({'run':rid,'action':'archived','purged':purge})
        finally:partial.unlink(missing_ok=True)
    return report

def archive_ledger(app,archive_dir,days=365):
    if days<1:raise ValueError('Archive retention must be positive.')
    import uuid
    directory=Path(archive_dir).resolve()
    if directory==app.data or app.data in directory.parents:raise ValueError('Use an independent archive directory.')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700);archive_key()
    target=directory/('audit-'+uuid.uuid4().hex+'.recon');partial=target.with_suffix('.partial');head='0'*64;count=0
    try:
        with tempfile.TemporaryDirectory(dir=app.data) as folder:
            plain=Path(folder)/'audit.zip'
            with zipfile.ZipFile(plain,'x',zipfile.ZIP_DEFLATED) as archive,app.db() as db:
                with archive.open('audit.jsonl','w') as stream:
                    for row in db.execute('SELECT * FROM audit ORDER BY id'):
                        stream.write((json.dumps(dict(row))+'\n').encode());head=row['hash'];count+=1
                archive.writestr('checkpoint.json',json.dumps({'head':head,'events':count,'created':datetime.now(timezone.utc).isoformat()}))
            encrypt_file(plain,partial);decrypt_file(partial,os.devnull);os.replace(partial,target)
        remote=locked_s3_copy(target,days) if os.environ.get('RECON_ARCHIVE_S3_BUCKET') else None
        with app.db() as db:app.audit(db,'operator','audit.archived',target.name,{'head':head,'events':count,'object_version':remote})
        return {'archive':str(target),'head':head,'events':count,'object_version':remote}
    finally:partial.unlink(missing_ok=True)
