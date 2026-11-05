"""Offline rejection tests for request-bound acceptance evidence."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'gdn_stage_under_test', Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def request(index):
    return [f'[q4t][request] id=r{index} event=admitted ',
            f'[mtp-accept] request=r{index} draft_counts=1,0,2,3',
            f'[q4t][request] id=r{index} event=end released=1 outcome=success']


class EvidenceContract(unittest.TestCase):
    def setUp(self):
        self.lines = [line for i in range(17) for line in request(i)]

    def read(self):
        return stage.acceptance('\n'.join(self.lines))

    def test_complete_histograms(self):
        self.assertEqual(self.read(), {f'r{i}': (1, 0, 2, 3) for i in range(17)})

    def test_cross_request_histogram_rejected(self):
        self.lines[1] = self.lines[1].replace('request=r0', 'request=r1')
        with self.assertRaises(ValueError):
            self.read()

    def test_duplicate_histogram_rejected(self):
        self.lines.insert(2, self.lines[1])
        with self.assertRaises(ValueError):
            self.read()

    def test_overlapping_requests_rejected(self):
        self.lines.insert(1, request(18)[0])
        with self.assertRaises(ValueError):
            self.read()

    def test_missing_completion_rejected(self):
        self.lines.pop()
        with self.assertRaises(ValueError):
            self.read()

    def test_failed_or_unreleased_request_rejected(self):
        original = self.lines[2]
        for bad in ('released=0 outcome=success', 'released=1 outcome=error'):
            with self.subTest(bad=bad):
                self.lines[2] = original.replace('released=1 outcome=success', bad)
                with self.assertRaises(ValueError):
                    self.read()

    def test_histogram_shape_values_and_coverage(self):
        original = self.lines[1]
        for bad in ('1,2,3', '1,-1,2,3', '0,0,0,0'):
            with self.subTest(bad=bad):
                self.lines[1] = original.replace('1,0,2,3', bad)
                with self.assertRaises(ValueError):
                    self.read()
        self.lines = self.lines[3:]
        with self.assertRaises(ValueError):
            self.read()

    def test_invalid_decode_interval_and_count(self):
        rows = [dict(output_tokens=256, latency=8.0, ttft=1.0) for _ in range(3)]
        self.assertEqual(stage.metrics(rows)['aggregate_decode'], 255 / 7)
        for bad in (8.0, 9.0, float('nan'), float('inf')):
            with self.subTest(bad=bad):
                rows[0]['ttft'] = bad
                with self.assertRaises(ValueError):
                    stage.metrics(rows)
        with self.assertRaises(ValueError):
            stage.metrics(rows[1:])


if __name__ == '__main__':
    unittest.main()
