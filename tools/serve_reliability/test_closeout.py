"""Reject vacuous publication probes and insufficient positive controls."""
import unittest
from closeout import validate_pair, validate_probe


class ProbeTests(unittest.TestCase):
    def test_short_and_mismatched_probes(self):
        saved = [10, 20, 30, 40]
        for invalid in [[10, 20, 30], [10, 21, 30, 40, 50]]:
            with self.assertRaises(AssertionError):
                validate_probe(saved, invalid)
        self.assertEqual(validate_probe(saved, [10, 20, 30, 99, 50]), 3)

    def test_positive_must_hit_actual_boundary(self):
        for positive in [0, 8192, 9229]:
            with self.assertRaises(AssertionError):
                validate_pair(9230, positive, 0)
        validate_pair(9230, 9230, 0)

    def test_wrong_publication_is_detected(self):
        with self.assertRaises(AssertionError):
            validate_pair(9230, 9230, 9230)


if __name__ == '__main__':
    unittest.main()
