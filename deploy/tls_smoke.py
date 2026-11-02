"""Verify an actual HTTPS reverse proxy with a test CA trusted only by this test."""
import json
import os
import secrets
import ssl
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

def cli(*args):return subprocess.run(['docker',*args],capture_output=True,text=True,check=True).stdout.strip()

def run():
    suffix=uuid.uuid4().hex[:12];network='reconcile-tls-'+suffix;app=network+'-app';edge=network+'-edge'
    try:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);env=root/'fixture.env';config=root/'Caddyfile';certificate=root/'ca.crt'
            env.write_text('RECON_ADMIN_EMAIL=fixture@example.invalid\nRECON_ADMIN_PASSWORD=fixture-long-password\nRECON_SECRET='+secrets.token_urlsafe(48)+'\nRECON_MFA_KEY='+secrets.token_urlsafe(48)+'\n')
            os.chmod(env,0o600)
            config.write_text('{\n auto_https disable_redirects\n}\nhttps://localhost {\n tls internal\n reverse_proxy app:8000\n}\n')
            cli('network','create',network)
            cli('run','-d','--name',app,'--network',network,'--network-alias','app','--read-only','--cap-drop','ALL',
                '--tmpfs','/data:rw,uid=10001,gid=10001,mode=0700,size=128m','--tmpfs','/tmp:size=64m','--env-file',str(env),'reconcile-platform:2.1')
            cli('run','-d','--name',edge,'--network',network,'--tmpfs','/data','--tmpfs','/config',
                '--mount',f'type=bind,source={config},target=/etc/caddy/Caddyfile,readonly','-p','127.0.0.1::443','caddy:2-alpine')
            deadline=time.monotonic()+60;last_error=''
            while time.monotonic()<deadline:
                try:
                    certificate.write_text(cli('exec',edge,'cat','/data/caddy/pki/authorities/local/root.crt'),encoding='ascii')
                    ports=json.loads(cli('inspect','--format','{{json .NetworkSettings.Ports}}',edge));port=ports['443/tcp'][0]['HostPort']
                    context=ssl.create_default_context(cafile=str(certificate))
                    with urllib.request.urlopen('https://localhost:'+port+'/healthz',context=context,timeout=5) as response:
                        body=json.loads(response.read())
                        if body['status']=='ok' and response.headers.get('Strict-Transport-Security'):break
                except Exception as error:last_error=repr(error);time.sleep(.5)
            else:
                logs={name:subprocess.run(['docker','logs',name],capture_output=True,text=True).stderr[-6000:] for name in (app,edge)}
                (Path(__file__).resolve().parents[1]/'tls_smoke_failure.json').write_text(json.dumps({'last_error':last_error,'logs':logs},indent=2))
                raise RuntimeError('Verified TLS reverse-proxy smoke test failed: '+last_error)
            return {'passed':True,'checks':['Certificate chain and localhost identity verified with an isolated test CA','HTTPS reverse proxy reaches the production application','Production HSTS response header is present'],'scope':'Local private CA; no browser warning bypass or machine trust-store modification. Public-domain ACME issuance requires the deployment domain.'}
    finally:
        for name in (edge,app):subprocess.run(['docker','rm','-f','-v',name],capture_output=True)
        subprocess.run(['docker','network','rm',network],capture_output=True)

if __name__=='__main__':
    result=run();(Path(__file__).resolve().parents[1]/'tls_smoke_results.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
