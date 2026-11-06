"""Offline diagnostic negative controls; no model, CUDA or private fixtures."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.vision_service import timing_analysis as a


def record(**changes):
    row = dict(version="1", id="request", clock="host_wall", completed="1",
               items="1", frames="1", patches="64", total_ms="14", other_ms="1")
    row.update({name + suffix: "1" for name in a.STAGES
                for suffix in ("_ms", "_calls")})
    row.update(changes)
    return a.PREFIX + " ".join(k + "=" + v for k, v in row.items())


class Timing(unittest.TestCase):
    def test_complete_partition(self):
        row = a.parse_records("unrelated log\n" + record())["request"]
        self.assertEqual(row["total_ms"], 14)

    def test_missing_duplicate_or_changed_fields(self):
        for value in (record().replace(" prepare_ms=1", ""),
                      record() + " prepare_ms=1", record(extra="1"),
                      record(version="2"), record(clock="gpu")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                a.parse_records(value)

    def test_invalid_durations_and_overlap(self):
        for value in (record(decode_jpeg_ms="nan"), record(forward_ms="inf"),
                      record(other_ms="-1"), record(total_ms="13"),
                      record(prepare_calls="0"), record(total_ms="0")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                a.parse_records(value)

    def test_duplicate_request_rejected(self):
        with self.assertRaises(ValueError):
            a.parse_records(record() + "\n" + record())

    def test_partial_is_retained_but_not_complete_sample(self):
        row = a.parse_records(record(completed="0"))["request"]
        with self.assertRaises(ValueError):
            a.validate_request(row, dict(request_id="request", messages=[]))

    def test_missing_stage_or_wrong_codec_rejected(self):
        body = dict(request_id="request", messages=[dict(content=[dict(
            type="image_url", image_url=dict(url="data:image/jpeg;base64,abc"))])])
        row = a.parse_records(record(decode_png_calls="0", decode_png_ms="0",
                                    cleanup_calls="3", other_ms="2"))["request"]
        self.assertEqual(a.validate_request(row, body)["jpeg"], 1)
        for key, value in (("forward_calls", 0), ("decode_jpeg_calls", 2),
                           ("items", 2), ("id", "wrong")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                a.validate_request(dict(row, **{key: value}), body)


if __name__ == "__main__":
    unittest.main()
