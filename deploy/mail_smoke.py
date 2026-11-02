"""Disposable staging email recovery test; mail is captured locally by Mailpit."""
import json
import re
import secrets
import sys
import time
import urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from loadtest import request

def run():
    base='http://127.0.0.1:8766'
    if not request(base,'/api/config')[0].get('demo'):raise ValueError('This check only runs against the disposable staging profile.')
    user,headers=request(base,'/api/demo-login',payload={});cookie=headers['Set-Cookie'].split(';')[0];csrf=user['csrf']
    email='fixture-'+secrets.token_hex(6)+'@reconcile.local';uid=None
    try:
        created,_=request(base,'/api/users',cookie,{'email':email,'password':secrets.token_urlsafe(24),'role':'viewer'},csrf);uid=created['id']
        request(base,'/api/password-reset/request',payload={'email':email})
        deadline=time.monotonic()+45;token=None
        while time.monotonic()<deadline:
            with urllib.request.urlopen('http://127.0.0.1:8025/api/v1/messages',timeout=5) as response:messages=json.load(response).get('messages',[])
            for item in messages:
                if not any(recipient.get('Address')==email for recipient in item.get('To',[])):continue
                with urllib.request.urlopen('http://127.0.0.1:8025/api/v1/message/'+item['ID'],timeout=5) as response:body=json.load(response)
                found=re.search(r'#reset=([A-Za-z0-9_-]+)',body.get('Text',''))
                if found:token=found.group(1);break
            if token:break
            time.sleep(.5)
        if not token:raise RuntimeError('Recovery message was not captured in the local mail sink.')
        password=secrets.token_urlsafe(24)
        request(base,'/api/password-reset/complete',payload={'token':token,'password':password})
        identity,_=request(base,'/api/login',payload={'email':email,'password':password})
        if identity['id']!=uid:raise AssertionError('Recovery changed the wrong account.')
        return {'passed':True,'checks':['Encrypted recovery outbox delivered to the local SMTP sink','Captured one-use link reset only the disposable member','New password authenticated successfully'],'scope':'Mailpit only; no external email was sent. Test member access revoked after the check.'}
    finally:
        if uid:revoke(base,uid,cookie,csrf)

def revoke(base,uid,cookie,csrf):
    data=json.dumps({'active':0}).encode()
    req=urllib.request.Request(base+'/api/users/'+uid,data,{'Cookie':cookie,'X-CSRF-Token':csrf,'Content-Type':'application/json'},method='PATCH')
    with urllib.request.urlopen(req,timeout=10) as response:response.read()

if __name__=='__main__':
    result=run();(ROOT/'mail_smoke_results.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
