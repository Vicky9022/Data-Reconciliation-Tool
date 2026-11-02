"""Offline operator utilities. Run only as an authorized data-volume operator."""
import argparse
import getpass
import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from server import Application, password_hash
from engine import canonical

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',default=os.environ.get('RECON_DATA_DIR','runtime'))
    commands=parser.add_subparsers(dest='command',required=True)
    reset=commands.add_parser('reset-password');reset.add_argument('email')
    backup=commands.add_parser('backup');backup.add_argument('output')
    verify=commands.add_parser('verify-audit');verify.add_argument('--trusted-head')
    ledger=commands.add_parser('archive-audit');ledger.add_argument('--archive-dir',required=True);ledger.add_argument('--days',type=int,default=365)
    retention=commands.add_parser('retention');retention.add_argument('--archive-dir',required=True);retention.add_argument('--days',type=int,default=90);retention.add_argument('--apply',action='store_true');retention.add_argument('--purge',action='store_true')
    decrypt=commands.add_parser('decrypt-archive');decrypt.add_argument('archive');decrypt.add_argument('output')
    args=parser.parse_args()
    if args.command=='decrypt-archive':
        from governance import decrypt_file
        output=Path(args.output).resolve()
        if output.exists():raise SystemExit('Output already exists.')
        temporary=output.with_suffix(output.suffix+'.partial')
        if temporary.exists():raise SystemExit('Temporary target already exists.')
        try:decrypt_file(args.archive,temporary);os.replace(temporary,output)
        finally:temporary.unlink(missing_ok=True)
        print('Verified archive decrypted. Protect the plaintext evidence ZIP.');return
    if not (Path(args.data_dir)/'reconcile.sqlite').is_file():raise SystemExit('Existing application database not found. Check --data-dir; no new store was created.')
    app=Application(args.data_dir,demo=False,start_worker=False)
    try:
        if args.command=='archive-audit':
            from governance import archive_ledger
            print(json.dumps(archive_ledger(app,args.archive_dir,args.days),indent=2))
        elif args.command=='retention':
            from governance import retain
            print(json.dumps(retain(app,args.archive_dir,args.days,args.apply,args.purge),indent=2))
        elif args.command=='reset-password':
            password=getpass.getpass('New password (12–200 characters): ')
            if not 12<=len(password)<=200 or password!=getpass.getpass('Confirm new password: '): raise SystemExit('Password requirements or confirmation failed.')
            with app.db() as db:
                user=db.execute('SELECT id FROM users WHERE email=?',(args.email.lower(),)).fetchone()
                if not user: raise SystemExit('Member not found.')
                db.execute('UPDATE users SET password=? WHERE id=?',(password_hash(password),user['id']))
                db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
                app.audit(db,'operator','user.password_reset',user['id'])
            print('Password changed; all sessions for this member were revoked.')
        elif args.command=='verify-audit':
            previous='0'*64;count=0;trusted_seen=False
            with app.db() as db:
                for row in db.execute('SELECT * FROM audit ORDER BY id'):
                    expected=hashlib.sha256(canonical([row['created'],row['actor'],row['action'],row['resource'],row['metadata'],row['previous']]).encode()).hexdigest()
                    if row['previous']!=previous or row['hash']!=expected: raise SystemExit(f'Audit integrity check failed at event {row["id"]}.')
                    previous=row['hash'];count+=1
                    if previous==args.trusted_head:trusted_seen=True
            if args.trusted_head and not trusted_seen:raise SystemExit('Trusted independent audit checkpoint is absent from this chain.')
            print(f'Verified {count} audit events. Chain head: {previous}')
        elif args.command=='backup':
            output=Path(args.output).resolve()
            if output.exists(): raise SystemExit('Backup target already exists; choose a new filename.')
            output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with tempfile.TemporaryDirectory() as folder:
                snap=Path(folder)/'reconcile.sqlite'
                with closing(sqlite3.connect(app.db_path)) as live,closing(sqlite3.connect(snap)) as copy: live.backup(copy)
                with closing(sqlite3.connect(snap)) as db:
                    files=[name for row in db.execute("SELECT source_file,destination_file FROM runs WHERE state!='archived'") for name in row]
                partial=output.with_suffix(output.suffix+'.partial')
                archive=zipfile.ZipFile(partial,'x',zipfile.ZIP_DEFLATED)
                os.chmod(partial,0o600)
                try:
                    with archive:
                        archive.write(snap,'reconcile.sqlite')
                        for name in files:archive.write(app.uploads/name,'snapshots/'+name)
                    with zipfile.ZipFile(partial) as check:
                        if check.testzip() is not None:raise ValueError('Backup integrity verification failed.')
                    os.link(partial,output)
                finally:partial.unlink(missing_ok=True)
            print(f'Backup saved to {output}. Encrypt and copy it to an independent backup system.')
    finally:app.close()

if __name__=='__main__':main()
