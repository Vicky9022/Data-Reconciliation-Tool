"""Exercise the real production image using disposable, named test resources."""
import json
import os
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
from reconcile.enterprise import totp

def cli(*args):return subprocess.run(['docker',*args],check=True,capture_output=True,text=True).stdout.strip()

def smoke(image):
    suffix=uuid.uuid4().hex[:12];name='reconcile-release-smoke-'+suffix;volume=name+'-data'
    password='Fixture!Password$2026';cookie='';csrf=''
    checks=[]
    try:
        with tempfile.TemporaryDirectory(prefix='reconcile-release-') as folder:
            env=Path(folder)/'fixture.env'
            env.write_text('\n'.join(['RECON_ADMIN_EMAIL=fixture@example.invalid','RECON_ADMIN_PASSWORD='+password,
                'RECON_SECRET='+secrets.token_urlsafe(48),'RECON_MFA_KEY='+secrets.token_urlsafe(48),'RECON_REQUIRE_MFA=1','RECON_DATASET_ACL=1']))
            os.chmod(env,0o600)
            cli('run','-d','--name',name,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--tmpfs','/tmp:size=64m,mode=1777','--mount',f'type=volume,source={volume},target=/data',
                '--env-file',str(env),'-p','127.0.0.1::8000',image)
            ports=json.loads(cli('inspect','--format','{{json .NetworkSettings.Ports}}',name));port=ports['8000/tcp'][0]['HostPort'];base='http://127.0.0.1:'+port
            def request(path,payload=None):
                headers={'Cookie':cookie,'X-CSRF-Token':csrf};data=None
                if payload is not None:headers['Content-Type']='application/json';data=json.dumps(payload).encode()
                with urllib.request.urlopen(urllib.request.Request(base+path,data,headers),timeout=20) as response:
                    body=response.read();kind=response.headers.get('Content-Type','')
                    return json.loads(body) if kind=='application/json' else body,response.headers
            def ready():
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    try:request('/healthz');return
                    except Exception:time.sleep(.5)
                raise RuntimeError('Production container did not become healthy.')
            ready();checks.append('Production Gunicorn process starts as non-root with a read-only root filesystem')
            if request('/api/config')[0]['demo']:raise AssertionError('Demo enabled in production image')
            try:request('/api/demo-login',{});raise AssertionError('Demo bypass accepted')
            except urllib.error.HTTPError as error:
                if error.code!=404:raise
            user,headers=request('/api/login',{'email':'fixture@example.invalid','password':password})
            if not all(flag in headers['Set-Cookie'] for flag in ('HttpOnly','Secure','SameSite=Strict')):raise AssertionError('Missing production cookie protections')
            cookie=headers['Set-Cookie'].split(';')[0];csrf=user['csrf']
            try:request('/api/runs');raise AssertionError('MFA enrollment not enforced')
            except urllib.error.HTTPError as error:
                if error.code!=403:raise
            setup,_=request('/api/security/enroll',{'password':password});confirmed,_=request('/api/security/confirm',{'code':totp(setup['secret'])})
            checks.append('Native login, secure cookies, mandatory MFA enrollment and recovery codes')
            run,_=request('/api/runs',{'dataset':'Disposable release fixture','source_csv':'id,value\na,10\n','destination_csv':'id,value\na,11\n','rules':{'version':'smoke-1','keys':['id'],'fields':{'value':{'type':'numeric'}}}})
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                result,_=request('/api/runs/'+run['id'])
                if result['state'] in ('completed','failed'):break
                time.sleep(.2)
            if result['state']!='completed' or result['summary']['exception_count']!=1:raise AssertionError('Reconciliation fixture did not match the oracle')
            csv,_=request('/api/runs/'+run['id']+'/export');xlsx,_=request('/api/runs/'+run['id']+'/export?format=xlsx')
            if b'value_mismatch' not in csv or not xlsx.startswith(b'PK'):raise AssertionError('Export validation failed')
            checks.append('Persisted reconciliation, exception evidence, CSV and Excel exports')
            request('/api/logout',{});cli('restart',name)
            ports=json.loads(cli('inspect','--format','{{json .NetworkSettings.Ports}}',name));base='http://127.0.0.1:'+ports['8000/tcp'][0]['HostPort']
            ready()
            user,headers=request('/api/login',{'email':'fixture@example.invalid','password':password,'code':confirmed['recovery_codes'][0]})
            cookie=headers['Set-Cookie'].split(';')[0];csrf=user['csrf']
            if request('/api/runs/'+run['id'])[0]['state']!='completed':raise AssertionError('Run disappeared after restart')
            checks.append('Volume persistence, encrypted authenticator storage and recovery-code login survive container restart')
        return {'image':image,'passed':True,'checks':checks,'scope':'Disposable localhost production-image smoke test; public DNS/TLS and organization OIDC are not exercised.'}
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True)
        subprocess.run(['docker','volume','rm',volume],capture_output=True)

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--image',default='reconcile-staging-app');args=parser.parse_args()
    result=smoke(args.image);reports=ROOT/'reports';reports.mkdir(exist_ok=True);(reports/'container_smoke_results.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
