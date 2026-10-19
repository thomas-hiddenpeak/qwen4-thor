from fractions import Fraction
import unittest
from fp32_reference import gemv_dot, power, round_fp32
from reference import exact_dot


class Fp32Test(unittest.TestCase):
    def test_ties_and_subnormals(self):
        self.assertEqual(round_fp32(1 + power(-24)), 1)
        self.assertEqual(round_fp32(1 + 3*power(-24)), 1 + power(-22))
        self.assertEqual(round_fp32(-1 - power(-24)), -1)
        self.assertEqual(round_fp32(power(-150)), 0)
        self.assertEqual(round_fp32(3*power(-150)), power(-148))
        with self.assertRaisesRegex(ValueError, 'overflow'):
            round_fp32(power(128))

    def test_exact_sum(self):
        self.assertEqual(gemv_dot([0x3f80]*256, [0x3f80]*256), 256)

    def test_reduction_is_not_real_dot(self):
        x = [0]*256
        x[0], x[2], x[4] = 0x4e80, 0x3f80, 0xce80  # 2**30, 1, -2**30
        w = [0x3f80]*256
        self.assertEqual(exact_dot(x, w)[0], Fraction(1))
        self.assertEqual(gemv_dot(x, w), Fraction(0))

    def test_bad_shape(self):
        with self.assertRaises(ValueError):
            gemv_dot([0], [0])


if __name__ == '__main__':
    unittest.main()
