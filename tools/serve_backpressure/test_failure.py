"""Targeted evidence repair after the first real payload failed; CPU only."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_backpressure import stage, verify


class Capture(unittest.TestCase):
    def test_proc_stat_discovers_child_without_children_file(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            proc = Path(directory)
            (proc / "meminfo").write_text("MemAvailable: 100 kB\n")
            for pid, parent in ((11, 1), (12, 11), (13, 99)):
                entry = proc / str(pid)
                entry.mkdir()
                fields = ["S", str(parent)]+["0"]*17+[str(pid*100)]
                (entry / "stat").write_text(str(pid)+" (name (with spaces)) "+" ".join(fields))
                (entry / "status").write_text(f"Pid:\t{pid}\nPPid:\t{parent}\nVmRSS:\t42 kB\n")
            result = verify.memory_snapshot(11, proc)
            self.assertEqual(set(result["processes"]), {"11", "12"})
            self.assertEqual(result["process_start_ticks"], {"11":1100, "12":1200})
            self.assertFalse(result["sampling_errors"])
            with self.assertRaises(ValueError): verify.memory_snapshot(99, proc)

    def test_actual_cpu_child_is_observed_and_reaped(self):
        # An idle CPU-only child exercises this host's procfs. No model/HTTP.
        child = subprocess.Popen(["/bin/cat"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            result = verify.memory_snapshot(os.getpid())
            self.assertIn(str(child.pid), result["processes"])
            self.assertIn("VmRSS:", result["processes"][str(child.pid)])
            self.assertEqual(result["child_discovery"], "proc_stat_ppid")
        finally:
            child.stdin.close()
            child.wait(timeout=3)
        self.assertEqual(child.returncode, 0)

    def test_only_matching_terminal_diagnostic_allows_read(self):
        line = "[q4t][request] instance=0 id=req.a event=end stage=response outcome=error reason=structured_output_incomplete"
        self.assertEqual(verify.ended_before_eagain(line, "req.a"), line)
        self.assertIsNone(verify.ended_before_eagain(line, "reqxa"))
        self.assertIsNone(verify.ended_before_eagain(line.replace("event=end", "event=stage"), "req.a"))
        self.assertIsNone(verify.ended_before_eagain(line.replace("id=req.a", "id=req.ab"), "req.a"))

    def test_complete_error_wire_is_evidence_not_acceptance(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            folder = Path(directory)
            wire = b'HTTP/1.1 200 OK\r\n\r\ndata: {"error":{"code":"structured_output_incomplete"}}\r\n\r\n'
            sock = Mock()
            sock.recv.side_effect = [wire[:30], wire[30:], b""]
            verify.preserve_ended_wire(sock, folder, "terminal diagnostic")
            self.assertEqual((folder / "long-ended-wire.bin").read_bytes(), wire)
            result = json.loads((folder / "ended-wire-capture.json").read_text())
            self.assertEqual(result, dict(bytes=len(wire), read_error=None, acceptance=False))

    def test_capture_timeout_keeps_prefix_without_overwriting(self):
        with tempfile.TemporaryDirectory(dir=stage.BASE) as directory:
            folder = Path(directory)
            sock = Mock()
            sock.recv.side_effect = [b"HTTP/1.1 200", TimeoutError("read expired")]
            verify.preserve_ended_wire(sock, folder, "terminal diagnostic")
            self.assertEqual((folder / "long-ended-wire.bin").read_bytes(), b"HTTP/1.1 200")
            result = json.loads((folder / "ended-wire-capture.json").read_text())
            self.assertIn("read expired", result["read_error"])
            self.assertFalse(result["acceptance"])
            with self.assertRaises(FileExistsError):
                verify.preserve_ended_wire(sock, folder, "second write")


if __name__ == "__main__":
    unittest.main()
