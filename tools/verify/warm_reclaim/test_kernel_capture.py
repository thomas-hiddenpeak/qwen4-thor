"""Host-only failure contracts; no tracefs or GPU mutation."""

import io
from pathlib import Path
import tempfile
import unittest

import kernel_capture as capture


class KernelCaptureTest(unittest.TestCase):
    def test_fields_include_pointer_and_array(self):
        value = ("field:struct shrinker * shr; offset:8;\n"
                 "field:char name[16]; offset:16;\n"
                 "field:unsigned int order; offset:32;\n")
        self.assertEqual(capture.format_fields(value), {"shr", "name", "order"})

    def test_required_event_missing_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(capture.CaptureFailure,
                                        "missing required event"):
                capture.discover_events(Path(directory))

    def test_required_field_missing_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = next(iter(capture.REQUIRED_EVENTS))
            path = root / "events" / name / "format"
            path.parent.mkdir(parents=True)
            path.write_text("field:int unrelated;")
            with self.assertRaisesRegex(capture.CaptureFailure, "missing fields"):
                capture.discover_events(root)

    def test_optional_absence_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, fields in capture.REQUIRED_EVENTS.items():
                path = root / "events" / name / "format"
                path.parent.mkdir(parents=True)
                path.write_text("\n".join("field:int " + f + ";" for f in fields))
            events, missing = capture.discover_events(root)
            self.assertEqual(events.keys(), capture.REQUIRED_EVENTS.keys())
            self.assertEqual(set(missing), set(capture.OPTIONAL_EVENTS))

    def test_loss_stats_preserved_and_aggregated(self):
        raw = ("entries: 0\noverrun: 3\ncommit overrun: 2\n"
               "dropped events: 1\nnow ts: 100.12030\n")
        stats = capture.parse_stats(raw)
        self.assertEqual(stats["now ts"], "100.12030")
        totals = capture.loss_totals({"cpu0": {"counters": stats},
                                      "cpu1": {"counters": stats}})
        self.assertEqual(totals, {"overrun": 6, "commit overrun": 4,
                                  "dropped events": 2})

    def test_missing_loss_counter_fails(self):
        with self.assertRaisesRegex(capture.CaptureFailure, "loss counters"):
            capture.parse_stats("entries: 0\noverrun: 0\n")

    def test_byte_budget_preserves_prefix_but_fails_explicitly(self):
        output = io.BytesIO()
        budget = capture.ByteBudget(5)
        budget.append(output, b"abc")
        with self.assertRaisesRegex(capture.CaptureFailure, "byte limit"):
            budget.append(output, b"defg")
        self.assertEqual(output.getvalue(), b"abcde")
        self.assertEqual(budget.unwritten_at_least, 2)

    def test_exact_byte_limit_is_not_success(self):
        budget = capture.ByteBudget(3)
        with self.assertRaises(capture.CaptureFailure):
            budget.append(io.BytesIO(), b"abc")


if __name__ == "__main__":
    unittest.main()
