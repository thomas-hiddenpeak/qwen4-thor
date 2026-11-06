"""Phase safety checks, no model/GPU/HTTP."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.vision_service import stage as s


class Stage(unittest.TestCase):
    def test_bad_waitable_exit_does_not_skip_other_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            folders=[root/'first',root/'second']
            for folder in folders:folder.mkdir()
            called=[]
            def wait(process,folder,timeout):
                called.append(process)
                (folder/'waitable.json').write_text('{}')
                if process==1:raise RuntimeError('child exit')
            children=SimpleNamespace(children=list(zip((1,2),folders)),wait=wait)
            errors=[];s.wait_owned(children,errors)
            self.assertEqual(called,[1,2]);self.assertEqual(len(errors),1)

    def test_missing_waitable_stops_normal_reap(self):
        with tempfile.TemporaryDirectory() as directory:
            def wait(*_):raise TimeoutError('still running')
            children=SimpleNamespace(children=[(1,Path(directory))],wait=wait)
            with self.assertRaises(TimeoutError):s.wait_owned(children,[])

    def test_guard_required_before_gpu_work(self):
        with self.assertRaises(s.guard_tools.EnvironmentAbort):
            s.guard_tools.require_guard()

    def test_attempt_budget_cannot_silently_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            run=object.__new__(s.Run)
            run.lock=threading.Lock();run.started=time.monotonic()
            run.plan={"limits":dict(seconds=100,generations=2)}
            run.counts=dict(generations=0);run.output=Path(folder)
            run.monitor=run.heartbeat=None;run.failures=[]
            with patch.object(s.guard_tools,"require_monitor"):
                run.budget("generations");run.budget("generations")
                with self.assertRaises(RuntimeError):run.budget("generations")
            self.assertEqual(run.counts["generations"],2)
            self.assertEqual(len(list(run.output.iterdir())),2)

    def test_source_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/"fixture";file.write_text("original")
            plan=dict(files={str(file):s.sha(file)},model_file_metadata={})
            s.guard_tools.verify_files(plan)
            file.write_text("changed")
            with self.assertRaises(RuntimeError):s.guard_tools.verify_files(plan)

    def test_frozen_limits_checked_before_output_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/"plan.json").write_text(json.dumps(dict(
                limits=dict(s.LIMITS,starts=999),port=s.PORT)))
            with self.assertRaises(RuntimeError):s.Run(root)
            self.assertFalse((root/"execution").exists())


if __name__ == "__main__":unittest.main()
