"""Exact BF16 dot reference, independent of GPU accumulation and Python floats.

The exact real dot and its nearest-even BF16 rounding are a reference, not a
requirement that FP32 implementations always return that rounding. Error bounds
and the executed reduction still need to be checked separately.
"""
from fractions import Fraction


def dyadic(bits):
    if type(bits) is not int or not 0 <= bits <= 65535:
        raise ValueError('BF16 bits out of range')
    exponent, fraction = (bits >> 7) & 255, bits & 127
    if exponent == 255:
        raise ValueError('nonfinite BF16')
    mantissa = fraction if exponent == 0 else 128 + fraction
    return (-mantissa if bits & 32768 else mantissa,
            -133 if exponent == 0 else exponent - 134)


def value(bits):
    mantissa, exponent = dyadic(bits)
    return Fraction(mantissa << max(exponent, 0), 1 << max(-exponent, 0))


def exact_dot(left, right):
    if not left or len(left) != len(right):
        raise ValueError('dot shape mismatch')
    # All finite BF16 products are integer multiples of 2**-266.
    total = 0
    absolute = 0
    for a, b in zip(left, right):
        am, ae = dyadic(a)
        bm, be = dyadic(b)
        term = (am * bm) << (ae + be + 266)
        total += term
        absolute += abs(term)
    return Fraction(total, 1 << 266), Fraction(absolute, 1 << 266)


def nearest_bf16(number):
    """Round an exact rational to finite BF16; fail on overflow to infinity."""
    number = Fraction(number)
    if number == 0:
        return 0
    sign = 32768 if number < 0 else 0
    magnitude = abs(number)
    maximum = value(0x7f7f)
    overflow_midpoint = maximum + (maximum - value(0x7f7e)) / 2
    if magnitude >= overflow_midpoint:
        raise ValueError('BF16 rounding overflows')
    if magnitude >= maximum:
        return sign | 0x7f7f
    low, high = 0, 0x7f7f
    while high - low > 1:
        middle = (low + high) // 2
        if value(middle) <= magnitude:
            low = middle
        else:
            high = middle
    dl, dh = magnitude - value(low), value(high) - magnitude
    chosen = low if dl < dh or (dl == dh and low % 2 == 0) else high
    return sign | chosen


def analyze_dot(left, right, observed, scale=Fraction(1)):
    exact, absolute = exact_dot(left, right)
    scale = Fraction(scale)
    exact *= scale
    absolute *= abs(scale)
    rounded = nearest_bf16(exact)
    def rational(number):
        return {'numerator': str(number.numerator),
                'denominator': str(number.denominator)}
    return dict(exact=rational(exact), sum_abs=rational(absolute),
                nearest_even_bf16=rounded,
                observed=[dict(bits=b, error=rational(value(b) - exact),
                               correctly_rounded=b == rounded)
                          for b in observed],
                interpretation='Exact real-dot reference only; differing from '
                'correct rounding does not alone prove an FP32 reduction defect.')
