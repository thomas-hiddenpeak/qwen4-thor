"""Bounded driver failures and owned cleanup without model, sockets or GPU."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True; sys.path.insert(0,str(ROOT))
from tools.project_tasks import protocol_stage as s, migration_worker, test_errors, transport, test_tasks
from tools.serve_error_params import stage as children_source


class Driver(unittest.TestCase):
    def fixture(self):
        parent=s.BASE/"driver-fixtures"; parent.mkdir(exist_ok=True)
        output=Path(tempfile.mkdtemp(dir=parent)); run=s.Run({})
        run.business=Mock()
        return output,run

    def test_unleased_entry_refuses_before_any_side_effect(self):
        output,run=self.fixture()
        with patch.object(s,"OUT",output),patch.object(s.h,"require_guard",side_effect=RuntimeError("wrong lease")),patch.object(s.s.a,"require_idle") as idle:
            with self.assertRaisesRegex(RuntimeError,"wrong lease"): run.execute()
        idle.assert_not_called(); run.business.assert_not_called(); self.assertFalse((output/"begin.json").exists())

    def test_busy_gpu_preserves_abort_and_never_launches(self):
        output,run=self.fixture()
        with patch.object(s,"OUT",output),patch.object(s.h,"require_guard"),patch.object(s.s.a,"require_idle",side_effect=[RuntimeError("ENV_ABORT: busy"),None]):
            with self.assertRaisesRegex(RuntimeError,"ENV_ABORT"): run.execute()
        run.business.assert_not_called(); self.assertEqual(s.pkg.read(output/"result.json")["status"],"ENV_ABORT")

    def test_identity_capture_failure_reaps_real_child_and_continues_cleanup(self):
        output,run=self.fixture(); children=s.Children(output/"lease"); older=Mock(); bad_socket=Mock()
        bad_socket.close.side_effect=RuntimeError("fixture close failure")
        run.services=[SimpleNamespace(sockets=[],children=older),SimpleNamespace(sockets=[bad_socket],children=children)]
        def business():
            with patch.object(children_source.child.custody,"proc",side_effect=RuntimeError("fixture identity failure")):
                children.launch(output/"child",[sys.executable,"-B","-c","import time;time.sleep(30)"])
        run.business=business
        with patch.object(s,"OUT",output),patch.object(s.h,"require_guard"),patch.object(s.s.a,"require_idle"):
            with self.assertRaisesRegex(RuntimeError,"own cleanup"): run.execute()
        older.cleanup.assert_called_once(); self.assertEqual(children.children,[])
        self.assertEqual(s.pkg.read(output/"result.json")["status"],"FAILED")

    def test_worker_pauses_only_after_committed_tool_result(self):
        output,_=self.fixture(); hold=Mock(); ledger=SimpleNamespace(meta={"task_id":"a"*64})
        hook=migration_worker.boundary(output,hold)
        for event in ("request","response","plan","tool_started","tool_returned"): hook(event,1,ledger)
        self.assertFalse((output/"boundary.json").exists()); hold.assert_not_called()
        hook("tool_result",1,ledger); hold.assert_called_once()
        self.assertEqual(s.pkg.read(output/"boundary.json")["kind"],"tool_result")

    def test_real_11g_profile_and_ca_must_be_inside_historical_root(self):
        output,_=self.fixture(); runtime=output/"historical"; s.history.extract_runtime(6,runtime)
        private=runtime/".q4t-work/private"; private.mkdir(mode=0o700,parents=True)
        ca=private/"ca.pem"; ca.write_bytes(test_errors.CA.read_bytes())
        value={k:v for k,v in test_tasks.BINDING.items() if k not in ("ca_sha256","key_sha256")}
        value.update(api_key="cpu-profile-boundary-"+"x"*40,ca_file=str(ca))
        profile=private/"profile.json"; s.pkg.save(profile,value)
        expected=transport.profile(profile)[1]
        script="from tools.project_tasks import transport;import json,sys;print(json.dumps(transport.profile(sys.argv[1])[1]))"
        child=subprocess.run([sys.executable,"-B","-c",script,str(profile)],cwd=runtime,capture_output=True,text=True,timeout=5)
        self.assertEqual(child.returncode,0,child.stderr); self.assertEqual(json.loads(child.stdout),expected)
        external=output/"outside.json"; s.pkg.save(external,value)
        child=subprocess.run([sys.executable,"-B","-c",script,str(external)],cwd=runtime,capture_output=True,text=True,timeout=5)
        self.assertNotEqual(child.returncode,0); self.assertIn("use build or .q4t-work",child.stderr)


def main():
    os.umask(0o077); output=s.BASE/"cpu-driver-v2"; output.mkdir(mode=0o700)
    sources={name:s.pkg.sha(ROOT/name) for name in s.DRIVER_SOURCES}
    for name in sources:
        path=output/"source"/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes((ROOT/name).read_bytes())
    log=io.StringIO(); result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Driver))
    (output/"tests.log").write_text(log.getvalue())
    s.pkg.save(output/"result.json",dict(passed=result.wasSuccessful(),tests_run=result.testsRun,errors=len(result.errors),failures=len(result.failures),
        sources=sources,models_started=0,listeners_started=0,gpu_started=0))
    print(log.getvalue(),end=""); return 0 if result.wasSuccessful() else 1


if __name__=="__main__": raise SystemExit(main())
