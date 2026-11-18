"""Ensure each frozen numeric gate independently rejects bad results."""
from array import array
import os
from pathlib import Path
import tempfile
import unittest

from .speaker_acceptance import pair


class SpeakerAcceptanceTests(unittest.TestCase):
    def compare(self, x, y, threshold):
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_TMP']) as folder:
            a, b = Path(folder) / 'a.f32', Path(folder) / 'b.f32'
            a.write_bytes(array('f', x).tobytes())
            b.write_bytes(array('f', y).tobytes())
            return pair(a, b, len(x), threshold)

    def test_exact_finite_nonzero_passes(self):
        self.assertTrue(self.compare([1, 2], [1, 2],
            dict(relative_l2=1e-4, cosine=0.99999999, max_abs=0.01))['passed'])

    def test_relative_error_alone_rejects(self):
        self.assertFalse(self.compare([1.1, 2.2], [1, 2],
            dict(relative_l2=0.01, cosine=0.9, max_abs=10))['passed'])

    def test_cosine_alone_rejects(self):
        self.assertFalse(self.compare([1, 0], [0, 1],
            dict(relative_l2=10, cosine=0.9, max_abs=10))['passed'])

    def test_absolute_error_alone_rejects(self):
        self.assertFalse(self.compare([1.01, 2.02], [1, 2],
            dict(relative_l2=0.1, cosine=0.9, max_abs=0.001))['passed'])

    def test_nonfinite_zero_and_partial_files_fail_closed(self):
        limits = dict(relative_l2=10, cosine=-1, max_abs=10)
        for x, y in (([float('nan')], [1]), ([1], [float('inf')]),
                     ([0], [1]), ([1, 2], [1])):
            with self.assertRaises(AssertionError):
                self.compare(x, y, limits)


if __name__ == '__main__':
    unittest.main()
