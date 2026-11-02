"""Record the actual complete local test result; integration skips stay visible."""
import json
import platform
import time
import unittest
import shutil
import subprocess
from datetime import datetime,timezone
from pathlib import Path

if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    reports=root/'reports'
    reports.mkdir(exist_ok=True)
    suite=unittest.defaultTestLoader.discover(str(root/'tests'),top_level_dir=str(root))
    start=time.perf_counter()
    with (reports/'test_results.txt').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    docker_available=False
    if shutil.which('docker'):
        try:docker_available=subprocess.run(['docker','info','--format','{{.ServerVersion}}'],capture_output=True,timeout=10).returncode==0
        except (OSError,subprocess.TimeoutExpired):pass
    report={'timestamp_utc':datetime.now(timezone.utc).isoformat(),'python':platform.python_version(),'docker_engine_available':docker_available,
      'tests_run':result.testsRun,'passed':result.testsRun-len(result.skipped)-len(result.failures)-len(result.errors),
      'skipped':len(result.skipped),'failures':len(result.failures),'errors':len(result.errors),'duration_seconds':round(time.perf_counter()-start,3),
      'skipped_tests':[{'test':str(test),'reason':reason} for test,reason in result.skipped],
      'validation_limits':([] if docker_available else ['A working Docker engine was not available on this host.'])+['Live PostgreSQL, object-store and OIDC integrations require explicit environment acceptance tests.','Independent penetration review and production recovery objectives require external validation.'],
      'scope':'Local test suite only. Browser and container checks are recorded separately.'}
    (reports/'verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,indent=2))
    raise SystemExit(0 if result.wasSuccessful() else 1)
