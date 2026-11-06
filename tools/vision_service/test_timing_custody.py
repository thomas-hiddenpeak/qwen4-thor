"""Real CPU children check repeated retirement; GPU acknowledgement is mocked.

This proves process ownership/reset behavior only, not GPU guard acceptance.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.vision_service import timing_stage as t
from tools.serve_cuda_quota_integration import verify as v


class Custody(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("Q4T_TEST_OUTPUT_DIR", t.BASE / "cpu-cleanup"))
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runner = t.Run.__new__(t.Run)
        self.runner.folder = self.root
        self.runner.output = self.root
        self.runner.children = t.s.Children(self.root / "lease")
        self.runner.model = None
        self.addCleanup(lambda: self.runner.children.cleanup())
        self.stack = [patch.object(v.custody, "complete_lines", return_value=[]),
                      patch.object(v.custody, "acknowledgement", return_value={
                          "CPU_test_only": True, "GPU_verified": False})]
        for item in self.stack:
            item.start()
            self.addCleanup(item.stop)

    def launch(self, mode):
        folder = self.root / ("model-" + mode)
        child = self.runner.children.launch(folder, [sys.executable, "-B", "-c",
            "import signal,sys; signal.signal(signal.SIGTERM,lambda *_:sys.exit(0)); "
            "print('ready',flush=True); signal.pause()"])
        self.runner.model = child
        until = time.monotonic() + 5
        while "ready" not in (folder / "stdout.log").read_text():
            self.assertTrue(time.monotonic() < until)
            time.sleep(.01)
        return child

    def test_repeated_real_child_retirement_uses_fresh_ledger(self):
        first_ledger = self.runner.children
        for mode in ("off", "on"):
            child = self.launch(mode)
            self.runner.retire_model(mode)
            self.assertIsNone(self.runner.model)
            self.assertEqual(self.runner.children.started, [])
            self.assertFalse(Path(f"/proc/{child.pid}").exists())
            result = json.loads((self.root / ("model-" + mode) / "reaped.json").read_text())
            self.assertEqual(result, dict(pid=child.pid, returncode=0))
        self.assertIsNot(first_ledger, self.runner.children)

    def test_old_reuse_reproduces_identity_mismatch(self):
        old_ledger = self.runner.children
        self.launch("off")
        self.runner.retire_model("off")
        self.runner.children = old_ledger
        self.launch("on")
        with self.assertRaisesRegex(RuntimeError, "retained process identity drift"):
            self.runner.retire_model("on")


if __name__ == "__main__":
    unittest.main()
