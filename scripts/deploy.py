"""Production configuration and deployment preflight. Does not publish implicitly."""
import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def portable_image_fingerprint(image):
    keys=('User','Cmd','Entrypoint','Env','WorkingDir','ExposedPorts','Healthcheck')
    payload={'platform':image['Os']+'/'+image['Architecture'],'layers':image['RootFS']['Layers'],
             'config':{key:image['Config'].get(key) for key in keys}}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def configure(domain,email,password):
    if not re.fullmatch(r'(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?',domain) or '.' not in domain:raise ValueError('Provide a DNS domain without a URL scheme or path.')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):raise ValueError('Provide an administrator email address.')
    if not 12<=len(password)<=200 or any(c in password for c in '\r\n\x00'):raise ValueError('Use a 12–200 character password without control characters.')
    # Compose single quotes keep dollar signs literal; escape quote/backslash per dotenv syntax.
    quoted=lambda value:"'"+value.replace("'","\\'")+"'"
    values={'RECON_DOMAIN':domain,'RECON_ADMIN_EMAIL':email,'RECON_ADMIN_PASSWORD':password,
            'RECON_SECRET':secrets.token_urlsafe(48),'RECON_MFA_KEY':secrets.token_urlsafe(48),'RECON_REQUIRE_MFA':'1',
            'RECON_ARCHIVE_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}
    return '\n'.join(key+'='+quoted(value) for key,value in values.items())+'\n'

def main():
    parser=argparse.ArgumentParser(description='Prepare and validate a private production deployment.')
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('configure');init.add_argument('--domain');init.add_argument('--email')
    commands.add_parser('check');commands.add_parser('build');start=commands.add_parser('start');start.add_argument('--prebuilt',action='store_true');commands.add_parser('status')
    args=parser.parse_args()
    if args.command=='configure':
        target=ROOT/'.env'
        if target.exists():raise SystemExit('.env already exists. Keep existing encryption keys; edit your protected configuration intentionally.')
        domain=args.domain or input('Public domain (for example reconcile.yourcompany.com): ').strip()
        email=args.email or input('Administrator email: ').strip()
        password=getpass.getpass('Initial administrator password: ')
        if password!=getpass.getpass('Confirm password: '):raise SystemExit('Passwords differ.')
        contents=configure(domain,email,password)
        descriptor=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:stream.write(contents)
        print('Protected .env created. Escrow the encryption keys separately. No credentials were printed.');return
    if not (ROOT/'.env').is_file():raise SystemExit('Run configure first, or install your reviewed .env from your secret manager.')
    def run(arguments):return subprocess.run(arguments,cwd=ROOT,check=True)
    run(['docker','compose','config','--quiet'])
    resolved=json.loads(subprocess.check_output(['docker','compose','config','--format','json'],cwd=ROOT,text=True,encoding='utf-8'))
    if args.command=='check':
        run(['docker','info','--format','{{.ServerVersion}}'])
        print('Configuration and Docker engine passed. DNS, HTTPS and external identities still require deployment smoke tests.')
    elif args.command=='build':run(['docker','compose','build','--pull'])
    elif args.command=='start':
        if args.prebuilt:
            manifest=ROOT/'deploy'/'images.json'
            if not manifest.is_file():raise SystemExit('The prebuilt image manifest is missing. Load the supplied image bundle or build from source.')
            image=resolved['services']['app']['image'];images=json.loads(manifest.read_text())['images']
            if image not in images:raise SystemExit('This prebuilt image tag is not part of the supplied release.')
            expected=images[image]['portable_fingerprint']
            actual=portable_image_fingerprint(json.loads(subprocess.check_output(['docker','image','inspect',image],text=True,encoding='utf-8'))[0])
            if actual!=expected:raise SystemExit('The local application image does not match this release. Load the supplied image bundle again.')
        run(['docker','compose','up','--no-build' if args.prebuilt else '--build','-d','--wait','--wait-timeout','180'])
        public_url=resolved['services']['app']['environment']['RECON_PUBLIC_URL'].rstrip('/')
        print('Containers started. Waiting for verified public HTTPS...')
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(public_url+'/healthz',timeout=5) as response:
                    if json.load(response).get('status')=='ok':
                        print('Deployment verified: '+public_url);return
            except Exception:time.sleep(2)
        raise SystemExit('Containers are running, but public HTTPS is not ready. Check DNS, ports 80/443 and certificate issuance in the edge logs.')
    elif args.command=='status':run(['docker','compose','ps'])

if __name__=='__main__':main()
