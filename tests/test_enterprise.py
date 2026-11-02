import base64
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import types
import sys
from pathlib import Path
from unittest.mock import patch
from tests import test_api as fixtures
from reconcile.enterprise import totp,valid_step
from reconcile.server import password_hash

class EnterpriseTests(unittest.TestCase):
    setUp=fixtures.APITests.setUp
    tearDown=fixtures.APITests.tearDown
    call=fixtures.APITests.call
    login=fixtures.APITests.login
    queued=fixtures.APITests.queued
    execute=fixtures.APITests.execute

    def test_totp_rfc_vector_replay_and_window(self):
        secret=base64.b32encode(b'12345678901234567890').decode()
        self.assertEqual('287082',totp(secret,59));self.assertEqual(1,valid_step(secret,'287082',timestamp=59))
        self.assertIsNone(valid_step(secret,'287082',last_step=1,timestamp=59))
        self.assertIsNone(valid_step(secret,'nope',timestamp=59))
        self.assertIsNone(valid_step(secret,'287082',timestamp=180))

    def test_dataset_acl_applies_to_list_detail_evidence_and_reviews(self):
        self.login();self.app.enforce_acl=True;rid=self.queued();self.execute(rid)
        iid=self.call('/api/runs/'+rid+'/issues')['body']['issues'][0]['id']
        user=self.call('/api/users','POST',{'email':'limited@example.com','password':'long-test-password','role':'analyst'})['body']['id']
        admin_cookie,admin_csrf=self.cookie,self.csrf
        result=self.call('/api/login','POST',{'email':'limited@example.com','password':'long-test-password'})
        self.cookie=result['headers']['Set-Cookie'].split(';')[0];self.csrf=result['body']['csrf'];limited_cookie,limited_csrf=self.cookie,self.csrf
        self.assertEqual([],self.call('/api/runs')['body']['runs'])
        for suffix in ('','/issues','/export'):
            self.assertEqual(403,self.call('/api/runs/'+rid+suffix)['status'])
        self.assertEqual(403,self.call(f'/api/issues/{iid}/review','POST',{'decision':'investigating','note':'A sufficient review rationale.'})['status'])
        self.cookie,self.csrf=admin_cookie,admin_csrf
        self.assertEqual(200,self.call('/api/access','POST',{'dataset':'Test','user_id':user,'permission':'read'})['status'])
        self.cookie,self.csrf=limited_cookie,limited_csrf
        self.assertEqual(1,len(self.call('/api/runs')['body']['runs']))
        self.assertEqual(200,self.call('/api/runs/'+rid+'/issues')['status'])
        self.assertEqual(403,self.call(f'/api/issues/{iid}/review','POST',{'decision':'investigating','note':'Still not allowed to write.'})['status'])

    def test_mfa_enrollment_recovery_codes_and_replay(self):
        self.login()
        with self.app.db() as db:db.execute('UPDATE users SET password=? WHERE id=?',(password_hash('test-admin-password'),'admin'))
        setup=self.call('/api/security/enroll','POST',{'password':'test-admin-password'})
        self.assertEqual(200,setup['status'],setup['body']);secret=setup['body']['secret']
        confirmed=self.call('/api/security/confirm','POST',{'code':totp(secret)})
        self.assertEqual(200,confirmed['status']);codes=confirmed['body']['recovery_codes'];self.assertEqual(10,len(codes))
        with self.app.db() as db:
            encrypted=db.execute('SELECT secret FROM factors').fetchone()[0];self.assertNotIn(secret,encrypted)
            self.assertFalse(self.app.factor_valid(db,'admin',totp(secret)))
            self.assertTrue(self.app.factor_valid(db,'admin',codes[0]));self.assertFalse(self.app.factor_valid(db,'admin',codes[0]))
        failed=self.call('/api/login','POST',{'email':'demo@reconcile.local','password':'test-admin-password'})
        self.assertEqual(401,failed['status'])
        success=self.call('/api/login','POST',{'email':'demo@reconcile.local','password':'test-admin-password','code':codes[1]})
        self.assertEqual(200,success['status'])
        replaced=self.call('/api/security/disable','POST',{'password':'test-admin-password','code':codes[2]})
        self.assertEqual(200,replaced['status']);self.assertEqual(401,self.call('/api/me')['status'])
        with self.app.db() as db:self.assertEqual(0,db.execute('SELECT COUNT(*) FROM factors').fetchone()[0])

    def test_required_mfa_blocks_data_until_enrollment(self):
        self.login()
        with patch.dict(os.environ,{'RECON_REQUIRE_MFA':'1'}):
            self.assertEqual(403,self.call('/api/runs')['status']);self.assertEqual(200,self.call('/api/security')['status']);self.assertEqual(200,self.call('/api/me')['status'])

    def test_trusted_sso_requires_secret_and_existing_member(self):
        with patch.dict(os.environ,{'RECON_SSO_PROXY_SECRET':'trusted-secret-'*4,'RECON_SSO_ONLY':'1'}):
            self.assertEqual(403,self.call('/api/sso-login','POST',headers={'HTTP_X_FORWARDED_EMAIL':'demo@reconcile.local'})['status'])
            headers={'HTTP_X_FORWARDED_EMAIL':'demo@reconcile.local','HTTP_X_RECON_PROXY_SECRET':'trusted-secret-'*4}
            self.assertEqual(200,self.call('/api/sso-login','POST',headers=headers)['status'])
            headers['HTTP_X_FORWARDED_EMAIL']='unprovisioned@example.com'
            self.assertEqual(403,self.call('/api/sso-login','POST',headers=headers)['status'])
            self.assertEqual(403,self.call('/api/login','POST',{'email':'demo@reconcile.local','password':'anything'})['status'])

    def test_recovery_token_single_use_no_account_enumeration(self):
        with patch.dict(os.environ,{'RECON_SMTP_HOST':'smtp.test','RECON_PUBLIC_URL':'https://reconcile.test'}),patch.object(self.app,'deliver_reset') as delivery:
            first=self.call('/api/password-reset/request','POST',{'email':'demo@reconcile.local'})
            missing=self.call('/api/password-reset/request','POST',{'email':'missing@example.com'})
            self.assertTrue(self.app.dispatch_mail_once())
            self.assertEqual(first['body'],missing['body']);self.assertEqual(1,delivery.call_count)
            token=delivery.call_args[0][1].split('#reset=')[1]
            payload={'token':token,'password':'new-reset-password-123'}
            self.assertEqual(200,self.call('/api/password-reset/complete','POST',payload)['status'])
            self.assertEqual(400,self.call('/api/password-reset/complete','POST',payload)['status'])

    def test_chunked_upload_offsets_checksums_ownership_and_sealing(self):
        self.login();uid=self.call('/api/uploads','POST',{'name':'test.csv'})['body']['id'];block=b'id,v\na,1\n'
        payload={'offset':0,'data':base64.b64encode(block).decode(),'sha256':hashlib.sha256(block).hexdigest()}
        self.assertEqual(200,self.call('/api/uploads/'+uid,'POST',payload)['status'])
        self.assertEqual(409,self.call('/api/uploads/'+uid,'POST',payload)['status'])
        self.assertEqual(409,self.call('/api/uploads/'+uid+'/complete','POST',{'sha256':'bad'})['status'])
        self.assertEqual(200,self.call('/api/uploads/'+uid+'/complete','POST',{'sha256':payload['sha256']})['status'])
        self.assertEqual(409,self.call('/api/uploads/'+uid,'POST',payload)['status'])
        result=self.call('/api/runs','POST',{'dataset':'Chunked','source_upload':uid,'destination_upload':uid,'rules':{'keys':['id'],'fields':{}}})
        self.assertEqual(202,result['status']);self.execute(result['body']['id'])
        self.assertEqual(0,self.call('/api/runs/'+result['body']['id'])['body']['summary']['exception_count'])

    def test_upload_larger_than_original_limit(self):
        self.login();uid=self.call('/api/uploads','POST',{})['body']['id'];block=b'a,1\n'*(1024*1024//4);offset=0
        for i in range(13):
            response=self.call('/api/uploads/'+uid,'POST',{'offset':offset,'data':base64.b64encode(block).decode()})
            self.assertEqual(200,response['status']);offset=response['body']['offset']
        self.assertGreater(offset,12*1024*1024)
        self.assertEqual(200,self.call('/api/uploads/'+uid+'/complete','POST')['status'])

    def test_metrics_authorization_operations_and_hold(self):
        self.login();rid=self.queued()
        self.assertEqual(200,self.call('/api/operations')['status']);self.assertEqual(403,self.call('/metrics')['status'])
        with patch.dict(os.environ,{'RECON_METRICS_TOKEN':'metrics-secret-'*4}):
            result=self.call('/metrics',headers={'HTTP_AUTHORIZATION':'Bearer '+'metrics-secret-'*4})
            self.assertEqual(200,result['status']);self.assertIn(b'reconcile_disk_free_bytes',result['raw'])
        self.assertEqual(200,self.call('/api/runs/'+rid+'/hold','POST',{'hold':True,'reason':'Preserve evidence for the audit.'})['status'])
        with self.app.db() as db:self.assertEqual(1,db.execute('SELECT legal_hold FROM retention').fetchone()[0])

    def test_encrypted_archival_authentication_legal_hold_and_purge(self):
        from reconcile.governance import retain,decrypt_file
        self.login();rid=self.queued();self.execute(rid)
        with self.app.db() as db:db.execute("UPDATE runs SET created='2020-01-01T00:00:00+00:00' WHERE id=?",(rid,))
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'RECON_ARCHIVE_KEY':base64.urlsafe_b64encode(b'k'*32).decode()}):
            destination=Path(directory)/'archives'
            self.assertEqual('would_archive',retain(self.app,destination,90)[0]['action'])
            with self.app.db() as db:db.execute('INSERT INTO retention(run_id,legal_hold) VALUES(?,1)',(rid,))
            self.assertEqual('held',retain(self.app,destination,90,True)[0]['action'])
            with self.app.db() as db:db.execute('UPDATE retention SET legal_hold=0 WHERE run_id=?',(rid,))
            report=retain(self.app,destination,90,True,True);self.assertTrue(report[0]['purged'])
            archive=destination/(rid+'.recon');restored=Path(directory)/'restored.zip';decrypt_file(archive,restored)
            self.assertTrue(restored.read_bytes().startswith(b'PK'))
            corrupt=bytearray(archive.read_bytes());corrupt[-1]^=1;bad=Path(directory)/'tampered.recon';bad.write_bytes(corrupt)
            with self.assertRaises(Exception):decrypt_file(bad,os.devnull)
            self.assertEqual('archived',self.call('/api/runs/'+rid)['body']['state'])
            self.assertEqual(409,self.call('/api/runs/'+rid+'/snapshot')['status'])

    def test_connector_cannot_bypass_dataset_permissions(self):
        self.login();self.app.enforce_acl=True
        with patch.dict(os.environ,{'RECON_CONNECTIONS':''}):self.assertEqual([],self.call('/api/connections')['body']['aliases'])
        uid=self.call('/api/users','POST',{'email':'connector-test@example.com','password':'long-test-password','role':'analyst'})['body']['id']
        user={'id':uid,'role':'analyst'}
        from reconcile.server import APIError
        with patch.dict(os.environ,{'RECON_CONNECTIONS':json.dumps({'private':{'dataset':'Private accounts','source':{},'destination':{}}})}):
            with self.assertRaises(APIError):self.app.validate_connection_payload({'connection':'private','dataset':'Different name'},user)
            with self.assertRaises(APIError):self.app.validate_connection_payload({'connection':'private','dataset':'Private accounts'},user)
            with self.app.db() as db:db.execute('INSERT INTO dataset_access VALUES(?,?,?)',('Private accounts',uid,'write'))
            self.app.validate_connection_payload({'connection':'private','dataset':'Private accounts'},user)

    def test_upload_quota_and_expired_scratch_cleanup(self):
        self.login();uid=self.call('/api/uploads','POST',{})['body']['id'];block=b'abcd'
        with patch.dict(os.environ,{'RECON_UPLOAD_QUOTA_BYTES':'3'}):
            self.assertEqual(413,self.call('/api/uploads/'+uid,'POST',{'offset':0,'data':base64.b64encode(block).decode()})['status'])
        self.assertEqual(200,self.call('/api/uploads/'+uid,'POST',{'offset':0,'data':base64.b64encode(block).decode()})['status'])
        with self.app.db() as db:db.execute('UPDATE upload_sessions SET created=? WHERE id=?',(time.time()-90000,uid))
        self.assertEqual(1,self.app.cleanup_uploads());self.assertFalse((self.app.data/'uploads'/uid).exists())

    def test_recovery_attempts_are_limited_without_consuming_valid_tokens(self):
        for _ in range(10):self.assertEqual(400,self.call('/api/password-reset/complete','POST',{'token':'wrong','password':'a-long-new-password'})['status'])
        self.assertEqual(429,self.call('/api/password-reset/complete','POST',{'token':'wrong','password':'a-long-new-password'})['status'])

    def test_object_lock_archive_request_requires_compliance_and_kms(self):
        from reconcile.governance import locked_s3_copy
        from unittest.mock import MagicMock
        client=MagicMock();client.get_object_lock_configuration.return_value={'ObjectLockConfiguration':{'ObjectLockEnabled':'Enabled'}};client.put_object.return_value={'VersionId':'immutable-v1'}
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'RECON_ARCHIVE_S3_BUCKET':'test-bucket','RECON_ARCHIVE_KMS_KEY':'test-kms-key'}),patch.dict(sys.modules,{'boto3':types.SimpleNamespace(client=lambda service:client)}):
            file=Path(folder)/'evidence.recon';file.write_bytes(b'encrypted-test-artifact')
            result=locked_s3_copy(file,30);self.assertEqual('immutable-v1',result['version'])
            kwargs=client.put_object.call_args.kwargs;self.assertEqual('COMPLIANCE',kwargs['ObjectLockMode']);self.assertEqual('aws:kms',kwargs['ServerSideEncryption'])
            client.get_object_lock_configuration.return_value={}
            with self.assertRaises(ValueError):locked_s3_copy(file,30)

    def test_independent_audit_archive_has_verifiable_checkpoint(self):
        from reconcile.governance import archive_ledger,decrypt_file
        import zipfile
        self.login()
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'RECON_ARCHIVE_KEY':base64.urlsafe_b64encode(b'z'*32).decode()}):
            result=archive_ledger(self.app,Path(folder)/'independent',30)
            decoded=Path(folder)/'ledger.zip';decrypt_file(result['archive'],decoded)
            with zipfile.ZipFile(decoded) as archive:
                checkpoint=json.loads(archive.read('checkpoint.json'));self.assertEqual(result['head'],checkpoint['head']);self.assertGreater(checkpoint['events'],0)

if __name__=='__main__':unittest.main()
