import json
from pathlib import Path
import tempfile
import unittest
from trace_audit import audit


def call(api, ident, ptr, size=0):
    common=dict(call=ident,tid=7,api=api,bytes=size,stream=4,flags=0,
                caller=123,backend='runtime',mono=float(ident))
    return [dict(common,phase='begin',ptr=0 if 'Malloc' in api else ptr,rc=-1),
            dict(common,phase='end',ptr=ptr,rc=0)]


class TraceTest(unittest.TestCase):
    def check(self,rows):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'trace.log'
            p.write_text(''.join('[memory-trace] '+json.dumps(dict(r,seq=i))+'\n'
                                 for i,r in enumerate(rows)))
            return audit(p)
    def test_reused_pointer_after_free(self):
        result=self.check(call('cudaMalloc',0,8,32)+call('cudaFree',1,8)+call('cudaMalloc',2,8,16)+call('cudaFree',3,8))
        self.assertFalse(result['errors']);self.assertFalse(result['remaining'])
        self.assertEqual(result['peak_logical_bytes']['device'],32)
    def test_unmatched_free(self):
        self.assertTrue(self.check(call('cudaFree',0,8))['errors'])
    def test_live_reuse(self):
        self.assertTrue(self.check(call('cudaMalloc',0,8,32)+call('cudaMalloc',1,8,16))['errors'])
    def test_interruption_keeps_pending(self):
        r=self.check(call('cudaMalloc',0,8,32)[:1])
        self.assertEqual(len(r['incomplete_calls']),1)
    def test_async_counts_submission(self):
        r=self.check(call('cudaMallocAsync',0,8,32)+call('cudaFreeAsync',1,8))
        self.assertEqual(r['async_free_submissions'],1)
        self.assertIn('not physical release',r['scope'])
    def test_failed_alloc_not_live(self):
        rows=call('cudaMalloc',0,0,32);rows[1]['rc']=2
        r=self.check(rows);self.assertEqual(r['allocations'],0)
        self.assertEqual(len(r['failed_calls']),1)

if __name__=='__main__':unittest.main()
