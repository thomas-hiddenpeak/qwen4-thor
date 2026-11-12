#include "q4t/server/custom_grammar.h"

#include <algorithm>
#include <charconv>
#include <cstdio>
#include <stdexcept>
#include <vector>

#include "custom_regex.h"
#include "q4t/server/custom_tools.h"
#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "schema_grammar.h"
#include "schema_idna.h"
#include "schema_string.h"
#include "schema_string_grammar.h"

#ifdef Q4T_ENABLE_XGRAMMAR
#include <xgrammar/grammar.h>
#endif

namespace q4t::server {
#ifdef Q4T_ENABLE_XGRAMMAR
namespace {
using Range = std::pair<int, int>;
constexpr size_t kExpansionLimit = 512 * 1024;

void Require(bool valid) {
  if (!valid) throw std::runtime_error("invalid serialized custom grammar");
}
void Bound(size_t size) {
  if (size > kExpansionLimit)
    throw std::length_error("custom grammar expansion exceeds bound");
}
std::string Literal(std::string_view value) {
  return '"' + detail::JsonEscape(std::string(value)) + '"';
}
const io::Json* Field(const io::Json& value, const std::string& name) {
  const auto* field = value.Find(name);
  Require(field != nullptr);
  return field;
}
int Integer(const io::Json& value) {
  const int64_t number = value.AsInt(-2147483648);
  Require(number > -2147483648 && number <= 2147483647);
  return static_cast<int>(number);
}
std::string Codepoint(int value, bool regex = false) {
  char text[16];
  if (value <= 0xffff)
    std::snprintf(text, sizeof(text), "\\u%04x", value);
  else if (regex)
    std::snprintf(text, sizeof(text), "\\u{%x}", value);
  else
    std::snprintf(text, sizeof(text), "\\U%08x", value);
  return text;
}
std::vector<Range> Merge(std::vector<Range> ranges) {
  std::sort(ranges.begin(), ranges.end());
  std::vector<Range> result;
  for (const auto& [low, high] : ranges) {
    Require(low >= 0 && high >= low && high <= 0x10ffff);
    if (!result.empty() && low <= result.back().second + 1)
      result.back().second = std::max(result.back().second, high);
    else
      result.emplace_back(low, high);
  }
  return result;
}
std::vector<Range> Without(const std::vector<Range>& ranges,
                           const std::vector<Range>& excluded) {
  std::vector<Range> result;
  for (const auto& [low, high] : ranges) {
    int begin = low;
    for (const auto& [left, right] : excluded) {
      if (right < begin) continue;
      if (left > high) break;
      if (left > begin) result.emplace_back(begin, left - 1);
      begin = std::max(begin, right + 1);
    }
    if (begin <= high) result.emplace_back(begin, high);
  }
  return result;
}
std::string RegexLiteral(std::string_view text) {
  std::string result;
  for (char c : text) {
    if (std::string_view("\\.^$|()[]{}*+?").find(c) != text.npos)
      result += '\\';
    result += c;
  }
  return result;
}
std::string CharacterClass(const std::vector<int>& data, bool repeat = false,
                           bool regex = false) {
  Require(!data.empty() && data.size() % 2 == 1);
  std::vector<Range> ranges;
  for (size_t i = 1; i < data.size(); i += 2)
    ranges.emplace_back(data[i], data[i + 1]);
  ranges = Merge(std::move(ranges));
  if (data[0]) ranges = Without({{0, 0x10ffff}}, ranges);
  const auto normal = Without(
      ranges, {{0, 31}, {34, 34}, {60, 60}, {92, 92}, {0xd800, 0xdfff}});
  std::vector<std::string> branches;
  std::string normal_class;
  if (!normal.empty()) {
    std::string text = "[";
    for (const auto& [low, high] : normal) {
      text += Codepoint(low, regex);
      if (low != high) text += '-' + Codepoint(high, regex);
    }
    normal_class = text + ']';
    if (!repeat) branches.push_back(normal_class);
  }
  for (int c = 0; c <= 92; ++c) {
    if (c > 31 && c != 34 && c != 60 && c != 92) continue;
    if (std::none_of(ranges.begin(), ranges.end(), [&](Range range) {
          return c >= range.first && c <= range.second;
        }))
      continue;
    auto encoded = EncodeCustomInput(std::string(1, static_cast<char>(c)));
    const auto text = encoded.substr(1, encoded.size() - 2);
    branches.push_back(regex ? RegexLiteral(text) : Literal(text));
  }
  if (branches.empty())
    return repeat ? (normal_class.empty() ? "\"\"" : normal_class + '*')
                  : "[^\\u0000-\\U0010ffff]";
  std::string output = "(";
  for (const auto& branch : branches) {
    if (output.size() > 1) output += regex ? "|" : " | ";
    output += branch;
  }
  output += ')';
  if (!repeat) return output;
  // Keep runs of ordinary Unicode as a native character-class star. Repeating
  // the whole (normal | JSON-escape) choice creates a matcher stack frame per
  // ordinary character; real-vocabulary masks then exceed the step deadline.
  // Factoring normal* (escape normal*)* preserves the encoded language.
  if (normal_class.empty()) return output + '*';
  return normal_class + "* (" + output + ' ' + normal_class + "*)*";
}

// The schema regular-language emitter writes terminal counted chains in this
// canonical form. Preserve them before FromEBNF normalizes a repeated encoded
// (ordinary scalar | JSON escape) into recursive rules. A Regex macro compiles
// the same encoded language as one finite-state machine, including escapes.
bool CountedPattern(std::string_view body, std::string* encoded) {
  if (!body.starts_with('[') || !body.ends_with('}')) return false;
  size_t at = 1;
  const auto codepoint = [&](int* result) {
    if (at + 2 > body.size() || body[at] != '\\') return false;
    const auto kind = body[at + 1];
    const size_t digits = kind == 'u' ? 4 : kind == 'U' ? 8 : 0;
    if (!digits || at + 2 + digits > body.size()) return false;
    const auto parsed = std::from_chars(body.data() + at + 2,
                                        body.data() + at + 2 + digits,
                                        *result, 16);
    if (parsed.ec != std::errc() ||
        parsed.ptr != body.data() + at + 2 + digits || *result > 0x10ffff)
      return false;
    at += 2 + digits;
    return true;
  };
  std::vector<int> ranges{0};
  while (at < body.size() && body[at] != ']') {
    int low, high;
    if (!codepoint(&low)) return false;
    high = low;
    if (at < body.size() && body[at] == '-') {
      ++at;
      if (!codepoint(&high) || high < low) return false;
    }
    ranges.push_back(low);
    ranges.push_back(high);
  }
  if (ranges.size() == 1 || body.substr(at, 2) != "]{") return false;
  at += 2;
  int lower = 0, upper = 0;
  const auto first = std::from_chars(body.data() + at,
                                    body.data() + body.size(), lower);
  if (first.ec != std::errc() || first.ptr == body.data() + body.size() ||
      *first.ptr != ',' || lower < 0)
    return false;
  const auto last = std::from_chars(first.ptr + 1,
                                   body.data() + body.size() - 1, upper);
  if (last.ec != std::errc() || last.ptr != body.data() + body.size() - 1 ||
      upper < lower)
    return false;
  *encoded = "Regex(" + Literal(CharacterClass(ranges, false, true) + '{' +
                                std::to_string(lower) + ',' +
                                std::to_string(upper) + '}') + ')';
  return true;
}

std::string PreserveCountedRules(
    std::string_view source,
    std::vector<std::pair<std::string, std::string>>* encoded) {
  Bound(source.size());
  std::string result;
  for (size_t begin = 0; begin < source.size();) {
    const auto end = source.find('\n', begin);
    const auto line = source.substr(begin, end == source.npos
                                              ? source.size() - begin
                                              : end - begin);
    const auto separator = line.find(" ::= ");
    std::string pattern;
    if (separator != line.npos &&
        CountedPattern(line.substr(separator + 5), &pattern)) {
      std::string marker = "__q4t_counted_json_" +
                           std::to_string(encoded->size()) + "__";
      while (source.find(marker) != source.npos) marker += '_';
      encoded->emplace_back(marker, std::move(pattern));
      result += std::string(line.substr(0, separator + 5)) + Literal(marker);
    } else {
      result += line;
    }
    result += '\n';
    Bound(result.size());
    if (end == source.npos) break;
    begin = end + 1;
  }
  return result;
}

// Public SerializeJSON exposes the pinned grammar expression representation.
// Read it defensively and generate independent EBNF rules; no private library
// headers or in-place AST mutation. A dependency upgrade must requalify this
// representation and the language-preserving encoding tests.
class Encoder {
 public:
  explicit Encoder(
      std::vector<std::pair<std::string, std::string>> regexes = {},
      std::vector<std::pair<std::string, std::string>> encoded = {})
      : regexes_(std::move(regexes)), encoded_(std::move(encoded)) {}
  std::string Encode(const xgrammar::Grammar& grammar) {
    const auto root = Add(grammar, 0);
    return "root ::= " + Literal("\"") + ' ' + root + ' ' + Literal("\"") +
           '\n' + rules_;
  }

