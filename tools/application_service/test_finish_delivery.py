"""Finish-stage failures must not become successful publication."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import finish_delivery as f


class FinishContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        package, output = root / "package", root / "execution"
        for directory in (root / "build", package / "previous",
                          package / "candidate", output):
            directory.mkdir(parents=True)
        for name in f.s.NAMES:
            (package / "previous" / name).write_bytes(("old-" + name).encode())
            (package / "candidate" / name).write_bytes(("new-" + name).encode())
            (root / "build" / name).write_bytes(("old-" + name).encode())
        self.root, self.package, self.output = root, package, output
        self.plan = dict(old={n: f.h.sha(package / "previous" / n) for n in f.s.NAMES},
                         new={n: f.h.sha(package / "candidate" / n) for n in f.s.NAMES},
                         original_started=time.time(), source_commit="test")
        patches = [patch.object(f, "ROOT", root), patch.object(f, "PACKAGE", package),
                   patch.object(f, "OUTPUT", output), patch.object(f, "MARKER",
                       root / "build/recovery.json"),
                   patch.object(f, "check", return_value=self.plan),
                   patch.object(f.h, "require_guard"),
                   patch.object(f.h, "verify_pair"),
                   patch.object(f.s.acceptance, "install_interrupt_handlers")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_failed_monitor_before_install_aborts_without_write(self):
        with patch.object(f.s, "gpu_query", return_value=dict(status="ERROR", pids=None)):
            with self.assertRaises(f.s.EnvironmentAbort):
                f.execute()
        f.s.verify_hashes(self.root / "build", self.plan["old"])
        self.assertFalse(f.MARKER.exists())
        self.assertEqual(f.h.read(self.output / "execution.json")["status"], "ENV_ABORT")

    def test_failed_monitor_after_install_restores_both_files(self):
        with patch.object(f.s, "gpu_query", side_effect=[dict(status="OK", pids=[]),
                         dict(status="ERROR", pids=None)]):
            with self.assertRaises(f.s.EnvironmentAbort):
                f.execute()
        f.s.verify_hashes(self.root / "build", self.plan["old"])
        self.assertFalse(f.MARKER.exists())
        self.assertEqual(f.h.read(self.output / "execution.json")["rollback"], "RESTORED")

    def test_guard_required_to_finalize(self):
        with patch.object(f.s, "gpu_query", return_value=dict(status="OK", pids=[])):
            f.execute()
        f.s.verify_hashes(self.root / "build", self.plan["new"])
        (self.output / "lease").mkdir()
        f.h.save(self.output / "lease/exit.json", dict(status="ENV_ABORT"))
        with self.assertRaises(RuntimeError):
            f.finalize()
        self.assertTrue(f.MARKER.exists())
        self.assertFalse((self.output / "finalized.json").exists())


if __name__ == "__main__":
    unittest.main()
