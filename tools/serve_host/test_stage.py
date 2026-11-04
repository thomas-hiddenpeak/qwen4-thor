"""No-listener stage failure tests: custody, bounded HTTP and cleanup order."""
import os
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from tools.serve_host import stage as s


class Driver(unittest.TestCase):
    def fixture(self):
        parent=s.BASE/'driver-fixtures';parent.mkdir(exist_ok=True)
        root=Path(tempfile.mkdtemp(dir=parent))
        run=object.__new__(s.Run)
        run.started=run.gpu_started=run.gpu_finished=time.monotonic()
        run.fronts={};run.logs=[];run.children={};run.custody=None;run.custody_done=False
        run.run_dir=root/'not-started';run.counts=dict(controls=0,generations=0,negatives=0)
        run.launch=Mock();run.stop_backend=Mock();run.business=Mock();run.cleaning=False
        return root/'execution',run

    def test_preflight_abort_never_launches_and_remains_environmental(self):
        output,run=self.fixture()
        with patch.object(s,'OUT',output),patch.object(s.retained.a,'require_idle',
                side_effect=RuntimeError('ENV_ABORT: GPU occupied')):
            with self.assertRaisesRegex(RuntimeError,'ENV_ABORT'):run.execute()
        run.launch.assert_not_called()
        self.assertEqual(s.pkg.read(output/'result.json')['status'],'ENV_ABORT')

    def test_cleanup_continues_after_component_failure_and_custody_is_last(self):
        output,run=self.fixture();seen=[]
        def component(name):
            seen.append(name)
            if name=='prometheus':raise ValueError('injected stop failure')
        run.stop_component=component
        run.fronts={1:dict(process=SimpleNamespace(poll=lambda:None,wait=lambda _:seen.append('front-exited')))}
        run.front_cli=lambda *_:seen.append('front-stop')
        run.stop_backend=lambda:seen.append('backend-stop')
        run.custody=SimpleNamespace(finish=lambda **kw:seen.append(('custody',kw['cleanup_confirmed'])))
        with patch.object(s,'OUT',output),patch.object(s.retained.a,'require_idle',side_effect=lambda *_:seen.append('GPU-probe')):
            with self.assertRaisesRegex(RuntimeError,'injected stop failure'):run.cleanup()
        self.assertEqual(seen,['prometheus','observer','front-stop','front-exited','backend-stop','GPU-probe',('custody',False)])
        self.assertTrue(run.custody_done)

    def test_http_cannot_start_without_or_after_original_lease_custody(self):
        _,run=self.fixture();run.budget=Mock();run.api_index=0
        with patch.object(s.http.client,'HTTPConnection') as connect:
            with self.assertRaisesRegex(RuntimeError,'requires original lease'):run.api('blocked',19448,'/metrics')
            run.custody=Mock();run.custody_done=True
            with self.assertRaisesRegex(RuntimeError,'requires original lease'):run.api('released',19448,'/metrics')
        connect.assert_not_called()

    def test_paused_observer_is_resumed_before_owned_stop(self):
        output,run=self.fixture();output.mkdir();run.observer=output/'observer';run.observer.mkdir()
        s.pkg.save(run.observer/'observer-terminal.json',dict(state='STOPPED',cleanup_confirmed=True))
        run.observer_paused=True;seen=[]
        child=SimpleNamespace(poll=lambda:None,returncode=0)
        run.children={'observer':dict(owner={'pid':999999999},process=child,log=SimpleNamespace(thread=None,error=None))}
        with patch.object(s,'OUT',output),patch.object(s.sup,'signal_owner',side_effect=lambda o,sig:seen.append(sig)), \
                patch.object(s.sup.processes,'stop_children',side_effect=lambda *a,**k:seen.append('reaped')):
            run.stop_component('observer')
        self.assertEqual(seen,[signal.SIGCONT,signal.SIGTERM,'reaped'])
        self.assertFalse(run.observer_paused)

    def test_custody_monitor_failure_is_environmental_not_product_failure(self):
        output,run=self.fixture();run.cleanup=Mock()
        run.business.side_effect=s.EnvironmentAbort('monitor failed without prefix')
        with patch.object(s,'OUT',output),patch.object(s.retained.a,'require_idle'), \
                patch.object(s.retained.a,'lock_available',return_value=True),patch.object(s,'check'):
            with self.assertRaises(s.EnvironmentAbort):run.execute()
        self.assertEqual(s.pkg.read(output/'result.json')['status'],'ENV_ABORT')
        run.cleanup.assert_called_once()


if __name__=='__main__':unittest.main()
