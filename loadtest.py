"""Opt-in synthetic HTTP exercise; never run against a business workspace."""
import argparse
import concurrent.futures
import json
import statistics
import time
import urllib.request
from pathlib import Path

def request(base,path,cookie='',payload=None,csrf=''):
    raw=json.dumps(payload).encode() if payload is not None else None
    headers={'Cookie':cookie,'X-CSRF-Token':csrf}
    if raw is not None:headers['Content-Type']='application/json'
    with urllib.request.urlopen(urllib.request.Request(base+path,raw,headers),timeout=30) as response:
        return json.loads(response.read()),response.headers

def run(base,requests=100,concurrency=8):
    config,_=request(base,'/api/config')
    if not config.get('demo'):raise ValueError('Load exercise is restricted to explicitly enabled synthetic workspaces.')
    user,headers=request(base,'/api/demo-login',payload={});cookie=headers['Set-Cookie'].split(';')[0]
    created,_=request(base,'/api/runs',cookie,{'dataset':'Synthetic load acceptance','demo':True,'rules':config['default_rules']},user['csrf'])
    rid=created['id'];deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        result,_=request(base,'/api/runs/'+rid,cookie)
        if result['state']=='completed':break
        if result['state']=='failed':raise ValueError('Synthetic acceptance run failed.')
        time.sleep(.25)
    else:raise TimeoutError('Synthetic run did not finish within 120 seconds.')
    targets=['/healthz','/api/runs','/api/runs/'+rid+'/issues?category=value_mismatch']
    def one(index):
        start=time.perf_counter()
        try:request(base,targets[index%len(targets)],cookie);return time.perf_counter()-start,None
        except Exception as error:return time.perf_counter()-start,type(error).__name__
    start=time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:results=list(pool.map(one,range(requests)))
    elapsed=time.perf_counter()-start;durations=sorted(r[0] for r in results)
    percentile=lambda p:durations[min(len(durations)-1,int((len(durations)-1)*p))]*1000
    return {'requests':requests,'concurrency':concurrency,'errors':sum(error is not None for _,error in results),
            'elapsed_seconds':round(elapsed,3),'requests_per_second':round(requests/elapsed,2),
            'latency_p50_ms':round(percentile(.5),2),'latency_p95_ms':round(percentile(.95),2),'latency_p99_ms':round(percentile(.99),2),
            'fixture_exceptions':result['summary']['exception_count'],'fixture_source_rows':result['summary']['source_rows'],
            'scope':'Loopback synthetic workspace, warm read endpoints, no TLS/proxy. Not a production capacity guarantee.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--url',default='http://127.0.0.1:8765');parser.add_argument('--requests',type=int,default=100);parser.add_argument('--concurrency',type=int,default=8);parser.add_argument('--output',default='load_results.json');args=parser.parse_args()
    if not 1<=args.requests<=10000 or not 1<=args.concurrency<=64:parser.error('Use 1–10000 requests and 1–64 concurrent clients.')
    result=run(args.url.rstrip('/'),args.requests,args.concurrency);Path(args.output).write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
