"""Bounded CPU checks of negative-probe construction and failure retention."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('seed_fixture',ROOT/'tools/prefix_seed/run_stage.py')
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)
BASE=ROOT/'.q4t-work/prefix-seed-20261028/fixture-revision'


class ProbeTests(unittest.TestCase):
    def test_all_probes_have_valid_independent_cancel_capabilities(self):
        expected=['requires prefix_cache_scope','must be 0 or an integer','not an allowed checkpoint','requires an uncached prompt token']
        bodies=[]
        def fake(req,timeout):
            self.assertEqual(timeout,30)
            body=json.loads(req.data);i=len(bodies);bodies.append(body)
            self.assertEqual(body['request_id'],'seed-reject-'+str(i))
            self.assertRegex(body['cancel_token'],r'^[0-9a-f]{64}$')
            raise urllib.error.HTTPError(req.full_url,400,'Bad Request',{},io.BytesIO(expected[i].encode()))
        with tempfile.TemporaryDirectory(dir=BASE) as d, patch.object(driver.urllib.request,'urlopen',side_effect=fake):
            driver.rejected_probes({'port':1},Path(d))
            rows=json.loads((Path(d)/'rejected-probes.json').read_text())
            self.assertEqual(len(rows),4)
            self.assertEqual(len({r['cancel_token'] for r in bodies}),4)
            self.assertEqual([r['response'] for r in rows],expected)

    def test_unexpected_response_is_retained_before_failure(self):
        def fake(req,timeout):
            raise urllib.error.HTTPError(req.full_url,409,'Conflict',{},io.BytesIO(b'unexpected earlier gate'))
        with tempfile.TemporaryDirectory(dir=BASE) as d, patch.object(driver.urllib.request,'urlopen',side_effect=fake):
            with self.assertRaisesRegex(RuntimeError,'wrong seed rejection'):
                driver.rejected_probes({'port':1},Path(d))
            rows=json.loads((Path(d)/'rejected-probes.json').read_text())
            self.assertEqual(rows[0]['status'],409)
            self.assertEqual(rows[0]['response'],'unexpected earlier gate')


if __name__=='__main__':unittest.main()
