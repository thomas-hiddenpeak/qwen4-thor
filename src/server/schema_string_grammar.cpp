#include "schema_string_grammar.h"

#include <algorithm>
#include <bit>
#include <stdexcept>

#include "custom_regex.h"
#include "q4t/server/custom_grammar.h"
#include "schema_idna.h"

namespace q4t::server {
namespace {
std::string Either(const std::vector<std::string>& branches) {
  std::string result = "(?:";
  for (size_t i = 0; i < branches.size(); ++i) {
    if (i) result += '|';
    result += branches[i];
  }
  return result + ')';
}
std::string Date() {
  const std::string leap =
      "(?:[0-9]{2}(?:0[48]|[2468][048]|[13579][26])|"
      "(?:[02468][048]|[13579][26])00)";
  return "(?:[0-9]{4}-(?:(?:01|03|05|07|08|10|12)-"
         "(?:0[1-9]|[12][0-9]|3[01])|(?:04|06|09|11)-"
         "(?:0[1-9]|[12][0-9]|30)|02-(?:0[1-9]|1[0-9]|2[0-8]))|" +
         leap + "-02-29)";
}
std::string Time() {
  return "(?:[01][0-9]|2[0-3]):[0-5][0-9]:(?:[0-5][0-9]|60)"
         "(?:\\.[0-9]+)?(?:[zZ]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])";
}
std::string Ipv4() {
  const std::string byte = "(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])";
  return "(?:" + byte + "\\.){3}" + byte;
}
std::string HexGroups(int count) {
  std::string result;
  for (int i = 0; i < count; ++i) {
    if (i) result += ':';
    result += "[0-9a-fA-F]{1,4}";
  }
  return result;
}
std::string Ipv6() {
  std::vector<std::string> branches{HexGroups(8), HexGroups(6) + ':' + Ipv4()};
  for (int left = 0; left < 8; ++left)
    branches.push_back(HexGroups(left) + "::" +
                       (left == 7
                            ? ""
                            : "(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0," +
                                  std::to_string(6 - left) + "})?"));
  // An IPv4 suffix represents two 16-bit groups; compression must omit at
  // least one of the other six groups.
  for (int left = 0; left < 6; ++left)
    branches.push_back(HexGroups(left) + "::(?:[0-9a-fA-F]{1,4}:){0," +
                       std::to_string(5 - left) + '}' + Ipv4());
  return Either(branches);
}
std::string Host() {
  const std::string label = "[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?";
  return label + "(?:\\." + label + ")*";
}
std::string Duration() {
  const std::string day =
      "(?:[0-9]+Y(?:[0-9]+M(?:[0-9]+D)?)?|[0-9]+M(?:[0-9]+D)?|[0-9]+D)";
  const std::string clock =
      "T(?:[0-9]+H(?:[0-9]+M(?:[0-9]+S)?)?|[0-9]+M(?:[0-9]+S)?|[0-9]+S)";
  return "P(?:[0-9]+W|" + day + "(?:" + clock + ")?|" + clock + ')';
}
std::string Email() {
  const std::string atom = "[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+";
  const auto local = "(?:" + atom + "(?:\\." + atom +
                     ")*|\"(?:[\\x20-\\x21\\x23-\\x5b\\x5d-\\x7e]|"
                     "\\\\[\\x20-\\x7e])*\")";
  const auto literal = "(?:" + Ipv4() + "|[iI][pP][vV]6:" + Ipv6() +
                       "|[a-zA-Z0-9-]*[a-zA-Z0-9]:"
                       "[\\x21-\\x5a\\x5e-\\x7e]+)";
  return local + '@' + "(?:" + Host() + "|\\[" + literal + "\\])";
}

// A pure length bound needs a counter, not one regex-NFA node per scalar.
// Power-of-two rules retain the exact language with logarithmic depth/size.
// Each leaf is one Unicode scalar; the existing encoder handles JSON escapes.
std::string LargeLength(int64_t minimum, int64_t maximum) {
  const auto power = [](int bit) { return "p" + std::to_string(bit); };
  const auto exact = [&](uint32_t count) {
    std::string result;
    for (int bit = 30; bit >= 0; --bit)
      if (count & (uint32_t{1} << bit)) result += power(bit) + ' ';
    return result.empty() ? "\"\"" : result;
  };
  std::string rules = "p0 ::= [\\u0000-\\ud7ff\\ue000-\\U0010ffff]\n";
  const auto largest =
      static_cast<uint32_t>(maximum == 2147483647 ? minimum : maximum);
  for (int bit = 1; bit < static_cast<int>(std::bit_width(largest)); ++bit)
    rules +=
        power(bit) + " ::= " + power(bit - 1) + ' ' + power(bit - 1) + '\n';
  std::string suffix;
  if (maximum == 2147483647) {
    suffix = "p0*";
  } else {
    auto remaining = static_cast<uint32_t>(maximum - minimum);
    suffix = "r" + std::to_string(remaining);
    while (remaining) {
      const int bit = static_cast<int>(std::bit_width(remaining)) - 1;
      const auto rest = remaining - (uint32_t{1} << bit);
      std::string below;
      for (int lower = bit - 1; lower >= 0; --lower)
        below += power(lower) + "? ";
      if (below.empty()) below = "\"\"";
      rules += "r" + std::to_string(remaining) + " ::= " + below + " | " +
               power(bit) + " r" + std::to_string(rest) + '\n';
      remaining = rest;
    }
    rules += "r0 ::= \"\"\n";
  }
  return EncodeJsonStringGrammar("root ::= " + exact(minimum) + ' ' + suffix +
                                 '\n' + rules);
}
std::string Format(std::string_view format) {
  if (format == "date") return Date();
  if (format == "time") return Time();
  if (format == "date-time") return Date() + "[tT]" + Time();
  if (format == "duration") return Duration();
  if (format == "hostname") return Host();
  if (format == "email") return Email();
  if (format == "ipv4") return Ipv4();
  if (format == "ipv6") return Ipv6();
  if (format == "uuid")
    return "[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}";
  throw std::runtime_error("invalid schema string format");
}
}  // namespace

std::string CompileSchemaStringSyntax(
    const std::vector<const io::Json*>& terms) {
  int64_t minimum = 0, maximum = 2147483647;
  std::vector<std::string> patterns;
  for (const auto* term : terms) {
    minimum = std::max(minimum, term->GetInt("minLength", 0));
    maximum = std::min(maximum, term->GetInt("maxLength", 2147483647));
    if (const auto* pattern = term->Find("pattern"))
      patterns.push_back("(?s:.*)(?:" + pattern->str + ")(?s:.*)");
    if (const auto* format = term->Find("format")) {
      if ((format->str == "hostname" || format->str == "email") &&
          !CheckSchemaIdnaLabel("xn--bcher-kva"))
        throw SchemaDependencyUnavailable("IDNA2008 backend self-check failed");
      patterns.push_back(Format(format->str));
    }
  }
  if (minimum > maximum) return "root ::= [^\\u0000-\\U0010ffff]\n";
  if (patterns.empty() &&
      (minimum > 1024 || (maximum > 1024 && maximum != 2147483647)))
    return LargeLength(minimum, maximum);
  // Unbounded .* imposes no extra constraint. Avoid multiplying a large
  // format automaton by an unnecessary length counter.
  if (minimum || maximum != 2147483647 || patterns.empty())
    patterns.push_back("(?s:.{" + std::to_string(minimum) + ',' +
                       (maximum == 2147483647 ? "" : std::to_string(maximum)) +
                       "})");
  std::sort(patterns.begin(), patterns.end());
  patterns.erase(std::unique(patterns.begin(), patterns.end()), patterns.end());
  return EncodeJsonStringGrammar(SchemaRegexEbnf(patterns));
}

}  // namespace q4t::server
