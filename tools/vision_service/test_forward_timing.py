"""Forward timing negative controls; no CUDA/model/private fixture access."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.vision_service import forward_timing_analysis as a


def record(**changes):
    row = dict(version="1", id="request", valid="1", clock="cuda_stream",
               items="1", patches="64", depth="1", events="24", total_ms="11",
               other_ms="1", setup_host_ms="2", read_host_ms="3",
               position_host_ms="4")
    row.update({s + suffix: "1" for s in a.STAGES
                for suffix in ("_ms", "_calls")})
    row["norm_calls"] = "2"
    row.update(changes)
    return a.PREFIX + " ".join(k + "=" + v for k, v in row.items())


class ForwardTiming(unittest.TestCase):
    def test_partition_does_not_add_host_clocks(self):
        row = a.parse_records(record())["request"]
        self.assertEqual(a.validate_request(row, dict(request_id="request"),
            dict(completed=1, items=1, patches=64), 1, 64)["total_ms"], 11)

    def test_missing_duplicate_and_unknown_fields(self):
        for value in (record().replace(" attention_ms=1", ""),
                      record() + " attention_ms=1", record(extra="1"),
                      record(version="2"), record(clock="host_wall"),
                      record(valid="2"), record() + "\n" + record()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                a.parse_records(value)

    def test_nonfinite_negative_and_bad_partition(self):
        for value in (record(attention_ms="nan"), record(mlp_ms="inf"),
                      record(other_ms="-1"), record(total_ms="10"),
                      record(setup_host_ms="nan"), record(events="8193"),
                      record(qkv_calls="0"), record(patches="0")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                a.parse_records(value)

    def test_missing_interval_geometry_or_identity_rejected(self):
        for changes in (dict(attention_calls="0", attention_ms="0", other_ms="2"),
                        dict(events="23"), dict(depth="2"), dict(items="2"),
                        dict(patches="65"), dict(id="wrong")):
            row = next(iter(a.parse_records(record(**changes)).values()))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                a.validate_request(row, dict(request_id="request"),
                    dict(completed=1, items=1, patches=64), 1, 64)

    def test_partial_is_preserved_but_rejected(self):
        row = a.parse_records(a.PREFIX + "version=1 id=request valid=0 "
                              "error=elapsed error_code=34 events=24")["request"]
        with self.assertRaises(ValueError):
            a.validate_request(row, dict(request_id="request"), {}, 1, 64)

    def test_multiphoto_counts(self):
        row = a.parse_records(record())["request"]
        row.update(items=2, patches=128, events=44)
        for stage in a.STAGES:
            if stage != "patch": row[stage + "_calls"] *= 2
        a.validate_request(row, dict(request_id="request"),
                           dict(completed=1, items=2, patches=128), 1, 128)


if __name__ == "__main__":
    unittest.main()
