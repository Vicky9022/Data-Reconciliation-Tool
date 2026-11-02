import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from distributed import execute_claim

class FakeBroker:
    def __init__(self):self.results=[];self.valid=True
    def renew(self,rid,owner):return self.valid
    def finish(self,rid,owner,result=None,error=None):
        if not self.valid:return False
        self.results.append((rid,result,error));return True

class FakeArtifacts:
    def __init__(self,source,dest):self.objects={'s':source,'d':dest}
    def get(self,key,path):Path(path).write_bytes(self.objects[key])
    def put(self,path,key):self.objects[key]=Path(path).read_bytes();return key

class DistributedTests(unittest.TestCase):
    def job(self):
        a=b'id,v\na,1\n';b=b'id,v\na,2\n';artifacts=FakeArtifacts(a,b)
        job={'id':'job1','owner':'attempt1','payload':{'source':'s','destination':'d','source_sha256':hashlib.sha256(a).hexdigest(),'destination_sha256':hashlib.sha256(b).hexdigest(),'rules':{'keys':['id'],'fields':{}}}}
        return job,artifacts
    def test_remote_artifacts_and_result_checksum(self):
        job,artifacts=self.job();broker=FakeBroker();self.assertTrue(execute_claim(broker,artifacts,job))
        _,result,error=broker.results[0];self.assertIsNone(error);self.assertEqual(1,result['summary']['exception_count'])
        self.assertEqual(hashlib.sha256(artifacts.objects[result['events']]).hexdigest(),result['events_sha256'])
    def test_corrupt_input_cannot_publish_results(self):
        job,artifacts=self.job();artifacts.objects['s']=b'tampered';broker=FakeBroker();execute_claim(broker,artifacts,job)
        self.assertIsNone(broker.results[0][1]);self.assertIn('checksum',broker.results[0][2])
    def test_lost_lease_cannot_finish(self):
        job,artifacts=self.job();broker=FakeBroker();broker.valid=False
        self.assertFalse(execute_claim(broker,artifacts,job));self.assertEqual([],broker.results)

if __name__=='__main__':unittest.main()
