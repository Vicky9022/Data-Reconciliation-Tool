import json
import sys
import tempfile
import unittest
from pathlib import Path
from reconcile.engine import reconcile, validate_rules, compare
from reconcile.synthetic import DEFAULT_RULES,generate

class EngineTests(unittest.TestCase):
    def compare_csv(self,source,destination,rules=None):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);(base/'s.csv').write_text(source,encoding='utf-8');(base/'d.csv').write_text(destination,encoding='utf-8')
            events=[];result=reconcile(base/'s.csv',base/'d.csv',rules or {'keys':['id'],'fields':{}},base/'stage.sqlite',events.append)
            return result,events

    def test_exact_injection_oracle_and_reproducibility(self):
        a,b,truth=generate();result,events=self.compare_csv(a,b,DEFAULT_RULES)
        actual={(e['category'],e['key'],e['column']) for e in events}
        self.assertEqual(truth,actual);self.assertEqual(150,len(events));self.assertEqual(9875,result['matching_records'])
        self.assertEqual((result,events),self.compare_csv(a,b,DEFAULT_RULES))

    def test_corrected_dataset(self):
        a,b,_=generate(corrected=True);result,events=self.compare_csv(a,b,DEFAULT_RULES)
        self.assertEqual([],events);self.assertEqual(10000,result['matching_records'])

    def test_composite_key_reordered_rows(self):
        a='id,txn,value\n1,a,10\n1,b,20\n';b='id,txn,value\n1,b,20\n1,a,10\n'
        result,events=self.compare_csv(a,b,{'keys':['id','txn'],'fields':{}})
        self.assertEqual([],events);self.assertEqual(2,result['matching_records'])

    def test_duplicate_isolation_and_overlap(self):
        _,events=self.compare_csv('id,v\na,1\na,2\n','id,v\nb,3\nb,4\n')
        self.assertEqual({'duplicate_source','duplicate_destination','missing_destination','unexpected_destination'},{e['category'] for e in events})
        _,events=self.compare_csv('id,v\na,1\na,2\n','id,v\na,3\n')
        self.assertEqual(['duplicate_source'],[e['category'] for e in events])

    def test_exact_decimal_tolerance_and_precision(self):
        self.assertTrue(compare('1.00','1.01',{'type':'numeric','tolerance':'.01'}))
        self.assertFalse(compare('1.00','1.01001',{'type':'numeric','tolerance':'.01'}))
        self.assertFalse(compare('1000000000000000000000000000000.001','1000000000000000000000000000000.002',{'type':'numeric'}))

    def test_nullability_normalization_explicit(self):
        self.assertTrue(compare('', '', {}));self.assertFalse(compare(' A ','a',{}))
        self.assertTrue(compare(' A ','a',{'trim':True,'casefold':True}))
        with self.assertRaises(ValueError):compare('','',{'nullable':False})

    def test_dates_and_timezone(self):
        self.assertTrue(compare('2026-01-01','2026-01-02',{'type':'date','tolerance':1}))
        self.assertTrue(compare('2026-01-01T12:00:00+05:30','2026-01-01T06:30:00Z',{'type':'date'}))
        _,events=self.compare_csv('id,d\na,2026-01-01\n','id,d\na,2026-01-01T00:00:00Z\n',{'keys':['id'],'fields':{'d':{'type':'date'}}})
        self.assertEqual('invalid_value',events[0]['category'])

    def test_missing_and_changed_schema(self):
        _,events=self.compare_csv('id,v\na,1\n','id,x\na,1\n',{'keys':['id'],'fields':{},'schemas':{'source':{'id':'integer'},'destination':{'id':'text'}}})
        self.assertEqual({'missing_column','unexpected_column','changed_type'},{e['category'] for e in events})

    def test_invalid_key(self):
        _,events=self.compare_csv('id,v\n,10\n','id,v\n,10\n')
        self.assertEqual(2,len(events));self.assertTrue(all(e['category']=='invalid_key' for e in events))

    def test_malformed_csv_and_headers(self):
        for content in ('id,id\na,1\n','id,v\na,1,2\n','id,v\na\n','x,v\na,1\n',''):
            with self.subTest(content=content),self.assertRaises(ValueError):self.compare_csv(content,'id,v\na,1\n')

    def test_invalid_rule_rejected(self):
        for rule in ({'type':'magic'},{'type':'numeric','tolerance':'NaN'},{'type':'numeric','tolerance':-1},{'trim':'yes'},{'type':'text','tolerance':1}):
            with self.subTest(rule=rule),self.assertRaises(ValueError):validate_rules({'keys':['id'],'fields':{'v':rule}})
        with self.assertRaises(ValueError):validate_rules({'keys':['id','id']})
        with self.assertRaises(ValueError):validate_rules({'keys':['id'],'fields':{'v':{'type':'text'}},'schemas':{'source':{'v':'x'*70000}}})

    def test_invalid_numeric_not_nan_match(self):
        for value in ('NaN','Infinity','bad','1e1001'):
            _,events=self.compare_csv('id,v\na,'+value+'\n','id,v\na,'+value+'\n',{'keys':['id'],'fields':{'v':{'type':'numeric'}}})
            self.assertEqual('invalid_value',events[0]['category'])

    def test_duplicate_evidence_cap(self):
        a='id,v\n'+''.join('a,1\n' for _ in range(100))
        _,events=self.compare_csv(a,'id,v\na,1\n')
        self.assertEqual(20,len(events[0]['source']));self.assertIn('100 rows',events[0]['detail'])

if __name__=='__main__':unittest.main()
