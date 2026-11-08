#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

#include "q4t/status.h"

namespace q4t::server {

// Exact finite decimal arithmetic for schema bounds/enum/multipleOf. The JSON
// spelling is the source of truth; binary64 rounding must not change
// membership.
class SchemaDecimal {
 public:
  static Status Parse(std::string_view text, SchemaDecimal* out);
  int Compare(const SchemaDecimal& other) const;
  Status MultipleOf(const SchemaDecimal& divisor, bool* result,
                    size_t* work_remaining) const;
  bool IsZero() const { return digits_ == "0"; }
  bool IsInteger() const { return IsZero() || exponent_ >= 0; }
  bool Negative() const { return negative_; }
  const std::string& Digits() const { return digits_; }
  int64_t Exponent() const { return exponent_; }
  std::string Canonical() const;
  Status Plain(std::string* out) const;

 private:
  std::string digits_ = "0";  // No leading/trailing zero except zero itself.
  int64_t exponent_ = 0;      // value = sign * digits * 10^exponent.
  bool negative_ = false;
};

}  // namespace q4t::server
