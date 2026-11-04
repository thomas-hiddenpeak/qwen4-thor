"""Actual owned-child failures and cleanup ordering; no HTTP or GPU."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_tls import stage as s


class Driver(unittest.TestCase):
    def fixture(self):
        parent=s.BASE/'driver-fixtures';parent.mkdir(exist_ok=True)
        output=Path(tempfile.mkdtemp(dir=parent))/'execution';run=object.__new__(s.Run)
        run.started=time.monotonic();run.gpu_started=run.gpu_finished=None
        run.fronts={};run.logs=[];run.pending={};run.children={};run.run_dir=output/'managed-run'
        run.counts=dict(controls=0,generations=0,negatives=0);run.cleaning=False
        run.observer_paused=False;run.receiver=None;run.source_after_stop=None;run.backend_process=None
        run.launch=Mock();run.stop_backend=Mock();run.business=Mock();run.budget=Mock()
        return output,run

    def test_busy_preflight_never_launches(self):
        output,run=self.fixture()
        with patch.object(s,'OUT',output),patch.object(s.a,'require_idle',side_effect=RuntimeError('ENV_ABORT: busy')):
            with self.assertRaisesRegex(RuntimeError,'ENV_ABORT'):run.execute()
        run.launch.assert_not_called();self.assertEqual(s.pkg.read(output/'result.json')['status'],'ENV_ABORT')

    def test_actual_early_identity_failure_reaped_before_backend_stop(self):
        output,run=self.fixture();identity=s.sup.process_identity
        def identify(pid):
            if pid!=os.getpid():raise RuntimeError('fixture child identity failure')
            return identity(pid)
        def launch():run.spawn('observer',[sys.executable,'-c','import time;time.sleep(30)'])
        def stop_backend():
            child=run.pending['observer'];self.assertIsNotNone(child.poll())
            self.assertFalse(s.sup.processes.group_members(child.pid));self.assertTrue(child.stdout.closed)
        run.launch=launch;run.stop_backend=Mock(side_effect=stop_backend)
        with patch.object(s,'OUT',output),patch.object(s.sup,'process_identity',side_effect=identify), \
             patch.object(s.a,'require_idle'),patch.object(s.a,'lock_available',return_value=True),patch.object(s,'check'):
            with self.assertRaisesRegex(RuntimeError,'owned cleanup failed'):run.execute()
        run.stop_backend.assert_called_once();self.assertEqual(s.pkg.read(output/'result.json')['status'],'FAILED')

    def test_failed_formal_stop_still_reaps_actual_child_before_backend(self):
        output,run=self.fixture();order=[]
        def launch():
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
            run.pending['observer']=child;run.children['observer']=dict(process=child,owner=None,log=None)
        def formal(label):
            if label=='observer':raise RuntimeError('fixture formal stop failed')
        def backend():
            self.assertIsNotNone(run.pending['observer'].poll())
            self.assertFalse(s.sup.processes.group_members(run.pending['observer'].pid));order.append('backend')
        run.launch=launch;run.stop_component=formal;run.stop_backend=Mock(side_effect=backend)
        run.receiver=Mock();run.receiver.stop.side_effect=lambda:order.append('receiver')
        with patch.object(s,'OUT',output),patch.object(s.a,'require_idle'),patch.object(s.a,'lock_available',return_value=True),patch.object(s,'check'):
            with self.assertRaisesRegex(RuntimeError,'owned cleanup failed'):run.execute()
        self.assertEqual(order,['receiver','backend'])
        result=s.pkg.read(output/'result.json');self.assertEqual(result['status'],'FAILED')
        self.assertIn('fixture formal stop failed',result['cleanup_error'])

    def test_auxiliary_normal_stop_and_receiver_precede_backend(self):
        output,run=self.fixture();order=[]
        run.stop_component=lambda label:order.append(label)
        run.stop_backend=Mock(side_effect=lambda:order.append('backend'))
        run.receiver=Mock();run.receiver.stop.side_effect=lambda:order.append('receiver')
        with patch.object(s,'OUT',output),patch.object(s.a,'require_idle'),patch.object(s.a,'lock_available',return_value=True),patch.object(s,'check'):
            run.execute()
        self.assertEqual(order,['gateway','observer','prometheus','alertmanager','receiver','backend'])
        self.assertEqual(s.pkg.read(output/'result.json')['status'],'PASS_TLS_OBSERVATION_SHORT')


def main():
    os.umask(0o077);output=s.BASE/'cpu-driver';output.mkdir(mode=0o700)
    sources={name:s.pkg.sha(ROOT/name) for name in s.SOURCES}
    for name in sources:
        target=output/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Driver))
    (output/'tests.log').write_text(log.getvalue())
    s.pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),
        failures=len(result.failures),sources=sources,models_started=0,http_started=0,gpu_started=0))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
