import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Application, password_hash

class APITests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.app=Application(self.folder.name,demo=True,start_worker=False)
        self.cookie='';self.csrf=''
    def tearDown(self):self.app.close();self.folder.cleanup()
    def call(self,path,method='GET',payload=None,headers=None):
        body=json.dumps(payload or {}).encode();route,_,query=path.partition('?')
        env={'PATH_INFO':route,'QUERY_STRING':query,'REQUEST_METHOD':method,'wsgi.input':io.BytesIO(body),'CONTENT_LENGTH':str(len(body)),'CONTENT_TYPE':'application/json','HTTP_HOST':'127.0.0.1:8765','REMOTE_ADDR':'127.0.0.1','HTTP_COOKIE':self.cookie,'HTTP_X_CSRF_TOKEN':self.csrf}
        env.update(headers or {});result={}
        def start(status,response_headers):result.update(status=int(status.split()[0]),headers=dict(response_headers))
        raw=b''.join(self.app(env,start));result['raw']=raw
        result['body']=json.loads(raw) if result['headers']['Content-Type']=='application/json' else raw
        return result
    def login(self):
        response=self.call('/api/demo-login','POST');self.assertEqual(200,response['status'])
        self.cookie=response['headers']['Set-Cookie'].split(';')[0];self.csrf=response['body']['csrf']
    def queued(self,source='id,v\na,1\n',destination='id,v\na,2\n',headers=None):
        response=self.call('/api/runs','POST',{'dataset':'Test','rules':{'keys':['id'],'fields':{}},'source_csv':source,'destination_csv':destination},headers)
        self.assertEqual(202,response['status'],response['body']);return response['body']['id']
    def execute(self,rid):
        with self.app.db() as db:row=dict(db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone())
        self.app.execute_run(row)

    def test_auth_csrf_origin_and_headers(self):
        self.assertEqual(401,self.call('/api/runs')['status']);self.login()
        self.assertEqual(403,self.call('/api/runs','POST',headers={'HTTP_X_CSRF_TOKEN':''})['status'])
        self.assertEqual(403,self.call('/api/runs','POST',headers={'HTTP_ORIGIN':'https://evil.test'})['status'])
        self.assertIn('Content-Security-Policy',self.call('/')['headers'])
        self.assertEqual(404,self.call('/../server.py')['status'])

    def test_run_queue_evidence_filter_and_reproducibility(self):
        self.login();rid=self.queued();self.execute(rid)
        result=self.call('/api/runs/'+rid)['body'];self.assertEqual('completed',result['state']);self.assertEqual(1,result['summary']['exception_count'])
        issue=self.call('/api/runs/'+rid+'/issues')['body']['issues'][0];self.assertEqual('1',issue['source']);self.assertEqual('2',issue['destination'])
        self.assertEqual(0,self.call('/api/runs/'+rid+'/issues?severity=critical')['body']['total'])
        self.assertEqual(0,self.call('/api/runs/'+rid+'/issues?q=unknown')['body']['total'])
        self.assertEqual(0,self.call('/api/runs/'+rid+"/issues?category=%27%20OR%201%3D1%20--")['body']['total'])
        self.assertEqual(1,self.call('/api/runs/'+rid+'/issues?q=a')['body']['total'])
        self.assertEqual(200,self.call('/api/runs/'+rid+'/snapshot?side=source')['status'])

    def test_idempotency(self):
        self.login();headers={'HTTP_IDEMPOTENCY_KEY':'test-idempotency-123'}
        self.assertEqual(self.queued(headers=headers),self.queued(headers=headers))
        self.assertEqual(1,len(self.call('/api/runs')['body']['runs']))

    def test_failed_job_has_no_partial_results(self):
        self.login();rid=self.queued('id,v\na,1,2\n');self.execute(rid)
        self.assertEqual('failed',self.call('/api/runs/'+rid)['body']['state'])
        self.assertEqual(409,self.call('/api/runs/'+rid+'/issues')['status'])

    def test_review_and_append_only_audit_chain(self):
        self.login();rid=self.queued();self.execute(rid);iid=self.call('/api/runs/'+rid+'/issues')['body']['issues'][0]['id']
        self.assertEqual(400,self.call(f'/api/issues/{iid}/review','POST',{'decision':'accepted','note':'short'})['status'])
        self.assertEqual(200,self.call(f'/api/issues/{iid}/review','POST',{'decision':'accepted','note':'Approved under documented policy T-123.'})['status'])
        issue=self.call('/api/runs/'+rid+'/issues')['body']['issues'][0];self.assertEqual('accepted',issue['status']);self.assertEqual(1,len(issue['reviews']))
        from engine import canonical
        with self.app.db() as db:
            previous='0'*64
            for row in db.execute('SELECT * FROM audit ORDER BY id'):
                self.assertEqual(previous,row['previous'])
                digest=hashlib.sha256(canonical([row['created'],row['actor'],row['action'],row['resource'],row['metadata'],row['previous']]).encode()).hexdigest()
                self.assertEqual(digest,row['hash']);previous=digest
            with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM audit')

    def test_viewer_restrictions_and_revoke_sessions(self):
        self.login()
        result=self.call('/api/users','POST',{'email':'viewer@example.com','password':'long-password-123','role':'viewer'});uid=result['body']['id']
        admin_cookie,admin_csrf=self.cookie,self.csrf
        result=self.call('/api/login','POST',{'email':'viewer@example.com','password':'long-password-123'})
        viewer_cookie=result['headers']['Set-Cookie'].split(';')[0];self.cookie=viewer_cookie;self.csrf=result['body']['csrf']
        self.assertEqual(200,self.call('/api/runs')['status']);self.assertEqual(403,self.call('/api/runs','POST',{})['status']);self.assertEqual(403,self.call('/api/users')['status']);self.assertEqual(403,self.call('/api/audit')['status'])
        self.cookie,self.csrf=admin_cookie,admin_csrf
        self.assertEqual(200,self.call('/api/users/'+uid,'PATCH',{'active':0})['status'])
        self.cookie=viewer_cookie;self.assertEqual(401,self.call('/api/me')['status'])

    def test_analyst_cannot_accept(self):
        self.login();rid=self.queued();self.execute(rid);iid=self.call('/api/runs/'+rid+'/issues')['body']['issues'][0]['id']
        self.call('/api/users','POST',{'email':'analyst@example.com','password':'long-password-123','role':'analyst'})
        result=self.call('/api/login','POST',{'email':'analyst@example.com','password':'long-password-123'});self.cookie=result['headers']['Set-Cookie'].split(';')[0];self.csrf=result['body']['csrf']
        self.assertEqual(403,self.call(f'/api/issues/{iid}/review','POST',{'decision':'accepted','note':'Trying to approve without admin.'})['status'])
        self.assertEqual(200,self.call(f'/api/issues/{iid}/review','POST',{'decision':'investigating','note':'Investigating source ledger reference.'})['status'])

    def test_login_throttle_and_logout(self):
        for _ in range(10):self.assertEqual(401,self.call('/api/login','POST',{'email':'wrong@example.com','password':'bad'})['status'])
        self.assertEqual(429,self.call('/api/login','POST',{'email':'wrong@example.com','password':'bad'})['status'])
        with self.app.db() as db:db.execute('DELETE FROM login_attempts')
        self.login();self.assertEqual(200,self.call('/api/logout','POST')['status']);self.assertEqual(401,self.call('/api/me')['status'])

    def test_exports_formula_safe(self):
        self.login();rid=self.queued('id,v\na,=1+1\n','id,v\na,2\n');self.execute(rid)
        exported=self.call('/api/runs/'+rid+'/export');self.assertEqual(200,exported['status']);self.assertIn(b'value_mismatch',exported['raw'])
        exported=self.call('/api/runs/'+rid+'/export?format=xlsx');self.assertEqual(200,exported['status']);self.assertTrue(exported['raw'].startswith(b'PK'))
        from openpyxl import load_workbook
        workbook=load_workbook(io.BytesIO(exported['raw']));self.assertTrue(all(c.data_type!='f' for row in workbook.active for c in row))

    def test_production_requires_credentials(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'RECON_SECRET':''}):
            with self.assertRaises(RuntimeError):Application(folder,demo=False,start_worker=False)

    def test_queue_limit(self):
        self.login()
        for _ in range(10):self.queued()
        self.assertEqual(429,self.call('/api/runs','POST',{'dataset':'Too many','demo':True})['status'])

    def test_production_cookies_and_no_demo_backdoor(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'RECON_SECRET':'x'*48,'RECON_ADMIN_EMAIL':'admin@example.com','RECON_ADMIN_PASSWORD':'production-password-123','RECON_PUBLIC_URL':'https://reconcile.example.com'}):
            old=self.app;self.app=Application(folder,demo=False,start_worker=False)
            try:
                self.assertEqual(404,self.call('/api/demo-login','POST')['status'])
                response=self.call('/api/login','POST',{'email':'admin@example.com','password':'production-password-123'},headers={'HTTP_ORIGIN':'https://reconcile.example.com'})
                self.assertEqual(200,response['status']);self.assertIn('; Secure',response['headers']['Set-Cookie']);self.assertIn('HttpOnly',response['headers']['Set-Cookie']);self.assertIn('Strict-Transport-Security',response['headers'])
            finally:self.app.close();self.app=old

    def test_restart_recovers_unfinished_job(self):
        import time
        self.login();rid=self.queued()
        with self.app.db() as db:db.execute("UPDATE runs SET state='running' WHERE id=?",(rid,))
        self.app.close();self.app=Application(self.folder.name,demo=True,start_worker=True)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            with self.app.db() as db:state=db.execute('SELECT state FROM runs WHERE id=?',(rid,)).fetchone()[0]
            if state=='completed':break
            time.sleep(.02)
        self.assertEqual('completed',state)

    def test_coordinator_process_lock(self):
        self.app.close();self.app=Application(self.folder.name,demo=True,start_worker=True)
        with self.assertRaises(RuntimeError):Application(self.folder.name,demo=True,start_worker=True)

    def test_request_validation_and_queue_permissions(self):
        self.login()
        self.assertEqual(415,self.call('/api/runs','POST',headers={'CONTENT_TYPE':'text/plain'})['status'])
        self.assertEqual(413,self.call('/api/runs','POST',headers={'CONTENT_LENGTH':str(40*1024*1024)})['status'])
        self.assertEqual(413,self.call('/api/login','POST',headers={'CONTENT_LENGTH':'70000'})['status'])
        self.assertEqual(400,self.call('/api/runs','POST',{'dataset':'Unknown connector','connection':'unconfigured'})['status'])
        self.assertEqual(400,self.call('/api/runs','POST',{'dataset':'Unsafe rule','rules':{'keys':['id'],'fields':{'v':{'type':'numeric','tolerance':'NaN'}}}})['status'])

    def test_unknown_column_rule_fails_without_results(self):
        self.login();response=self.call('/api/runs','POST',{'dataset':'Typo','source_csv':'id,v\na,1\n','destination_csv':'id,v\na,1\n','rules':{'keys':['id'],'fields':{'typo':{'type':'text'}}}})
        rid=response['body']['id'];self.execute(rid)
        self.assertEqual('failed',self.call('/api/runs/'+rid)['body']['state'])

    def test_secret_rotation_invalidates_sessions(self):
        self.login();self.app.secret='rotated-secret-value';self.assertEqual(401,self.call('/api/me')['status'])

if __name__=='__main__':unittest.main()
