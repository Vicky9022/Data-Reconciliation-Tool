import time
import urllib.request

deadline=time.monotonic()+240
while time.monotonic()<deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8766/healthz',timeout=5) as response:
            if response.status==200:print('Disposable staging service is ready.');break
    except Exception:time.sleep(2)
else:raise SystemExit('Staging readiness timed out. Inspect container logs.')
