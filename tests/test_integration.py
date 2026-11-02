"""Requires the disposable Docker staging stack. Never target production."""
import json
import os
import secrets
import time
import unittest
from unittest.mock import patch

ENABLED=os.environ.get('RECON_RUN_INTEGRATION')=='1'

@unittest.skipUnless(ENABLED,'Live staging not enabled; set RECON_RUN_INTEGRATION=1 after starting deploy/staging.yaml')
class PostgreSQLIntegration(unittest.TestCase):
    def test_readonly_role_cannot_modify_source(self):
        import psycopg
        dsn=os.environ.get('RECON_TEST_READ_DSN','postgresql://recon_read:local-read-password@127.0.0.1:6543/reconcile')
        with psycopg.connect(dsn) as db:
            self.assertEqual(10000,db.execute('SELECT COUNT(*) FROM source_data.customers').fetchone()[0])
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):db.execute("UPDATE source_data.customers SET balance=0 WHERE customer_id='C00001'")

    def test_job_lease_fencing_against_real_postgres(self):
        from distributed import Broker
        dsn=os.environ.get('RECON_TEST_JOB_DSN','postgresql://recon_jobs:local-job-password@127.0.0.1:6543/reconcile')
        with patch.dict(os.environ,{'RECON_JOB_DATABASE_URL':dsn,'RECON_LOCAL_STAGE':'1'}):
            broker=Broker();rid='integration-'+secrets.token_hex(10)
            broker.submit(rid,{'test':'lease'})
            # Claim just this test row, leaving the running stage's normal jobs untouched.
            with broker.connect() as db:job=db.execute("UPDATE reconcile_jobs SET state='running',owner='first',lease_until=%s WHERE id=%s RETURNING *",(time.time()+120,rid)).fetchone()
            self.assertTrue(broker.renew(rid,'first'));self.assertFalse(broker.finish(rid,'wrong-owner',{'bad':True}))
            self.assertTrue(broker.finish(rid,'first',{'verified':True}));self.assertFalse(broker.finish(rid,'first',{'late':True}))

    def test_live_database_ingestion_through_remote_worker(self):
        from loadtest import request
        from synthetic import DEFAULT_RULES
        base=os.environ.get('RECON_TEST_URL','http://127.0.0.1:8766')
        user,headers=request(base,'/api/demo-login',payload={});cookie=headers['Set-Cookie'].split(';')[0]
        created,_=request(base,'/api/runs',cookie,{'dataset':'PostgreSQL integration acceptance','connection':'customer-lab','rules':DEFAULT_RULES},user['csrf'])
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            result,_=request(base,'/api/runs/'+created['id'],cookie)
            if result['state'] in ('completed','failed'):break
            time.sleep(.5)
        self.assertEqual('completed',result['state'],result.get('error'))
        self.assertEqual(10000,result['summary']['source_rows']);self.assertEqual(125,result['summary']['exception_count']);self.assertEqual(9900,result['summary']['matching_records'])

if __name__=='__main__':unittest.main()
