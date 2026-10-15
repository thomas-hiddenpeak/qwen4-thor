// Independent a-priori BF16 projection envelope, derived from the unchanged
// mtp_projection_path_test.cpp contract. K is explicit (HC: 10240 or 320).
// This does not define a tolerance for normalization, sigmoid or SiLU.
#pragma once

#include <bit>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace q4t::test::hc_dot {
inline void Require(bool value, const char* message) {
  if (!value) throw std::runtime_error(message);
}
inline double Value(uint16_t bits) {
  return std::bit_cast<float>(uint32_t{bits} << 16);
}
inline bool FiniteNormalOrZero(uint16_t bits) {
  const uint16_t magnitude = bits & 0x7fff;
  return magnitude == 0 || (magnitude >= 0x0080 && magnitude < 0x7f80);
}

// Direct binary64 -> BF16 RNE, avoiding a binary32 double-rounding step.
inline uint16_t RoundBf16(double value) {
  Require(std::isfinite(value), "nonfinite BF16 rounding endpoint");
  const uint16_t sign = std::signbit(value) ? 0x8000 : 0;
  const double magnitude = std::abs(value);
  constexpr uint16_t kMax = 0x7f7f;
  if (magnitude > Value(kMax)) {
    const double threshold = std::ldexp(1.0, 128) - std::ldexp(1.0, 119);
    return sign | (magnitude >= threshold ? 0x7f80 : kMax);
  }
  int low = 0, high = kMax;
  while (low < high) {
    const int middle = low + (high - low) / 2;
    if (Value(static_cast<uint16_t>(middle)) < magnitude)
      low = middle + 1;
    else
      high = middle;
  }
  if (low == 0 || Value(static_cast<uint16_t>(low)) == magnitude)
    return sign | static_cast<uint16_t>(low);
  const double midpoint = (Value(static_cast<uint16_t>(low - 1)) +
                           Value(static_cast<uint16_t>(low))) *
                          0.5;
  if (magnitude < midpoint || (magnitude == midpoint && (low & 1))) --low;
  return sign | static_cast<uint16_t>(low);
}
inline double Up(double value) {
  return std::nextafter(value, std::numeric_limits<double>::infinity());
}
inline double GammaUpper(int operations, int precision) {
  const double nu = operations * std::ldexp(1.0, -precision);
  Require(nu < 1.0, "invalid analytic gamma domain");
  return Up(nu / (1.0 - nu));
}
struct Envelope {
  double reference = 0;
  double sum_abs_upper = 0;
  double fp32_error_upper = 0;
  double fp64_error_upper = 0;
  uint16_t lower = 0;
  uint16_t upper = 0;
};
inline Envelope DotEnvelope(const uint16_t* input, const uint16_t* weight,
                            int inner) {
  Require(inner == 10240 || inner == 320, "unfrozen HC projection dimension");
  Envelope result;
  for (int k = 0; k < inner; ++k) {
    Require(FiniteNormalOrZero(input[k]) && FiniteNormalOrZero(weight[k]),
            "unsupported nonfinite/subnormal BF16 operand");
    const double product = Value(input[k]) * Value(weight[k]);
    if (product == 0) continue;
    // Products have at most 16 significant bits. This grid precondition
    // excludes FP32 subnormal partial sums regardless of reduction order.
    Require(std::isfinite(product) && std::ilogb(std::abs(product)) >= -111,
            "unsupported product grid: possible FP32 underflow");
    result.reference += product;
    result.sum_abs_upper = Up(result.sum_abs_upper + std::abs(product));
  }
  if (result.sum_abs_upper == 0) return result;
  result.fp32_error_upper =
      Up(GammaUpper(inner + 2, 24) * result.sum_abs_upper);
  result.fp64_error_upper = Up(GammaUpper(inner, 53) * result.sum_abs_upper);
  const double error = Up(result.fp32_error_upper + result.fp64_error_upper);
  Require(std::isfinite(result.reference) &&
              result.sum_abs_upper + error <
                  static_cast<double>(std::numeric_limits<float>::max()),
          "unsupported projection envelope: possible FP32 overflow");
  result.lower = RoundBf16(std::nextafter(
      result.reference - error, -std::numeric_limits<double>::infinity()));
  result.upper = RoundBf16(Up(result.reference + error));
  Require(
      std::isfinite(Value(result.lower)) && std::isfinite(Value(result.upper)),
      "unsupported projection envelope: possible BF16 overflow");
  return result;
}
inline bool InEnvelope(uint16_t actual, const Envelope& expected) {
  const double value = Value(actual);
  return std::isfinite(value) && value >= Value(expected.lower) &&
         value <= Value(expected.upper);
}
}  // namespace q4t::test::hc_dot
