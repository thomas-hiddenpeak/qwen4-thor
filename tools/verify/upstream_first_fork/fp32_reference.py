"""Exact-rational emulation of the documented production GEMV reduction.

Each FMA/add is rounded to IEEE binary32 nearest-even. No host floating-point
sum is used. This models gemv.cu's two per-lane accumulators and shuffle tree;
comparison with captured GPU FP32 outputs is still required.
"""
from fractions import Fraction
from reference import value


def power(exponent):
    return Fraction(1 << exponent) if exponent >= 0 else Fraction(1, 1 << -exponent)


def round_fp32(number):
    number = Fraction(number)
    if not number:
        return Fraction(0)
    negative = number < 0
    n, d = abs(number.numerator), number.denominator
    exponent = n.bit_length() - d.bit_length()
    if Fraction(n, d) < power(exponent):
        exponent -= 1
    if exponent > 127:
        raise ValueError('FP32 overflow')
    quantum = max(exponent - 23, -149)
    numerator, denominator = n << max(-quantum, 0), d << max(quantum, 0)
    q, r = divmod(numerator, denominator)
    if 2*r > denominator or (2*r == denominator and q % 2):
        q += 1
    result = q * power(quantum)
    if result >= power(128):
        raise ValueError('FP32 overflow')
    return -result if negative else result


def gemv_dot(left, right):
    if not left or len(left) != len(right) or len(left) % 8:
        raise ValueError('GEMV shape')
    x, w = list(map(value, left)), list(map(value, right))
    lanes = []
    for lane in range(32):
        accumulator = [Fraction(0), Fraction(0)]
        for block in range(lane, len(x)//8, 32):
            for pair in range(4):
                for component in range(2):
                    i = block*8 + pair*2 + component
                    accumulator[component] = round_fp32(
                        accumulator[component] + x[i]*w[i])
        lanes.append(round_fp32(sum(accumulator)))
    for offset in [16, 8, 4, 2, 1]:
        previous = lanes[:]
        for lane in range(32-offset):
            lanes[lane] = round_fp32(previous[lane] + previous[lane+offset])
    return lanes[0]
