"""Check installation boundaries that can break when repository paths change."""
import contextlib
import importlib.resources
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from reconcile.server import Application


class PackageTests(unittest.TestCase):
    def test_browser_assets_are_available_as_package_resources(self):
        assets = importlib.resources.files('reconcile').joinpath('static')
        for name in ('index.html', 'app.js', 'enterprise.js', 'styles.css'):
            with self.subTest(asset=name):
                self.assertGreater(len(assets.joinpath(name).read_bytes()), 0)

    def test_default_data_directory_stays_outside_installed_package(self):
        with tempfile.TemporaryDirectory() as folder:
            with contextlib.chdir(folder), patch.dict(os.environ, {}, clear=True):
                app = Application(demo=True, start_worker=False)
                try:
                    self.assertEqual(Path(folder).resolve()/'runtime', app.data)
                    self.assertTrue(app.db_path.is_file())
                finally:
                    app.close()

    def test_module_entry_points_work_outside_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            for module in ('reconcile', 'reconcile.manage'):
                with self.subTest(module=module):
                    result = subprocess.run(
                        [sys.executable, '-m', module, '--help'], cwd=folder,
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn('usage:', result.stdout.lower())
