#include "schema_number_grammar.h"

#include <charconv>
#include <optional>
#include <stdexcept>
#include <vector>

#include "custom_regex.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_decimal.h"

namespace q4t::server {
namespace {
constexpr auto kEmpty = "[a&&b]";
constexpr auto kPositive = "(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?";
constexpr auto kNonzero = "(?:[1-9][0-9]*(?:\\.[0-9]+)?|0\\.[0-9]*[1-9][0-9]*)";
std::string Union(const std::vector<std::string>& values) {
  std::string result = "(?:";
  bool first = true;
  for (const auto& value : values) {
    if (value == kEmpty) continue;
    if (!first) result += '|';
    first = false;
    result += value;
  }
  return first ? kEmpty : result + ')';
}
std::string Digits(int low, int high) {
  if (low > high) return kEmpty;
  if (low == high) return std::to_string(low);
  return '[' + std::to_string(low) + '-' + std::to_string(high) + ']';
}
std::string IntegerCompare(std::string_view bound, bool above) {
  std::string same_length = kEmpty;
  for (size_t i = bound.size(); i-- > 0;) {
    const int digit = bound[i] - '0';
    const auto alternative =
        above ? Digits(digit + 1, 9) : Digits(i ? 0 : 1, digit - 1);
    std::vector<std::string> branches;
    if (same_length != kEmpty)
      branches.push_back(std::string(1, bound[i]) + same_length);
    if (alternative != kEmpty)
      branches.push_back(alternative + "[0-9]{" +
                         std::to_string(bound.size() - i - 1) + '}');
    same_length = Union(branches);
  }
  std::vector<std::string> result{same_length};
  if (above) {
    result.push_back("[1-9][0-9]{" + std::to_string(bound.size()) + ",}");
  } else if (bound != "0") {
    result.push_back("0");
    if (bound.size() > 1)
      result.push_back("[1-9][0-9]{0," + std::to_string(bound.size() - 2) +
                       '}');
  }
  return Union(result);
}

// Compare the infinite decimal fraction with a finite normalized suffix.
// Missing/trailing digits are zero, so short strings and trailing zeroes retain
// exact numeric meaning. Factoring equal prefixes keeps expansion linear.
std::string FractionCompare(std::string_view bound, bool above,
                            bool inclusive) {
  if (bound.empty()) {
    if (above) return inclusive ? "[0-9]+" : "[0-9]*[1-9][0-9]*";
    return inclusive ? "0+" : kEmpty;
  }
  std::string result = above ? (inclusive ? "[0-9]*" : "[0-9]*[1-9][0-9]*")
                             : (inclusive ? "0*" : kEmpty);
  for (size_t i = bound.size(); i-- > 0;) {
    const int digit = bound[i] - '0';
    const auto alternative =
        above ? Digits(digit + 1, 9) : Digits(0, digit - 1);
    std::vector<std::string> branches;
    if (result != kEmpty) branches.push_back(std::string(1, bound[i]) + result);
    if (alternative != kEmpty) branches.push_back(alternative + "[0-9]*");
    if (!above && i > 0) branches.push_back("");
    result = Union(branches);
  }
  return result;
}

std::string PositiveCompare(const SchemaDecimal& bound, bool above,
                            bool inclusive) {
  std::string plain;
  auto status = bound.Plain(&plain);
  if (!status) throw std::length_error(status.message());
  if (plain.starts_with('-')) plain.erase(0, 1);
  const auto point = plain.find('.');
  const auto whole = plain.substr(0, point);
  const auto fraction = point == plain.npos ? "" : plain.substr(point + 1);
  std::vector<std::string> branches;
  const auto integers = IntegerCompare(whole, above);
  if (integers != kEmpty) branches.push_back(integers + "(?:\\.[0-9]+)?");
  const auto fractions = FractionCompare(fraction, above, inclusive);
  if (fractions != kEmpty) branches.push_back(whole + "\\." + fractions);
  const bool zero_fraction =
      above ? fraction.empty() && inclusive : !fraction.empty() || inclusive;
  if (zero_fraction) branches.push_back(whole);
  return Union(branches);
}

struct Bound {
  SchemaDecimal value;
  bool exclusive = false;
};
std::string Comparison(const Bound& bound, bool above) {
  if (above && bound.value.Negative())
    return Union({kPositive,
                  "-" + PositiveCompare(bound.value, false, !bound.exclusive)});
  if (!above && !bound.value.Negative())
    return Union({std::string("-") + kNonzero,
                  PositiveCompare(bound.value, false, !bound.exclusive)});
  return (bound.value.Negative() ? "-" : "") +
         PositiveCompare(bound.value, true, !bound.exclusive);
}
}  // namespace

std::string SchemaNumberGrammar(const io::Json& schema, bool integer) {
  std::optional<Bound> lower, upper;
  for (const auto* key :
       {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}) {
    const auto* value = schema.Find(key);
    if (!value) continue;
    Bound parsed;
    if (!value->IsNumber() ||
        !SchemaDecimal::Parse(OutputJson(*value), &parsed.value))
      throw std::runtime_error("invalid schema number bound");
    const std::string_view name(key);
    parsed.exclusive = name.starts_with("exclusive");
    const bool above = name == "minimum" || name == "exclusiveMinimum";
    auto& target = above ? lower : upper;
    const int compare = target ? parsed.value.Compare(target->value) : 0;
    if (!target || (above ? compare > 0 : compare < 0) ||
        (compare == 0 && parsed.exclusive))
      target = std::move(parsed);
  }
  std::vector<std::string> patterns{
      integer ? "-?(?:0|[1-9][0-9]*)" : "-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?"};
  if (lower) patterns.push_back(Comparison(*lower, true));
  if (upper) patterns.push_back(Comparison(*upper, false));
  uint32_t multiple = 0;
  int32_t scale = 0;
  if (const auto* value = schema.Find("multipleOf")) {
    SchemaDecimal step;
    const auto status = SchemaDecimal::Parse(OutputJson(*value), &step);
    if (!status || step.Negative() || step.IsZero())
      throw std::runtime_error("multipleOf must be positive");
    const auto& digits = step.Digits();
    const auto parsed =
        std::from_chars(digits.data(), digits.data() + digits.size(), multiple);
    if (parsed.ec != std::errc{} ||
        parsed.ptr != digits.data() + digits.size() || multiple > 4096 ||
        step.Exponent() < -16384 || step.Exponent() > 16384)
      throw std::length_error(
          "exact multiple automaton exceeds resource bound");
    scale = static_cast<int32_t>(-step.Exponent());
  }
  return SchemaRegexEbnf(patterns, multiple, scale);
}
}  // namespace q4t::server
