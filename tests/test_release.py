import unittest
import json
import os
import shutil
import subprocess
import tempfile
import secrets
import sys
from pathlib import Path
from scripts.deploy import configure,portable_image_fingerprint

class ReleaseConfigurationTests(unittest.TestCase):
    def test_strong_distinct_keys_and_literal_password(self):
        content=configure('reconcile.example.com','admin@example.com','a-long-password-$value')
        rows=dict(line.split('=',1) for line in content.splitlines())
        self.assertNotEqual(rows['RECON_SECRET'],rows['RECON_MFA_KEY'])
        self.assertIn("'a-long-password-$value'",content)
        self.assertEqual("'1'",rows['RECON_REQUIRE_MFA'])
        image={'Id':'containerd-index','Os':'linux','Architecture':'amd64','RootFS':{'Layers':['layer-a']},'Config':{'User':'10001','Cmd':['server']}}
        original=portable_image_fingerprint(image);image['Id']='classic-config-id'
        self.assertEqual(original,portable_image_fingerprint(image))
        image['Config']['User']='0';self.assertNotEqual(original,portable_image_fingerprint(image))
    def test_reject_bad_domain_email_and_password(self):
        for domain,email,password in [('https://bad/path','a@b.com','long-test-password'),('valid.example','no-email','long-test-password'),('valid.example','a@b.com','short')]:
            with self.assertRaises(ValueError):configure(domain,email,password)
    @unittest.skipUnless(shutil.which('docker'),'Docker Compose is required for dotenv round-trip verification')
    def test_compose_password_round_trip(self):
        password="Example 'quoted'\\path$value123"
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text(configure('reconcile.example.com','admin@example.com',password))
            (root/'compose.yaml').write_text('services:\n  app:\n    image: postgres:16\n    environment:\n      PASSWORD: ${RECON_ADMIN_PASSWORD}\n')
            env={key:value for key,value in os.environ.items() if not key.startswith('RECON_')}
            env['COMPOSE_PROJECT_NAME']='reconcile-config-fixture-'+secrets.token_hex(6)
            result=subprocess.run(['docker','compose','config','--format','json'],cwd=root,env=env,capture_output=True,text=True,check=True)
            # Compose serialization escapes dollars for a possible second parse.
            self.assertEqual(password,json.loads(result.stdout)['services']['app']['environment']['PASSWORD'].replace('$$','$'))
            if subprocess.run(['docker','image','inspect','postgres:16'],capture_output=True).returncode==0:
                try:
                    actual=subprocess.run(['docker','compose','run','--rm','--no-deps','--entrypoint','printenv','app','PASSWORD'],cwd=root,env=env,capture_output=True,text=True,check=True)
                    self.assertEqual(password,actual.stdout.rstrip('\r\n'))
                finally:subprocess.run(['docker','compose','down','--remove-orphans'],cwd=root,env=env,capture_output=True)
