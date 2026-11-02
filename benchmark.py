"""Measured, repeatable fixture benchmark. Does not assert general accuracy."""
import json
import platform
import tempfile
import time
import tracemalloc
import argparse
import csv
from pathlib import Path
from engine import reconcile
from synthetic import DEFAULT_RULES,generate

def benchmark(size=10000):
    if size<1000:raise ValueError('Synthetic benchmark requires at least 1,000 records.')
    _,_,truth=generate(size=1000)
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder);a=path/'source.csv';b=path/'destination.csv'
        with a.open('w',encoding='utf-8',newline='') as sa,b.open('w',encoding='utf-8',newline='') as sb:
            source=csv.writer(sa);dest=csv.writer(sb);header=['customer_id','name','balance','joined'];source.writerow(header);dest.writerow(header)
            for i in range(size):
                row=[f'C{i:05}',f'Customer {i}',str(1000+i),'2026-01-01'];source.writerow(row)
                if i<50:continue
                if 50<=i<80:row[2]=str(1450+i)
                if 100<=i<125:row[3]='invalid-date'
                dest.writerow(row)
                if 80<=i<100:dest.writerow(row)
            for i in range(25):dest.writerow([f'X{i:03}','Additional customer','4100','2026-01-01'])
        actual=set()
        def emit(e):actual.add((e['category'],e['key'],e['column']))
        tracemalloc.start();start=time.perf_counter()
        result=reconcile(a,b,DEFAULT_RULES,path/'stage.sqlite',emit)
        elapsed=time.perf_counter()-start;_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        other=reconcile(a,b,DEFAULT_RULES,path/'repeat.sqlite',lambda e:None)
        truth_keys={key for _,key,_ in truth};flagged={key for _,key,_ in actual}
        false_keys=flagged-truth_keys
        metrics={'measured_date':'2026-10-28','python':platform.python_version(),'platform':platform.platform(),
                 'source_rows':result['source_rows'],'destination_rows':result['destination_rows'],
                 'matching_records':result['matching_records'],'exception_events':result['exception_count'],
                 'exact_events_expected':len(truth),'exact_events_detected':len(actual & truth),
                 'detection_recall':len(flagged & truth_keys)/len(truth_keys),
                 'false_positive_rate':len(false_keys)/(result['distinct_keys']-len(truth_keys)),
                 'classification_accuracy':len(actual & truth)/len(truth),
                 'runtime_seconds_with_tracemalloc':round(elapsed,6),'peak_python_allocations_bytes':peak,
                 'reproducible_summary':result==other,'fingerprint':result['fingerprint'],
                 'scope':f'One {size:,}-row deterministic fixture written incrementally. Runtime excludes fixture generation, file ingestion, API persistence and export. Memory is Python allocations, not total RSS or SQLite native memory.'}
        return metrics

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--rows',type=int,default=10000);parser.add_argument('--output',default='measured_results.json');args=parser.parse_args()
    metrics=benchmark(args.rows);Path(args.output).write_text(json.dumps(metrics,indent=2),encoding='utf-8');print(json.dumps(metrics,indent=2))
