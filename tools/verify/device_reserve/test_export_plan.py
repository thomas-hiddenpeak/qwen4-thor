"""Host contracts for refusing incomplete or incompatible reservation plans."""

import json
from pathlib import Path
import tempfile
import unittest

import export_plan as export


def record(sequence, call, phase, api="cudaMalloc", **changes):
    item = dict(seq=sequence, call=call, tid=42, api=api, phase=phase,
                bytes=16, ptr=0 if phase == "begin" else 4096,
                stream=0, flags=0, caller=256,
                rc=-1 if phase == "begin" else 0,
                mono=100 + sequence, backend="runtime")
    item.update(changes)
    return "[memory-trace] " + json.dumps(item)


READY = "[q4t] serving on port 8740 (host 127.0.0.1, model test)"


class ExportPlanTest(unittest.TestCase):
    def valid(self):
        return [record(0, 0, "begin"), record(1, 0, "end"), READY]

    def test_exact_startup_sequence(self):
        result = export.parse_trace("\n".join(self.valid()))
        self.assertEqual(result["startup_device_bytes"], 16)
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["startup_device_allocations"][0]["call"], 0)

    def test_all_post_ready_records_are_checked(self):
        lines = self.valid() + [record(2, 1, "begin"),
                                record(3, 1, "end", rc=2, ptr=0)]
        with self.assertRaisesRegex(ValueError, "failed CUDA"):
            export.parse_trace("\n".join(lines))

    def test_incomplete_at_ready_or_eof_rejected(self):
        for lines in ([record(0, 0, "begin"), READY],
                      self.valid() + [record(2, 1, "begin")]):
            with self.subTest(lines=lines):
                with self.assertRaisesRegex(ValueError, "unfinished"):
                    export.parse_trace("\n".join(lines))

    def test_startup_free_and_async_rejected(self):
        for api in ("cudaFree", "cudaFreeAsync", "cudaMallocAsync"):
            with self.subTest(api=api):
                with self.assertRaisesRegex(ValueError, "startup contains"):
                    export.parse_trace(record(0, 0, "begin", api=api) + "\n" + READY)

    def test_allocation_completion_order_rejected(self):
        lines = [record(0, 0, "begin"), record(1, 1, "begin"),
                 record(2, 1, "end"), record(3, 0, "end"), READY]
        with self.assertRaisesRegex(ValueError, "completion order"):
            export.parse_trace("\n".join(lines))

    def test_trace_gap_and_pair_mismatch_rejected(self):
        for ending in (record(2, 0, "end"), record(1, 0, "end", bytes=32)):
            with self.assertRaises(ValueError):
                export.parse_trace("\n".join([record(0, 0, "begin"), ending, READY]))

    def test_missing_or_duplicate_ready_rejected(self):
        for lines in (self.valid()[:-1], self.valid() + [READY]):
            with self.assertRaisesRegex(ValueError, "ready"):
                export.parse_trace("\n".join(lines))

    def test_duplicate_json_key_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            export.load_json('{"seq":0,"seq":1}')

    def test_nonfinite_timestamp_rejected(self):
        with self.assertRaisesRegex(ValueError, "nonfinite"):
            export.parse_trace(record(0, 0, "begin", mono=float("nan")))

    def test_sealed_input_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            manifest = {}
            for name in export.INPUTS:
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
                manifest[name] = export.sha256_bytes(path.read_bytes())
            (stage / "sealed-files.json").write_text(json.dumps(manifest))
            export.verified_inputs(stage)
            (stage / export.INPUTS[0]).write_text("changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                export.verified_inputs(stage)


if __name__ == "__main__":
    unittest.main()
