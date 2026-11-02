"""Independent deterministic fixture and exact event oracle."""
import csv
import io
from engine import canonical

DEFAULT_RULES = {'version':'1.0', 'keys':['customer_id'], 'fields':{
    'balance':{'type':'numeric','tolerance':'0.01','severity':'high','nullable':False},
    'joined':{'type':'date','tolerance':'0','nullable':False},
    'name':{'type':'text','trim':False,'casefold':False}}}

def generate(size=10000, corrected=False):
    source=[dict(customer_id=f'C{i:05}',name=f'Customer {i}',balance=str(1000+i),joined='2026-01-01') for i in range(size)]
    dest=[r.copy() for r in source]
    truth=set()
    if not corrected:
        dest=dest[50:]
        for row in dest[:30]: row['balance']=str(int(row['balance'])+450)
        dest += [r.copy() for r in dest[30:50]]
        dest += [dict(customer_id=f'X{i:03}',name='Additional customer',balance='4100',joined='2026-01-01') for i in range(25)]
        for row in dest[50:75]: row['joined']='invalid-date'
        for category, ids, field in [('missing_destination',range(50),''),('value_mismatch',range(50,80),'balance'),('duplicate_destination',range(80,100),''),('invalid_value',range(100,125),'joined')]:
            truth.update((category,canonical([f'C{i:05}']),field) for i in ids)
        truth.update(('unexpected_destination',canonical([f'X{i:03}']),'') for i in range(25))
    def encode(rows):
        buf=io.StringIO(newline='');writer=csv.DictWriter(buf,fieldnames=['customer_id','name','balance','joined']);writer.writeheader();writer.writerows(rows)
        return buf.getvalue()
    return encode(source),encode(dest),truth
