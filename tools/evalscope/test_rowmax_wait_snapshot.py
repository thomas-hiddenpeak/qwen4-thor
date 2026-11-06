"""No-CUDA checks for owned-process selection and bounded wait snapshots."""
import os
from pathlib import Path
import tempfile
import unittest

from tools.evalscope import rowmax_wait_snapshot as w


class WaitSnapshotTest(unittest.TestCase):
    def test_only_owned_query_is_selected(self):
        guard = dict(pid=10, uid=os.getuid())
        query = dict(pid=11, ppid=10, start_ticks=100, uid=os.getuid(),
                     argv=["python3", "-B", w.QUERY, "--worker", "a" * 32])
        self.assertTrue(w.is_query(query, guard))
        for changes in (dict(ppid=99), dict(uid=-1), dict(argv=["python3", w.QUERY]),
                        dict(argv=["python3", "-B", w.QUERY, "--worker", "not-a-token"])):
            self.assertFalse(w.is_query(dict(query, **changes), guard))

    def test_pid_reuse_and_two_capture_budget(self):
        tracker = w.Tracker()
        query = dict(pid=11, start_ticks=100)
        self.assertIsNone(tracker.due([query], 0))
        self.assertIsNone(tracker.due([query], 1.9))
        reused = dict(pid=11, start_ticks=200)
        self.assertIsNone(tracker.due([reused], 2))
        self.assertEqual(tracker.due([reused], 4)[1]["threshold_seconds"], 2)
        self.assertIsNone(tracker.due([reused], 5))
        self.assertEqual(tracker.due([reused], 6)[1]["threshold_seconds"], 4)
        self.assertIsNone(tracker.due([reused, dict(pid=12, start_ticks=201)], 20))

    def test_exited_first_slow_worker_is_not_replaced(self):
        tracker = w.Tracker()
        query = dict(pid=11, start_ticks=100)
        tracker.due([query], 0)
        self.assertIsNotNone(tracker.due([query], 2))
        tracker.due([], 3)
        for now in (4, 10):
            self.assertIsNone(tracker.due([dict(pid=12, start_ticks=200)], now))

    def test_snapshot_boundaries_errors_and_identity(self):
        directory = w.ROOT / ".q4t-work/vision-attention-rowmax-20261107/cpu/wait-tests"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            proc = Path(temporary)
            target = proc / "11"
            task = target / "task/11"
            task.mkdir(parents=True)
            fields = ["S", "10"] + ["0"] * 17 + ["100"]
            (target / "stat").write_text("11 (test) " + " ".join(fields))
            (target / "cmdline").write_bytes(b"owned\0")
            for name in ("stat", "status", "wchan", "stack"):
                (task / name).write_text("x" * (w.MAX_FILE_BYTES + 10) if name == "stack" else "ok")
            original = w.identity(proc, 11)
            captured = w.snapshot(proc, original)
            self.assertTrue(captured["stable_identity"])
            self.assertEqual(captured["threads"][0]["truncated"], ["stack"])
            self.assertEqual(len(captured["threads"][0]["files"]["stack"]), w.MAX_FILE_BYTES)
            self.assertIn("syscall", captured["threads"][0]["errors"])
            fields[19] = "101"
            (target / "stat").write_text("11 (test) " + " ".join(fields))
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                w.snapshot(proc, original)


if __name__ == "__main__":
    unittest.main()
