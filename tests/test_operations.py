import contextlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Application,password_matches
from manage import main

class OperationsTests(unittest.TestCase):
    def test_backup_restore_integrity_and_operator_password_reset(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'RECON_SECRET':'test-secret-'*6,'RECON_ADMIN_EMAIL':'admin@example.com','RECON_ADMIN_PASSWORD':'long-admin-password'}):
            root=Path(folder);data=root/'live';app=Application(data,demo=False,start_worker=False)
            rid=app.enqueue({'id':'admin'},{'dataset':'Backup fixture','source_csv':'id,v\na,1\n','destination_csv':'id,v\na,2\n','rules':{'keys':['id'],'fields':{}}})
            with app.db() as db:run=dict(db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone())
            app.execute_run(run);app.close();archive=root/'backup.zip'
            with contextlib.redirect_stdout(io.StringIO()):
                with patch.object(sys,'argv',['manage.py','--data-dir',str(data),'backup',str(archive)]):main()
                restored=root/'restored';restored.mkdir()
                with zipfile.ZipFile(archive) as z:
                    self.assertIn('reconcile.sqlite',z.namelist());self.assertEqual(3,len(z.namelist()));z.extractall(restored)
                with patch.object(sys,'argv',['manage.py','--data-dir',str(restored),'verify-audit']):main()
                with patch.object(sys,'argv',['manage.py','--data-dir',str(restored),'reset-password','admin@example.com']),patch('getpass.getpass',side_effect=['replacement-password-123','replacement-password-123']):main()
            check=Application(restored,demo=False,start_worker=False)
            try:
                with check.db() as db:
                    self.assertTrue(password_matches('replacement-password-123',db.execute('SELECT password FROM users').fetchone()[0]))
                    self.assertEqual(1,db.execute('SELECT COUNT(*) FROM issues').fetchone()[0])
                    self.assertEqual('completed',db.execute('SELECT state FROM runs').fetchone()[0])
                    snapshot=db.execute('SELECT source_file FROM runs').fetchone()[0]
                    self.assertEqual('id,v\na,1\n',(restored/'snapshots'/snapshot).read_text())
            finally:check.close()

if __name__=='__main__':unittest.main()
