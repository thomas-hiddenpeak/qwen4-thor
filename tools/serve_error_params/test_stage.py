"""CPU safeguards for the bounded acceptance driver; no HTTP or GPU."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools.serve_error_params import stage as s


class Driver(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(dir=s.BASE,prefix='driver-private-'))

    def test_identity_failure_still_cleans_actual_own_child(self):
        children=s.Children(self.root/'unused-lease')
        with mock.patch.object(s.child.custody,'proc',side_effect=RuntimeError('identity failed')):
            with self.assertRaisesRegex(RuntimeError,'identity failed'):
                children.launch(self.root/'child',[sys.executable,'-c','import time; time.sleep(30)'])
        self.assertEqual(len(children.children),1)
        process=children.children[0][0]
        try:self.assertIsNone(process.poll())
        finally:children.cleanup()
        self.assertIsNotNone(process.returncode)
        self.assertFalse(Path(f'/proc/{process.pid}').exists())

    def test_gpu_observation_abort_prevents_model_launch(self):
        run=object.__new__(s.Run)
        run.counts=dict(starts=0,generation_attempts=0,controls=0)
        run.started=time.monotonic();run.sockets=[]
        run.children=mock.Mock()
        with mock.patch.object(s.s,'OUT',self.root), \
             mock.patch.object(s.s.a,'require_idle',side_effect=RuntimeError('ENV_ABORT: occupied')), \
             mock.patch.object(run,'launch') as launch:
            with self.assertRaisesRegex(RuntimeError,'ENV_ABORT'):run.execute()
        launch.assert_not_called();run.children.cleanup.assert_called_once()
        record=json.loads((self.root/'result.json').read_bytes())
        self.assertEqual(record['status'],'ENV_ABORT')
        self.assertEqual(record['counts']['starts'],0)

    def test_previously_reaped_child_does_not_prevent_remaining_cleanup(self):
        children=s.Children(self.root/'unused-lease')
        live=children.launch(self.root/'live',[sys.executable,'-c','import time; time.sleep(30)'])
        exited=children.launch(self.root/'exited',[sys.executable,'-c','pass'])
        try:
            self.assertEqual(exited.wait(timeout=5),0)
            children.cleanup()
            self.assertIsNotNone(live.returncode)
            self.assertFalse(Path(f'/proc/{live.pid}').exists())
        finally:
            if live.poll() is None:live.kill();live.wait(timeout=5)

    def test_wrong_error_location_stops_before_more_requests(self):
        run=object.__new__(s.Run);run.negative_results=[]
        def wrong(label,*args,**kwargs):
            s.cpu.save(self.root/(label+'-http.json'),dict(status=400,headers={
                'X-Request-Id':label,'Content-Type':'application/json',
                'X-Q4T-Gateway-Request-Id':'fixture'}))
            return dict(error=dict(param=None,code='invalid_request',type='invalid_request_error'))
        with mock.patch.object(s.s,'OUT',self.root),mock.patch.object(run,'idle',return_value={}), \
             mock.patch.object(run,'request',side_effect=wrong) as requests:
            with self.assertRaisesRegex(RuntimeError,'wrong error location'):run.negatives()
        self.assertEqual(requests.call_count,1)
        self.assertEqual(run.negative_results,[])


if __name__=='__main__':unittest.main()
