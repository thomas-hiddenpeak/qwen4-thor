#include "q4t/server/schema_decimal.h"

#include <algorithm>
#include <charconv>

namespace q4t::server {
namespace {
constexpr size_t kDigitsLimit = 16384;
constexpr int64_t kExponentLimit = 1000000;

Status Resource() {
  return Status::Fail("schema_resource_exhausted: exact decimal work limit");
}

void TrimLeading(std::string* value) {
  const auto first = value->find_first_not_of('0');
  if (first == value->npos)
    *value = "0";
  else if (first)
    value->erase(0, first);
}

int CompareDigits(std::string_view left, std::string_view right) {
  if (left.size() != right.size()) return left.size() < right.size() ? -1 : 1;
  const int cmp = left.compare(right);
  return cmp < 0 ? -1 : cmp > 0 ? 1 : 0;
}

// left >= right; decimal long division subtracts at most nine times per digit.
void Subtract(std::string* left, std::string_view right) {
  int borrow = 0;
  for (size_t offset = 0; offset < left->size(); ++offset) {
    const size_t i = left->size() - 1 - offset;
    int digit = (*left)[i] - '0' - borrow;
    if (offset < right.size()) digit -= right[right.size() - 1 - offset] - '0';
    borrow = digit < 0;
    (*left)[i] = static_cast<char>('0' + digit + (borrow ? 10 : 0));
  }
  TrimLeading(left);
}
}  // namespace

Status SchemaDecimal::Parse(std::string_view text, SchemaDecimal* out) {
  *out = {};
  if (text.size() > kDigitsLimit) return Resource();
  const auto invalid = [] { return Status::Fail("invalid decimal spelling"); };
  if (text.empty()) return invalid();
  SchemaDecimal value;
  size_t offset = 0;
  if (text[0] == '-') {
    value.negative_ = true;
    ++offset;
  }
  const auto digit = [&] {
    return offset < text.size() && text[offset] >= '0' && text[offset] <= '9';
  };
  if (!digit()) return invalid();
  const size_t begin = offset;
  if (text[offset++] != '0') {
    while (digit()) ++offset;
  } else if (digit()) {
    return invalid();
  }
  value.digits_ = text.substr(begin, offset - begin);
  if (offset < text.size() && text[offset] == '.') {
    const size_t fraction = ++offset;
    if (!digit()) return invalid();
    while (digit()) ++offset;
    value.digits_.append(text.substr(fraction, offset - fraction));
    value.exponent_ = -static_cast<int64_t>(offset - fraction);
  }
  if (offset < text.size() && (text[offset] == 'e' || text[offset] == 'E')) {
    ++offset;
    bool negative = false;
    if (offset < text.size() && (text[offset] == '+' || text[offset] == '-'))
      negative = text[offset++] == '-';
    if (!digit()) return invalid();
    int64_t exponent = 0;
    while (digit()) {
      exponent = exponent * 10 + text[offset++] - '0';
      if (exponent > kExponentLimit) return Resource();
    }
    value.exponent_ += negative ? -exponent : exponent;
  }
  if (offset != text.size()) return invalid();
  TrimLeading(&value.digits_);
  if (value.IsZero()) {
    value.negative_ = false;
    value.exponent_ = 0;
  } else {
    while (value.digits_.back() == '0') {
      value.digits_.pop_back();
      ++value.exponent_;
    }
  }
  *out = std::move(value);
  return Status();
}

int SchemaDecimal::Compare(const SchemaDecimal& other) const {
  if (negative_ != other.negative_) return negative_ ? -1 : 1;
  const int sign = negative_ ? -1 : 1;
  if (IsZero() || other.IsZero())
    return IsZero() == other.IsZero() ? 0 : (IsZero() ? -sign : sign);
  const int64_t order = static_cast<int64_t>(digits_.size()) + exponent_;
  const int64_t other_order =
      static_cast<int64_t>(other.digits_.size()) + other.exponent_;
  if (order != other_order) return order < other_order ? -sign : sign;
  for (size_t i = 0; i < std::max(digits_.size(), other.digits_.size()); ++i) {
    const char a = i < digits_.size() ? digits_[i] : '0';
    const char b = i < other.digits_.size() ? other.digits_[i] : '0';
    if (a != b) return a < b ? -sign : sign;
  }
  return 0;
}

Status SchemaDecimal::MultipleOf(const SchemaDecimal& divisor, bool* result,
                                 size_t* work_remaining) const {
  *result = false;
  if (divisor.IsZero() || divisor.negative_)
    return Status::Fail("multipleOf must be positive");
  if (IsZero()) {
    *result = true;
    return Status();
  }
  const int64_t zeros = exponent_ - divisor.exponent_;
  // Both coefficients have no trailing zero. A smaller exponent cannot be an
  // integer multiple: it would need a factor of ten in the numerator.
  if (zeros < 0) return Status();
  if (divisor.digits_ == "1") {
    *result = true;
    return Status();
  }
  const size_t count = digits_.size() + static_cast<size_t>(zeros);
  if (count > *work_remaining) return Resource();
  std::string remainder = "0";
  for (size_t i = 0; i < count; ++i) {
    if (remainder == "0") remainder.clear();
    remainder += i < digits_.size() ? digits_[i] : '0';
    const size_t charge = 1 + 20 * (remainder.size() + divisor.digits_.size());
    if (charge > *work_remaining) return Resource();
    *work_remaining -= charge;
    while (CompareDigits(remainder, divisor.digits_) >= 0)
      Subtract(&remainder, divisor.digits_);
  }
  *result = remainder == "0";
  return Status();
}

std::string SchemaDecimal::Canonical() const {
  return (negative_ ? "-" : "") + digits_ +
         (exponent_ ? "e" + std::to_string(exponent_) : "");
}

Status SchemaDecimal::Plain(std::string* out) const {
  out->clear();
  const int64_t point = static_cast<int64_t>(digits_.size()) + exponent_;
  const size_t size =
      point <= 0 ? 2 - point + digits_.size()
                 : std::max(static_cast<size_t>(point), digits_.size()) + 1;
  if (size > kDigitsLimit) return Resource();
  if (negative_) *out = '-';
  if (point <= 0) {
    *out += "0." + std::string(static_cast<size_t>(-point), '0') + digits_;
  } else if (static_cast<size_t>(point) >= digits_.size()) {
    *out +=
        digits_ + std::string(static_cast<size_t>(point) - digits_.size(), '0');
  } else {
    *out += digits_.substr(0, point) + '.' + digits_.substr(point);
  }
  return Status();
}
}  // namespace q4t::server
