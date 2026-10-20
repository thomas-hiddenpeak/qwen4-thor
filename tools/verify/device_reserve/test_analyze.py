"""Host contracts for reserve trace attribution and censored operations."""

import json
import unittest

import analyze


def lines(rows):
    return ["[reserve-trace] " + json.dumps(dict(
        seq=index, tid=42, mono=100 + index, **row))
        for index, row in enumerate(rows)]


PLAN = dict(event="plan", count=1, total_bytes=16,
            plan_sha256="plan", source_sha256="source")
BEGIN = dict(event="alloc", phase="begin", api="cudaMalloc",
             index=0, bytes=16, ptr=0, rc=-1)
END = dict(event="alloc", phase="end", api="cudaMalloc",
           index=0, bytes=16, ptr=4096, rc=0)


class AnalyzeReserveTest(unittest.TestCase):
    def test_actual_alloc_pair_and_elapsed_time(self):
        result = analyze.parse_reserve(lines([PLAN, BEGIN, END]), [16], "plan", "source")
        self.assertEqual(result["operations"][0]["seconds"], 1)
        self.assertEqual(result["allocated_count"], 1)
        self.assertEqual(result["uncompleted_operations"], [])

    def test_open_alloc_is_preserved_without_return(self):
        result = analyze.parse_reserve(lines([PLAN, BEGIN]), [16], "plan", "source")
        self.assertEqual(len(result["uncompleted_operations"]), 1)
        self.assertEqual(result["operations"], [])
        self.assertEqual(result["allocated_count"], 0)

    def test_changed_plan_rejected(self):
        with self.assertRaisesRegex(ValueError, "identity differs"):
            analyze.parse_reserve(lines([PLAN]), [16], "wrong", "source")

    def test_gap_and_unknown_event_rejected(self):
        for values in (lines([PLAN])[0:1] + lines([BEGIN]),
                       lines([PLAN, {"event": "unexpected"}])):
            with self.assertRaises(ValueError):
                analyze.parse_reserve(values, [16], "plan", "source")

    def test_wrong_size_or_missing_begin_rejected(self):
        for values in ([PLAN, dict(BEGIN, bytes=32)], [PLAN, END]):
            with self.assertRaises(ValueError):
                analyze.parse_reserve(lines(values), [16], "plan", "source")

    def test_operation_lookup_never_crosses_gap(self):
        operations = [dict(begin=2, observed_end=3), dict(begin=5, observed_end=8)]
        self.assertIsNone(analyze.operation_at(operations, [2, 5], 4))
        self.assertIs(analyze.operation_at(operations, [2, 5], 6), operations[1])


if __name__ == "__main__":
    unittest.main()
