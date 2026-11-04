"""Owned application cleanup precedes gateway/backend shutdown, without HTTP."""
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
from tools.project_tasks import trust_stage as s


class Driver(unittest.TestCase):
    def fixture(self):
        parent=s.BASE/'driver-fixtures';parent.mkdir(exist_ok=True);root=Path(tempfile.mkdtemp(dir=parent));run=object.__new__(s.Run)
        run.started=time.monotonic();run.gpu_started=run.gpu_finished=None;run.fronts={};run.logs=[];run.pending={};run.children={}
        run.run_dir=root/'not-started';run.counts=dict(controls=0,generations=0,negatives=0);run.tls_probes=run.negative_tls=0
        run.launch=Mock();run.business=Mock();run.stop_backend=Mock();run.budget=Mock()
        return root/'execution',run

    def test_busy_preflight_preserves_env_abort(self):
        output,run=self.fixture()
        with patch.object(s,'OUT',output),patch.object(s.a,'require_idle',side_effect=RuntimeError('ENV_ABORT: busy')):
            with self.assertRaisesRegex(RuntimeError,'ENV_ABORT'):run.execute()
        run.launch.assert_not_called();self.assertEqual(s.pkg.read(output/'result.json')['status'],'ENV_ABORT')

    def test_actual_unbound_application_is_reaped_before_gateway_and_backend(self):
        output,run=self.fixture();identify=s.sup.process_identity;order=[]
        def identity(pid):
            if pid!=os.getpid():raise RuntimeError('fixture application identity failure')
            return identify(pid)
        run.launch=lambda:run.spawn('app-alpha',[sys.executable,'-c','import time;time.sleep(30)'])
        def gateway(label):
            child=run.pending['app-alpha'];self.assertIsNotNone(child.poll());self.assertFalse(s.sup.processes.group_members(child.pid))
            self.assertTrue(child.stdout.closed);order.append(label)
        run.stop_component=gateway;run.stop_backend=Mock(side_effect=lambda:order.append('backend'))
        with patch.object(s,'OUT',output),patch.object(s.owned,'OUT',output),patch.object(s.sup,'process_identity',side_effect=identity), \
             patch.object(s.a,'require_idle'),patch.object(s.a,'lock_available',return_value=True),patch.object(s,'check'):
            with self.assertRaisesRegex(RuntimeError,'application identity failure'):run.execute()
        self.assertEqual(order,['gateway-1','gateway-2','backend']);self.assertEqual(s.pkg.read(output/'result.json')['status'],'FAILED')

    def test_formal_gateway_failure_still_reaps_before_backend(self):
        output,run=self.fixture()
        def launch():
            p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
            run.pending['gateway-1']=p;run.children['gateway-1']=dict(process=p,owner=None,log=None)
        run.launch=launch;run.stop_component=Mock(side_effect=RuntimeError('fixture stop failure'))
        def backend():self.assertIsNotNone(run.pending['gateway-1'].poll());self.assertFalse(s.sup.processes.group_members(run.pending['gateway-1'].pid))
        run.stop_backend=Mock(side_effect=backend)
        with patch.object(s,'OUT',output),patch.object(s.a,'require_idle'),patch.object(s.a,'lock_available',return_value=True),patch.object(s,'check'):
            with self.assertRaisesRegex(RuntimeError,'owned cleanup failed'):run.execute()
        run.stop_backend.assert_called_once();self.assertEqual(s.pkg.read(output/'result.json')['status'],'FAILED')

    def test_expected_saved_tool_boundary_is_the_only_worker_pause(self):
        from tools.project_tasks import migration_worker
        root=s.BASE/'driver-fixtures';path=Path(tempfile.mkdtemp(dir=root));hold=Mock()
        hook=migration_worker.boundary(path,hold);ledger=Mock();ledger.meta={'task_id':'a'*64}
        for kind in ('request','response','tool_started','finished'):hook(kind,1,ledger)
        self.assertFalse((path/'boundary.json').exists());hold.assert_not_called()
        hook('tool_result',1,ledger);hold.assert_called_once();self.assertEqual(s.pkg.read(path/'boundary.json')['kind'],'tool_result')


def main():
    os.umask(0o077);output=s.BASE/'cpu-driver';output.mkdir(mode=0o700);sources={n:s.pkg.sha(ROOT/n) for n in s.SOURCES}
    for name in sources:
        path=output/'source'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Driver))
    (output/'tests.log').write_text(log.getvalue());s.pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        errors=len(result.errors),failures=len(result.failures),sources=sources,models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
