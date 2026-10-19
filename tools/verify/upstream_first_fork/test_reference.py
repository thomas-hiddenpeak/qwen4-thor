"""Exact arithmetic boundary controls, without model loading or GPU calls."""
from fractions import Fraction
import unittest

from reference import analyze_dot, exact_dot, nearest_bf16, value


class ReferenceTest(unittest.TestCase):
    def test_known_dot_and_cancellation(self):
        # [1, 2, -1] dot [2, -1, 1] = -1, absolute product sum = 5.
        exact, absolute = exact_dot([0x3f80, 0x4000, 0xbf80],
                                    [0x4000, 0xbf80, 0x3f80])
        self.assertEqual((exact, absolute), (Fraction(-1), Fraction(5)))
        self.assertEqual(nearest_bf16(exact), 0xbf80)
        # Cancellation retains a product far below the larger operands' ULP.
        exact, _ = exact_dot([0x7f00, 0x0001, 0xff00],
                             [0x3f80, 0x0001, 0x3f80])
        self.assertEqual(exact, Fraction(1, 1 << 266))

    def test_midpoints_even_and_negative(self):
        for a, b, result in [(0x3f80, 0x3f81, 0x3f80),
                              (0x3f81, 0x3f82, 0x3f82),
                              (0x007f, 0x0080, 0x0080)]:
            midpoint = (value(a) + value(b)) / 2
            self.assertEqual(nearest_bf16(midpoint), result)
            self.assertEqual(nearest_bf16(-midpoint), result | 0x8000)
            delta = (value(b) - value(a)) / 1024
            self.assertEqual(nearest_bf16(midpoint - delta), a)
            self.assertEqual(nearest_bf16(midpoint + delta), b)

    def test_subnormal_zero_and_overflow(self):
        self.assertEqual(nearest_bf16(Fraction(1, 1 << 134)), 0)
        self.assertEqual(nearest_bf16(-Fraction(1, 1 << 134)), 0x8000)
        self.assertEqual(nearest_bf16(Fraction(3, 1 << 134)), 2)
        with self.assertRaisesRegex(ValueError, 'overflows'):
            nearest_bf16(Fraction(1 << 128))

    def test_nonfinite_and_shape_rejected(self):
        for bits in [0x7f80, 0xff80, 0x7fc0, -1, True]:
            with self.assertRaises(ValueError):
                value(bits)
        for left, right in [([], []), ([0], [0, 1])]:
            with self.assertRaises(ValueError):
                exact_dot(left, right)

    def test_reference_does_not_impose_correct_rounding_contract(self):
        result = analyze_dot([0x3f80], [0x3f80], [0x3f80, 0x3f81])
        self.assertTrue(result['observed'][0]['correctly_rounded'])
        self.assertFalse(result['observed'][1]['correctly_rounded'])
        self.assertEqual(result['observed'][1]['error'],
                         {'numerator': '1', 'denominator': '128'})
        self.assertIn('does not alone prove', result['interpretation'])


if __name__ == '__main__':
    unittest.main()
