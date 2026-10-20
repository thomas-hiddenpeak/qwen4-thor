"""Boundary tests for trace attribution and cleanup time clipping."""

import json
from pathlib import Path
import tempfile
import unittest

import analyze_kernel as analyze


class AnalyzeKernelTest(unittest.TestCase):
    def make_sealed_inputs(self, stage):
        manifest = {}
        for name in analyze.SOURCES:
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
            manifest[name] = analyze.sha256(path)
        (stage / "sealed-files.json").write_text(json.dumps(manifest))
        return manifest

    def test_sealed_inputs_all_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            manifest = self.make_sealed_inputs(stage)
            self.assertEqual(analyze.verify_sealed_inputs(stage)["inputs"], manifest)

    def test_changed_input_hash_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            self.make_sealed_inputs(stage)
            (stage / "kernel/trace.txt").write_text("changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                analyze.verify_sealed_inputs(stage)

    def test_unsealed_input_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            manifest = self.make_sealed_inputs(stage)
            del manifest["kernel/trace.txt"]
            (stage / "sealed-files.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "missing or invalid"):
                analyze.verify_sealed_inputs(stage)

    def test_duplicate_manifest_entry_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            json.loads('{"trace":"one","trace":"two"}',
                       object_pairs_hook=analyze.unique_keys)

    def test_output_in_stage_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "stage"
            stage.mkdir()
            with self.assertRaisesRegex(ValueError, "outside"):
                analyze.validate_output_path(stage, stage / "analysis.json")
            analyze.validate_output_path(stage, Path(directory) / "analysis.json")

    def test_output_symlink_into_stage_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir()
            alias = root / "alias"
            alias.symlink_to(stage, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "outside"):
                analyze.validate_output_path(stage, alias / "analysis.json")

    def test_comm_tgid_and_timestamp(self):
        line = (" worker name-42 (   40) [003] ..... 123.456789: "
                "mm_compaction_begin: zone_start=0x100000, mode=sync")
        item = analyze.parse_line(line, 7)
        self.assertEqual((item["comm"], item["pid"], item["tgid"], item["line"]),
                         ("worker name", 42, "40", 7))
        self.assertEqual(item["t"], 123.456789)

    def test_malformed_trace_is_not_ignored(self):
        with self.assertRaisesRegex(ValueError, "trace line 9"):
            analyze.parse_line("LOST 4 EVENTS", 9)

    def test_cleanup_clips_spanning_and_later_intervals(self):
        self.assertEqual(analyze.overlap(2, 8, 0, 5), 3)
        self.assertEqual(analyze.overlap(6, 8, 0, 5), 0)
        self.assertEqual(analyze.overlap(2, 4, 0, 5), 2)

    def test_incomplete_window_does_not_invent_api_return(self):
        window = analyze.make_window({"mono": 10, "api": "cudaMalloc"})
        window["observed_through_runner_sigterm"] = 12
        self.assertEqual(analyze.window_end(window), 12)
        self.assertNotIn("end", window["call"])

    def test_app_sequence_gap_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.log"
            path.write_text('[memory-trace] {"seq":1,"call":0}\n')
            with self.assertRaisesRegex(ValueError, "sequence gap"):
                analyze.read_application(path)

    def test_no_sigterm_means_boundary_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / "run").mkdir()
            (stage / "run/normal-process.json").write_text('{"signals":[]}')
            self.assertFalse(analyze.cleanup_boundary(stage)["available"])

    def test_wall_boundary_uses_recorded_clock_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / "run").mkdir()
            (stage / "kernel").mkdir()
            (stage / "run/normal-process.json").write_text(json.dumps({
                "signals": [{"signal": "SIGTERM", "t": 1005}]}))
            (stage / "state.json").write_text(json.dumps({
                "started": {"wall": 1000, "monotonic": 100}}))
            (stage / "kernel/timeline.jsonl").write_text(json.dumps({
                "kind": "marker", "text": "mono_ns=100000000000 "
                "realtime_ns=1000000000000"}) + "\n")
            self.assertEqual(analyze.cleanup_boundary(stage)["monotonic_approx"],
                             105)


if __name__ == "__main__":
    unittest.main()
