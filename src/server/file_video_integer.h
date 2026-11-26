#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <compare>
#include <cstddef>
#include <cstdint>
#include <stdexcept>

namespace q4t::server::video_detail {
// Fixed storage for exact projective display arithmetic. Two signed 32-bit
// source matrices, bounded 8192-pixel 16.16 extents, inverse pixel centers
// and 16-bit bilinear weights fit the 512-bit accumulator.
class DisplayInteger {
 public:
  static constexpr size_t kWords = 8;

  DisplayInteger(__int128 value = 0) {
    negative_ = value < 0;
    auto magnitude = static_cast<unsigned __int128>(value);
    if (negative_) magnitude = 0 - magnitude;
    words_[0] = static_cast<uint64_t>(magnitude);
    words_[1] = static_cast<uint64_t>(magnitude >> 64);
  }
  bool IsZero() const { return Length() == 0; }
  bool IsNegative() const { return negative_; }
  const std::array<uint64_t, kWords>& Words() const { return words_; }
  bool operator==(const DisplayInteger&) const = default;
  std::strong_ordering operator<=>(const DisplayInteger& other) const {
    if (negative_ != other.negative_)
      return negative_ ? std::strong_ordering::less
                       : std::strong_ordering::greater;
    const auto order = CompareMagnitude(other);
    return negative_ ? 0 <=> order : order <=> 0;
  }
  DisplayInteger operator-() const {
    auto result = *this;
    if (!result.IsZero()) result.negative_ = !result.negative_;
    return result;
  }
  DisplayInteger operator+(const DisplayInteger& other) const {
    DisplayInteger result;
    if (negative_ == other.negative_) {
      unsigned __int128 carry = 0;
      for (size_t i = 0; i < kWords; ++i) {
        const auto value =
            carry + words_[i] + static_cast<unsigned __int128>(other.words_[i]);
        result.words_[i] = static_cast<uint64_t>(value);
        carry = value >> 64;
      }
      if (carry) Overflow();
      result.negative_ = negative_;
    } else {
      const bool left = CompareMagnitude(other) >= 0;
      const auto& larger = left ? *this : other;
      const auto& smaller = left ? other : *this;
      uint64_t borrow = 0;
      for (size_t i = 0; i < kWords; ++i) {
        const unsigned __int128 value = larger.words_[i];
        const auto sub =
            static_cast<unsigned __int128>(smaller.words_[i]) + borrow;
        result.words_[i] = static_cast<uint64_t>(value - sub);
        borrow = value < sub;
      }
      result.negative_ = larger.negative_ && !result.IsZero();
    }
    return result;
  }
  DisplayInteger operator-(const DisplayInteger& other) const {
    return *this + -other;
  }
  DisplayInteger operator*(const DisplayInteger& other) const {
    DisplayInteger result;
    const size_t left = Length(), right = other.Length();
    for (size_t i = 0; i < left; ++i) {
      unsigned __int128 carry = 0;
      for (size_t j = 0; j < right; ++j) {
        if (i + j >= kWords) {
          if ((words_[i] && other.words_[j]) || carry) Overflow();
          continue;
        }
        const auto value =
            static_cast<unsigned __int128>(words_[i]) * other.words_[j] +
            result.words_[i + j] + carry;
        result.words_[i + j] = static_cast<uint64_t>(value);
        carry = value >> 64;
      }
      if (carry) {
        if (i + right >= kWords) Overflow();
        result.words_[i + right] = static_cast<uint64_t>(carry);
      }
    }
    result.negative_ = (negative_ != other.negative_) && !result.IsZero();
    return result;
  }

  // The float is a search hint only. Exact products check and correct every
  // result; a bounded integer search handles any inaccurate floating hint.
  static uint32_t FloorRatio(const DisplayInteger& numerator,
                             const DisplayInteger& denominator,
                             uint32_t maximum) {
    if (numerator.IsNegative() || denominator.IsNegative() ||
        denominator.IsZero() ||
        numerator >= denominator * DisplayInteger(uint64_t(maximum) + 1))
      throw std::out_of_range("video display ratio outside bound");
    const auto hint = numerator.Approximate() / denominator.Approximate();
    auto quotient = static_cast<uint32_t>(
        std::clamp(hint, 0.L, static_cast<long double>(maximum)));
    for (int i = 0; i < 3; ++i) {
      const auto product = denominator * DisplayInteger(quotient);
      if (product > numerator) {
        if (!quotient) break;
        --quotient;
      } else if (product + denominator <= numerator) {
        if (quotient == maximum) break;
        ++quotient;
      } else {
        return quotient;
      }
    }
    uint32_t low = 0, high = maximum;
    while (low < high) {
      const auto middle = low + (uint64_t(high) - low + 1) / 2;
      if (denominator * DisplayInteger(middle) <= numerator)
        low = static_cast<uint32_t>(middle);
      else
        high = static_cast<uint32_t>(middle - 1);
    }
    return low;
  }

 private:
  size_t Length() const {
    size_t length = kWords;
    while (length && !words_[length - 1]) --length;
    return length;
  }
  int CompareMagnitude(const DisplayInteger& other) const {
    for (size_t i = kWords; i-- > 0;) {
      if (words_[i] != other.words_[i])
        return words_[i] < other.words_[i] ? -1 : 1;
    }
    return 0;
  }
  long double Approximate() const {
    long double result = 0;
    for (size_t i = Length(); i-- > 0;)
      result = std::ldexp(result, 64) + words_[i];
    return negative_ ? -result : result;
  }
  [[noreturn]] static void Overflow() {
    throw std::overflow_error("video display arithmetic overflow");
  }
  std::array<uint64_t, kWords> words_{};
  bool negative_ = false;
};
}  // namespace q4t::server::video_detail
