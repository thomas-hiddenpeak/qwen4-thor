"""TLS stage ownership, failed cleanup and retention capacity checks, no HTTP."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_gateway import tls_stage as s


class Driver(unittest.TestCase):
    def fixture(self):
        parent=s.BASE/'driver-fixtures';parent.mkdir(exist_ok=True)
        root=Path(tempfile.mkdtemp(dir=parent));run=object.__new__(s.Run)
        run.started=time.monotonic();run.gpu_started=run.gpu_finished=None
        run.fronts={};run.logs=[];run.pending={};run.run_dir=root/'not-started';run.counts={}
        run.launch=Mock();run.stop_backend=Mock()
        return root/'execution',run

    def test_retention_admits_backend_and_both_gateway_generations(self):
        parent=s.BASE/'driver-fixtures';parent.mkdir(exist_ok=True)
        root=Path(tempfile.mkdtemp(dir=parent))/'store'
        s.retained.store.init(root,**s.STORE_POLICY);fds=[]
        try:
            for kind in ('backend','gateway','gateway'):
                _,fd=s.retained.store.reserve(root,kind,{},s.sup.process_identity(os.getpid()));fds.append(fd)
            self.assertEqual(len(fds),3)
        finally:
            for fd in fds:os.close(fd)

    def test_gpu_busy_stops_before_launch_and_remains_env_abort(self):
        output,run=self.fixture()
        with patch.object(s,'OUT',output),patch.object(s.a,'require_idle',side_effect=RuntimeError('ENV_ABORT: busy')):
            with self.assertRaisesRegex(RuntimeError,'ENV_ABORT'):run.execute()
        run.launch.assert_not_called()
        self.assertEqual(s.pkg.read(output/'result.json')['status'],'ENV_ABORT')

    def test_unbound_spawn_is_cleaned_after_reservation_failure(self):
        output,run=self.fixture();child=[]
        def launch():
            p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
            child.append(p);run.pending['early']=p
            raise ValueError('fixture identity/reservation failure')
        run.launch=launch
        with patch.object(s,'OUT',output),patch.object(s.a,'require_idle'),patch.object(s.a,'lock_available',return_value=True),patch.object(s,'check'):
            with self.assertRaisesRegex(ValueError,'reservation failure'):run.execute()
        self.assertIsNotNone(child[0].poll());self.assertFalse(s.sup.processes.group_members(child[0].pid))
        self.assertEqual(s.pkg.read(output/'result.json')['status'],'FAILED')

    def test_formal_cleanup_failure_cannot_keep_pass_and_still_stops_backend(self):
        output,run=self.fixture();run.business=Mock();run.tls_probes=0
        run.fronts={1:dict(process=SimpleNamespace(poll=lambda:None,wait=lambda _:None))}
        run.front_cli=Mock(side_effect=ValueError('fixture cleanup failure'))
        with patch.object(s,'OUT',output),patch.object(s.time,'sleep'),patch.object(s.a,'require_idle'), \
             patch.object(s.a,'lock_available',return_value=True),patch.object(s,'check'):
            with self.assertRaisesRegex(RuntimeError,'formal cleanup failed'):run.execute()
        run.stop_backend.assert_called_once()
        result=s.pkg.read(output/'result.json');self.assertEqual(result['status'],'FAILED')
        self.assertIn('fixture cleanup failure',result['cleanup_error'])


def main():
    os.umask(0o077);output=s.BASE/'cpu-driver';output.mkdir(mode=0o700)
    sources={name:s.pkg.sha(ROOT/name) for name in s.SOURCES}
    for name in sources:
        dest=output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Driver))
    (output/'tests.log').write_text(log.getvalue())
    s.pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),
        failures=len(result.failures),sources=sources,models_started=0,http_started=0,gpu_started=0))
    print(log.getvalue(),end='');return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
