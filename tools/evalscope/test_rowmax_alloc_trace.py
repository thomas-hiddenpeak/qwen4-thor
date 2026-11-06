"""Ownership, partial setup and bounded capture using a fake tracefs tree."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tools.evalscope import rowmax_alloc_trace as t


class TraceOwnership(unittest.TestCase):
    def setUp(self):
        scratch = t.STAGE.parent / "cpu/trace-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.trace = self.root / "instances"
        self.trace.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.plan = dict(trace_nonce="a" * 16)
        self.pid = os.getpid()
        self.instance = self.trace / t.instance_name(self.plan, self.pid)
        self.foreign = self.trace / "other-session"
        self.foreign.mkdir()
        (self.foreign / "tracing_on").write_text("foreign")
        for replacement in (
            patch.object(t, "TRACE_ROOT", self.trace),
            patch.dict(os.environ, SUDO_UID=str(os.getuid()), SUDO_GID=str(os.getgid())),
            patch.object(t, "identity", return_value=dict(pid=self.pid, uid=os.getuid(),
                start_ticks=1, argv=[str(t.ROOT / "tools/evalscope/gpu_exclusive.py")])),
            patch.object(t, "descendants", return_value=[self.pid]),
            patch.object(t, "global_controls", return_value={"state": "unchanged"}),
            patch.object(t, "configure", side_effect=self.populate),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def populate(self, instance, _pids):
        values = {"tracing_on": "1", "trace_clock": "[mono]", "buffer_size_kb": "256",
            "buffer_total_size_kb": "256", "options/event-fork": "1", "set_event_pid": str(self.pid),
            t.EVENT + "/filter": t.FAILED_PAGE, t.EVENT + "/trigger": "stacktrace",
            t.EVENT + "/enable": "1", "trace": "abcde", "per_cpu/cpu0/stats": "overrun: 0\n"}
        for name, value in values.items():
            path = instance / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)

    def remove(self, path):
        self.assertEqual(path, self.instance)
        self.assertEqual((path / "tracing_on").read_text().strip(), "0")
        self.assertEqual((path / (t.EVENT + "/enable")).read_text().strip(), "0")
        # Real tracefs permits rmdir despite the virtual control files.
        shutil.rmtree(path)

    def collect(self):
        original = Path.rmdir
        def remove(path):
            if path == self.instance:
                # shutil.rmtree itself calls os.rmdir, not Path.rmdir.
                return self.remove(path)
            return original(path)
        with patch.object(Path, "rmdir", remove):
            return t.collect(self.plan, self.output, self.pid)

    def test_existing_instance_is_never_reused(self):
        self.instance.mkdir()
        marker = self.instance / "foreign"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            t.setup(self.plan, self.output, self.pid)
        self.assertEqual(marker.read_text(), "keep")
        self.assertFalse((self.output / "owner.json").exists())

    def test_capture_is_bounded_and_other_instance_unchanged(self):
        self.assertEqual(t.setup(self.plan, self.output, self.pid), "CONFIGURED")
        with patch.object(t, "MAX_TRACE_BYTES", 4):
            self.assertEqual(self.collect(), "CAPTURED_AND_REMOVED")
        self.assertEqual((self.output / "trace.txt").read_bytes(), b"abcd")
        self.assertTrue(json.loads((self.output / "collected.json").read_text())["truncated"])
        self.assertEqual((self.foreign / "tracing_on").read_text(), "foreign")

    def test_partial_setup_stops_then_can_be_collected(self):
        def failed(instance, pids):
            self.populate(instance, pids)
            raise OSError("injected setup failure")
        with patch.object(t, "configure", side_effect=failed):
            with self.assertRaisesRegex(OSError, "injected"):
                t.setup(self.plan, self.output, self.pid)
        self.assertEqual((self.instance / "tracing_on").read_text().strip(), "0")
        self.assertTrue((self.output / "owner.json").exists())
        self.assertEqual(self.collect(), "CAPTURED_AND_REMOVED")

    def test_foreign_owner_refused_before_trace_changes(self):
        t.setup(self.plan, self.output, self.pid)
        owner = self.output / "owner.json"
        value = json.loads(owner.read_text())
        value["nonce"] = "b" * 16
        owner.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, "foreign trace ownership"):
            t.collect(self.plan, self.output, self.pid)
        self.assertEqual((self.instance / "tracing_on").read_text(), "1")
        self.assertEqual((self.foreign / "tracing_on").read_text(), "foreign")

    def test_global_change_is_reported_without_restoring_foreign_state(self):
        t.setup(self.plan, self.output, self.pid)
        with patch.object(t, "global_controls", return_value={"state": "foreign change"}):
            self.collect()
        receipt = json.loads((self.output / "collected.json").read_text())
        self.assertFalse(receipt["global_unchanged"])
        self.assertEqual((self.foreign / "tracing_on").read_text(), "foreign")

    def test_enabled_trigger_readback_accepts_star_but_not_disabled(self):
        def trigger_mode(instance, pids):
            self.populate(instance, pids)
            (instance / (t.EVENT + "/enable")).write_text("1*\n")
        with patch.object(t, "configure", side_effect=trigger_mode):
            self.assertEqual(t.setup(self.plan, self.output, self.pid), "CONFIGURED")
        for value in ("0", "0*", "X", "", "10", "1**"):
            self.assertFalse(t.event_enabled(value))
        self.collect()


if __name__ == "__main__":
    unittest.main()
