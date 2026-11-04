"""Retention contract and zero-work continuation gates; no listeners."""
import copy
from pathlib import Path
import tempfile
import unittest

from tools.serve_host import continuation as c


class Continuation(unittest.TestCase):
    def test_actual_store_accepts_corrected_minimum_policy(self):
        root=Path(tempfile.mkdtemp(dir=c.ORIGINAL))/'store'
        c.s.retained.store.init(root,**c.STORE_POLICY)
        value=c.pkg.read(root/'store.json')
        self.assertEqual(value['policy'],c.STORE_POLICY)
        self.assertEqual(list((root/'generations').iterdir()),[])

    def test_original_failure_is_before_any_model_http_or_gpu_window(self):
        value=c.pkg.read(c.ORIGINAL/'execution/result.json');c.original_gate(value)
        for key,replacement in (('gpu_started',1),('gpu_finished',2),('status','PASS')):
            with self.subTest(key=key),self.assertRaises(RuntimeError):c.original_gate(dict(value,**{key:replacement}))
        changed=copy.deepcopy(value);changed['counts']['generations']=1
        with self.assertRaises(RuntimeError):c.original_gate(changed)

    def test_continuation_keeps_model_implementations_and_business_bounds(self):
        plan=c.pkg.read(c.ORIGINAL/'plan.json')
        self.assertEqual(plan['limits'],c.s.LIMITS);self.assertEqual(plan['gpu_window_seconds'],c.s.GPU_WINDOW)
        for name,checksum in plan['source_files'].items():self.assertEqual(c.pkg.sha(c.ROOT/name),checksum,name)
        self.assertEqual(plan['complete_responses'],4);self.assertEqual(plan['real_tools'],2)
        self.assertEqual(c.STORE_POLICY['max_generations'],plan['store_policy']['max_generations'])


if __name__=='__main__':unittest.main()
