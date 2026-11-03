"""Failure cleanup must keep all frontend shutdown ahead of lease release."""
from types import SimpleNamespace
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from tools.serve_retention import stage


class Driver(unittest.TestCase):
    def test_client_failure_stops_front_before_backend_before_join(self):
        seen=[]
        process=SimpleNamespace(poll=lambda:None,wait=lambda timeout:seen.append('front-exited'))
        fixture=SimpleNamespace(fronts={1:{'process':process}},cleaning=False,
            front_cli=lambda *a:seen.append('front-stop'),stop_backend=lambda:seen.append('backend-stop'))
        with self.assertRaisesRegex(ValueError,'broken client'):
            with stage.Run.concurrent_requests(fixture) as pool:
                self.assertEqual(pool.submit(lambda:1).result(),1)
                raise ValueError('broken client')
        self.assertEqual(seen,['front-stop','front-exited','backend-stop'])
        self.assertTrue(fixture.cleaning)

    def test_gpu_observation_abort_is_not_a_product_failure_or_launch(self):
        parent=stage.BASE/'driver-fixtures';parent.mkdir(exist_ok=True)
        root=Path(tempfile.mkdtemp(dir=parent));output=root/'execution'
        instance=object.__new__(stage.Run)
        instance.started=time.monotonic();instance.fronts={};instance.logs=[]
        instance.run_dir=output/'not-started';instance.counts={}
        instance.launch=mock.Mock();instance.stop_backend=mock.Mock()
        with mock.patch.object(stage,'OUT',output),mock.patch.object(stage.a,'require_idle',
                side_effect=RuntimeError('ENV_ABORT: GPU observation failed or occupied')):
            with self.assertRaisesRegex(RuntimeError,'ENV_ABORT'):instance.execute()
        instance.launch.assert_not_called()
        self.assertEqual(stage.r.pkg.read(output/'result.json')['status'],'ENV_ABORT')