 private:
  std::string Add(const xgrammar::Grammar& grammar, int depth) {
    if (depth > 16) throw std::length_error("custom grammar nesting bound");
    const auto serialized = grammar.SerializeJSON();
    Bound(serialized.size());
    io::Json object;
    Require(io::ParseJson(serialized, &object, true,
                          {32, 262144, kExpansionLimit, true})
                .ok());
    const auto& rules = *Field(object, "rules");
    const auto& pointers = *Field(object, "grammar_expr_indptr");
    const auto& data = *Field(object, "grammar_expr_data");
    Require(rules.IsArray() && pointers.IsArray() && data.IsArray());
    if (rules.array.size() > 8192 || pointers.array.size() > 65536)
      throw std::length_error("custom grammar node bound");
    const auto prefix = "q4t_g" + std::to_string(next_++) + '_';
    const auto rule = [&](int id) {
      Require(id >= 0 && static_cast<size_t>(id) < rules.array.size());
      return prefix + 'r' + std::to_string(id);
    };
    const auto expr = [&](int id) {
      Require(id >= 0 && static_cast<size_t>(id) < pointers.array.size());
      return prefix + 'e' + std::to_string(id);
    };
    for (size_t i = 0; i < pointers.array.size(); ++i) {
      const int offset = Integer(pointers.array[i]);
      Require(offset >= 0 &&
              static_cast<size_t>(offset) + 2 <= data.array.size());
      const int kind = Integer(data.array[offset]);
      const int count = Integer(data.array[offset + 1]);
      Require(count >= 0 &&
              static_cast<size_t>(offset) + 2 + count <= data.array.size());
      std::vector<int> values;
      for (int j = 0; j < count; ++j)
        values.push_back(Integer(data.array[offset + 2 + j]));
      std::string body;
      switch (kind) {
        case 0: {  // ByteString.
          std::string raw;
          for (const auto byte : values) {
            Require(byte >= 0 && byte <= 255);
            raw.push_back(static_cast<char>(byte));
          }
          size_t offset = 0;
          do {
            size_t next = raw.size();
            const std::pair<std::string, std::string>* match = nullptr;
            bool already_encoded = false;
            for (const auto& replacement : regexes_) {
              if (depth != 0) break;
              const auto at = raw.find(replacement.first, offset);
              if (at < next) {
                next = at;
                match = &replacement;
              }
            }
            for (const auto& replacement : encoded_) {
              if (depth != 0) break;
              const auto at = raw.find(replacement.first, offset);
              if (at < next) {
                next = at;
                match = &replacement;
                already_encoded = true;
              }
            }
            const auto encoded = EncodeCustomInput(
                std::string_view(raw).substr(offset, next - offset));
            if (!body.empty()) body += ' ';
            body += Literal(encoded.substr(1, encoded.size() - 2));
            if (!match) break;
            body += ' ' + (already_encoded
                               ? match->second
                               : Add(xgrammar::Grammar::FromEBNF(match->second),
                                     depth + 1));
            offset = next + match->first.size();
          } while (offset < raw.size());
          break;
        }
        case 1:
        case 2:  // Unicode character class, optionally repeated.
          body = CharacterClass(values, kind == 2);
          break;
        case 3:
          body = "\"\"";
          break;
        case 4:
          Require(values.size() == 1);
          body = rule(values[0]);
          break;
        case 5:
        case 6:
          body = "(";
          for (const auto id : values) {
            if (body.size() > 1) body += kind == 5 ? " " : " | ";
            body += expr(id);
          }
          body += ')';
          if (values.empty()) body = "\"\"";
          break;
        case 8:  // Repeat(rule, min, max); -1 means unbounded.
          Require(values.size() == 3 && values[1] >= 0 &&
                  (values[2] == -1 || values[2] >= values[1]));
          body = rule(values[0]) + '{' + std::to_string(values[1]) + ',' +
                 (values[2] < 0 ? "" : std::to_string(values[2])) + '}';
          break;
        case 12: {  // A Lark lexer regex retained as a leaf expression.
          Require(!values.empty() && values[0] == 0);
          std::string regex;
          for (size_t j = 1; j < values.size(); ++j) {
            Require(values[j] >= 0 && values[j] <= 255);
            regex.push_back(static_cast<char>(values[j]));
          }
          body = Add(xgrammar::Grammar::FromEBNF(CustomRegexEbnf(regex)),
                     depth + 1);
          break;
        }
        default:
          throw std::runtime_error(
              "custom grammar contains non-text expressions");
      }
      rules_ += expr(static_cast<int>(i)) + " ::= " + body + '\n';
      Bound(rules_.size());
    }
    for (size_t i = 0; i < rules.array.size(); ++i) {
      const auto& fields = rules.array[i];
      Require(fields.IsArray() && fields.array.size() >= 3);
      rules_ +=
          rule(static_cast<int>(i)) + " ::= " + expr(Integer(fields.array[1]));
      const int lookahead = Integer(fields.array[2]);
      if (lookahead != -1) rules_ += " (= " + expr(lookahead) + ')';
      rules_ += '\n';
      Bound(rules_.size());
    }
    return rule(Integer(*Field(object, "root_rule_id")));
  }
  std::vector<std::pair<std::string, std::string>> regexes_;
  std::vector<std::pair<std::string, std::string>> encoded_;
  int next_ = 0;
  std::string rules_;
};

io::Json String(std::string value) {
  io::Json out;
  out.type = io::Json::Type::kString;
  out.str = std::move(value);
  return out;
}
void Expand(io::Json* node, int depth) {
  Require(depth < 32);
  if (node->GetString("type") == "json_schema") return;
  if (node->GetString("type") == "q4t_schema") {
    OutputSchemaGraph graph;
    const auto status = graph.Init(*Field(*node, "schema"),
                                   node->GetBool("strict"), nullptr);
    if (!status) {
      if (status.message().starts_with("schema_resource_exhausted:"))
        throw std::length_error("schema graph resource limit");
      throw std::runtime_error("invalid schema graph");
    }
    const auto grammar = CompileOutputSchemaEbnf(
        graph, CompileSchemaStringSyntax, CheckSchemaString);
    node->object = {{"type", String("grammar")},
                    {"syntax", String("ebnf")},
                    {"grammar", String(grammar)}};
    return;
  }
  if (node->GetString("type") == "q4t_custom") {
    const auto syntax = node->GetString("syntax");
    const auto definition = node->GetString("definition");
    Require(syntax == "text" || syntax == "regex" || syntax == "lark");
    const auto lark =
        syntax == "lark" ? PrepareCustomLark(definition) : CustomLarkSource{};
    auto grammar =
        syntax == "text"
            ? xgrammar::Grammar::FromEBNF("root ::= [\\u0000-\\U0010ffff]*")
        : syntax == "regex"
            ? xgrammar::Grammar::FromEBNF(CustomRegexEbnf(definition))
            : xgrammar::Grammar::FromLark(lark.text);
    const auto encoded = Encoder(lark.regexes).Encode(grammar);
    node->object = {{"type", String("grammar")},
                    {"syntax", String("ebnf")},
                    {"grammar", String(encoded)}};
    return;
  }
  for (auto& [key, value] : node->object) {
    (void)key;
    Expand(&value, depth + 1);
  }
  for (auto& value : node->array) Expand(&value, depth + 1);
}
}  // namespace
#endif

std::string ExpandCustomGrammarFormats(std::string_view structural_tag) {
  if (structural_tag.find("\"q4t_custom\"") == structural_tag.npos &&
      structural_tag.find("\"q4t_schema\"") == structural_tag.npos &&
      structural_tag.find("\"q4t_guard\"") == structural_tag.npos)
    return std::string(structural_tag);
#ifdef Q4T_ENABLE_XGRAMMAR
  io::Json value;
  Require(io::ParseJson(std::string(structural_tag), &value, true,
                        {64, 262144, 2 * 1024 * 1024, true, true})
              .ok());
  std::erase_if(value.object, [](const auto& member) {
    return member.first == "q4t_guard";
  });
  Expand(&value, 0);
  auto result = OutputJson(value);
  Bound(result.size());
  return result;
#else
  throw std::runtime_error("custom grammar backend is not built");
#endif
}

std::string EncodeJsonStringGrammar(std::string_view ebnf) {
#ifdef Q4T_ENABLE_XGRAMMAR
  std::vector<std::pair<std::string, std::string>> encoded;
  const auto preserved = PreserveCountedRules(ebnf, &encoded);
  return Encoder({}, std::move(encoded)).Encode(
      xgrammar::Grammar::FromEBNF(preserved));
#else
  (void)ebnf;
  throw std::runtime_error("schema grammar backend is not built");
#endif
}

}  // namespace q4t::server
