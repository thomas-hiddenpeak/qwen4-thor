#include "schema_grammar.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <map>
#include <numeric>
#include <set>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/custom_tools.h"
#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_decimal.h"
#include "schema_number_grammar.h"
#include "schema_idna.h"

namespace q4t::server {
namespace {
using io::Json;
using Terms = std::vector<const Json*>;
constexpr auto kEmpty = "[^\\u0000-\\U0010ffff]";
constexpr size_t kBytes = 512 * 1024;
constexpr unsigned kAll = 127;

std::string Literal(std::string_view text) {
  return '"' + detail::JsonEscape(std::string(text)) + '"';
}
unsigned TypeBit(std::string_view name) {
  if (name == "null") return 1;
  if (name == "boolean") return 2;
  if (name == "string") return 4;
  if (name == "integer") return 8;
  if (name == "number") return 8 | 16;
  if (name == "array") return 32;
  if (name == "object") return 64;
  throw std::runtime_error("unchecked schema type");
}
unsigned Types(const Json& schema) {
  const auto* type = schema.Find("type");
  if (!type) return kAll;
  if (type->IsString()) return TypeBit(type->str);
  unsigned result = 0;
  for (const auto& name : type->array) result |= TypeBit(name.str);
  return result;
}
std::string Union(const std::vector<std::string>& values) {
  if (values.empty()) return kEmpty;
  if (values.size() == 1) return values.front();
  std::string result = "(";
  for (size_t i = 0; i < values.size(); ++i) {
    if (i) result += " | ";
    result += values[i];
  }
  return result + ')';
}
std::string SafeJson(const Json& value) {
  // A JSON literal can appear inside the model's tool delimiters. Escape '<'
  // in string values and property names, without changing the JSON value.
  const auto text = OutputJson(value);
  std::string result;
  for (char c : text) result += c == '<' ? "\\u003c" : std::string(1, c);
  return result;
}

SchemaDecimal Number(const Json& value) {
  SchemaDecimal out;
  const auto status = SchemaDecimal::Parse(OutputJson(value), &out);
  if (!status) throw std::runtime_error(status.message());
  return out;
}
Json NumberSchema(const Terms& terms) {
  Json result;
  result.type = Json::Type::kObject;
  for (const auto* key :
       {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}) {
    const Json* selected = nullptr;
    const bool lower = std::string_view(key) == "minimum" ||
                       std::string_view(key) == "exclusiveMinimum";
    for (const auto* term : terms) {
      const auto* raw = term->Find(key);
      if (!raw) continue;
      if (!selected || (lower ? Number(*raw).Compare(Number(*selected)) > 0
                              : Number(*raw).Compare(Number(*selected)) < 0))
        selected = raw;
    }
    if (selected) result.object.emplace_back(key, *selected);
  }
  // LCM of positive decimal rationals by prime exponents. Powers of two/five
  // may be negative; all other prime exponents come from integer coefficients.
  // Keep the modulo automaton's real resource bound rather than rounding or
  // dropping a second multipleOf introduced by a reference/anyOf sibling.
  bool multiple = false;
  int64_t twos = 0, fives = 0;
  uint64_t rest = 1;
  for (const auto* term : terms) {
    const auto* raw = term->Find("multipleOf");
    if (!raw) continue;
    const auto number = Number(*raw);
    const auto& digits = number.Digits();
    uint64_t coefficient = 0;
    const auto parsed = std::from_chars(
        digits.data(), digits.data() + digits.size(), coefficient);
    if (parsed.ec != std::errc{} ||
        parsed.ptr != digits.data() + digits.size() || !coefficient ||
        coefficient > 4096)
      throw std::length_error("multipleOf automaton resource bound");
    int64_t a = number.Exponent(), b = a;
    while (coefficient % 2 == 0) {
      coefficient /= 2;
      ++a;
    }
    while (coefficient % 5 == 0) {
      coefficient /= 5;
      ++b;
    }
    twos = multiple ? std::max(twos, a) : a;
    fives = multiple ? std::max(fives, b) : b;
    multiple = true;
    rest = std::lcm(rest, coefficient);
    if (rest > 4096)
      throw std::length_error("combined multipleOf automaton resource bound");
  }
  if (multiple) {
    const auto exponent = std::min(twos, fives);
    for (int64_t i = exponent; i < twos; ++i) {
      if (rest > 4096 / 2)
        throw std::length_error("multipleOf coefficient bound");
      rest *= 2;
    }
    for (int64_t i = exponent; i < fives; ++i) {
      if (rest > 4096 / 5)
        throw std::length_error("multipleOf coefficient bound");
      rest *= 5;
    }
    Json value;
    const auto status = io::ParseJson(
        std::to_string(rest) + "e" + std::to_string(exponent), &value, true);
    if (!status)
      throw std::length_error("combined multipleOf exceeds numeric range");
    result.object.emplace_back("multipleOf", std::move(value));
  }
  return result;
}

class Compiler {
 public:
  Compiler(const OutputSchemaGraph& graph, const SchemaStringCompiler& strings,
           const OutputSchemaGraph::StringCheck& check_strings)
      : graph_(graph), strings_(strings), check_strings_(check_strings) {
    for (const auto* node : graph.Nodes()) ids_.emplace(node, ids_.size());
  }
  std::string Run() {
    const auto root = Add({&graph_.Root()});
    return "root ::= ws " + root + " ws\nws ::= [ \\t\\n\\r]*\n" + rules_;
  }

 private:
  void Charge(size_t amount) {
    if (amount > work_) throw std::length_error("schema compiler work limit");
    work_ -= amount;
  }
  void Rule(const std::string& name, const std::string& body) {
    Charge(name.size() + body.size());
    rules_ += name + " ::= " + body + '\n';
    if (rules_.size() > kBytes)
      throw std::length_error("schema grammar expansion resource bound");
  }
  std::string Name() {
    if (next_ >= 32768) throw std::length_error("schema grammar rule limit");
    return "schema_" + std::to_string(next_++);
  }
  std::vector<Terms> Product(const std::vector<Terms>& a,
                             const std::vector<Terms>& b) {
    std::vector<Terms> result;
    for (const auto& left : a) {
      for (const auto& right : b) {
        Charge(1 + left.size() + right.size());
        if (result.size() >= 1024)
          throw std::length_error("schema alternative product bound");
        Terms terms = left;
        terms.insert(terms.end(), right.begin(), right.end());
        result.push_back(std::move(terms));
      }
    }
    return result;
  }
  std::vector<Terms> Expand(const Json& node,
                            std::unordered_set<const Json*>* active) {
    Charge(1);
    if (active->size() > 64) throw std::length_error("schema reference depth");
    if (!active->insert(&node).second) return {};
    std::vector<Terms> result{{&node}};
    if (const auto* target = graph_.RefTarget(node))
      result = Product(result, Expand(*target, active));
    if (const auto* choices = node.GetArray("anyOf")) {
      std::vector<Terms> alternatives;
      for (const auto& child : choices->array) {
        auto expanded = Expand(child, active);
        if (expanded.size() > 1024 - alternatives.size())
          throw std::length_error("schema alternative count bound");
        alternatives.insert(alternatives.end(), expanded.begin(),
                            expanded.end());
      }
      result = Product(result, alternatives);
    }
    active->erase(&node);
    return result;
  }
  std::vector<size_t> Key(Terms* terms) {
    std::sort(terms->begin(), terms->end(), [&](const auto* a, const auto* b) {
      return ids_.at(a) < ids_.at(b);
    });
    terms->erase(std::unique(terms->begin(), terms->end()), terms->end());
    std::vector<size_t> key;
    for (const auto* term : *terms) key.push_back(ids_.at(term));
    return key;
  }
  std::string Add(Terms terms) {
    Charge(1 + terms.size());
    const auto key = Key(&terms);
    if (auto found = names_.find(key); found != names_.end())
      return found->second;
    const auto name = Name();
    names_.emplace(key,
                   name);  // Before children: productive refs keep identity.
    std::vector<Terms> alternatives{{}};
    for (const auto* term : terms) {
      std::unordered_set<const Json*> active;
      alternatives = Product(alternatives, Expand(*term, &active));
    }
    std::vector<std::string> bodies;
    std::set<std::vector<size_t>> seen;
    for (auto& alternative : alternatives) {
      if (!seen.insert(Key(&alternative)).second) continue;
      bodies.push_back(Atom(alternative));
    }
    Rule(name, Union(bodies));
    return name;
  }
  std::string Import(const std::string& ebnf) {
    if (auto found = imports_.find(ebnf); found != imports_.end())
      return found->second;
    Charge(ebnf.size());
    const auto prefix = Name() + '_';
    std::string result;
    char quoted = 0;
    bool character_class = false, escaped = false;
    for (size_t i = 0; i < ebnf.size();) {
      const char c = ebnf[i];
      if (quoted || character_class) {
        result += c;
        ++i;
        if (escaped)
          escaped = false;
        else if (c == '\\')
          escaped = true;
        else if (quoted && c == quoted)
          quoted = 0;
        else if (character_class && c == ']')
          character_class = false;
      } else if (c == '"' || c == '\'') {
        quoted = c;
        result += c;
        ++i;
      } else if (c == '[') {
        character_class = true;
        result += c;
        ++i;
      } else if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_') {
        const auto start = i++;
        while (i < ebnf.size()) {
          const char next = ebnf[i];
          if (!((next >= 'a' && next <= 'z') || (next >= 'A' && next <= 'Z') ||
                (next >= '0' && next <= '9') || next == '_' || next == '-'))
            break;
          ++i;
        }
        const auto identifier = ebnf.substr(start, i - start);
        // Public EBNF macros are operators, not imported rule identifiers.
        // Only the generated Regex(string) form is emitted by our encoder.
        result += identifier == "Regex" && i < ebnf.size() && ebnf[i] == '('
                      ? identifier
                      : prefix + identifier;
      } else {
        result += c;
        ++i;
      }
    }
    if (quoted || character_class || escaped)
      throw std::runtime_error("malformed internal schema EBNF");
    rules_ += result;
    if (rules_.size() > kBytes)
      throw std::length_error("schema grammar expansion resource bound");
    imports_.emplace(ebnf, prefix + "root");
    return prefix + "root";
  }
  std::string Atom(const Terms& terms) {
    const Json* constants = nullptr;
    bool singleton = false;
    for (const auto* term : terms) {
      if (const auto* constant = term->Find("const")) {
        constants = constant;
        singleton = true;
        break;
      }
      if (!constants) constants = term->GetArray("enum");
    }
    if (constants) {
      std::vector<const Json*> values;
      if (singleton)
        values.push_back(constants);
      else
        for (const auto& value : constants->array) values.push_back(&value);
      std::vector<std::string> options;
      for (const auto* value : values) {
        bool valid = true;
        for (const auto* term : terms) {
          Charge(1);
          const auto status = graph_.CheckNode(*term, *value, check_strings_);
          if (!status &&
              status.message().starts_with("schema_dependency_unavailable:"))
            throw SchemaDependencyUnavailable(status.message());
          if (!status &&
              status.message().starts_with("schema_resource_exhausted:"))
            throw std::length_error(status.message());
          if (!status) {
            valid = false;
            break;
          }
        }
        if (valid) options.push_back(Literal(SafeJson(*value)));
      }
      return Union(options);
    }
    unsigned types = kAll;
    for (const auto* term : terms) types &= Types(*term);
    std::vector<std::string> bodies;
    if (types & 1) bodies.push_back("\"null\"");
    if (types & 2) bodies.push_back("(\"true\" | \"false\")");
    if (types & 4) {
      if (!strings_)
        throw std::runtime_error("schema string compiler required");
      bodies.push_back(Import(strings_(terms)));
    }
    if (types & (8 | 16))
      bodies.push_back(
          Import(SchemaNumberGrammar(NumberSchema(terms), !(types & 16))));
    if (types & 32) bodies.push_back(Array(terms));
    if (types & 64) bodies.push_back(Object(terms));
    return Union(bodies);
  }
  std::string Array(const Terms& terms) {
    size_t minimum = 0, maximum = 2147483647;
    Terms items;
    for (const auto* term : terms) {
      minimum =
          std::max(minimum, static_cast<size_t>(term->GetInt("minItems", 0)));
      maximum = std::min(
          maximum, static_cast<size_t>(term->GetInt("maxItems", 2147483647)));
      if (const auto* child = term->Find("items")) items.push_back(child);
    }
    if (minimum > maximum) return kEmpty;
    if (maximum == 0) return "\"[\" ws \"]\"";
    const auto item = items.empty() ? Any() : Add(items);
    const auto tail = "(ws \",\" ws " + item + ')';
    const auto count = [](size_t value) {
      return value == 2147483647 ? "" : std::to_string(value);
    };
    std::string body =
        item + ' ' + tail + '{' + std::to_string(minimum ? minimum - 1 : 0) +
        ',' + count(maximum == 2147483647 ? maximum : maximum - 1) + '}';
    if (!minimum) body = '(' + body + ")?";
    return "\"[\" ws " + body + " ws \"]\"";
  }
  std::string Object(const Terms& terms) {
    std::vector<std::string> order;
    std::map<std::string, Terms> properties;
    std::set<std::string> required;
    bool closed = false;
    for (const auto* term : terms) {
      if (const auto* list = term->GetArray("required"))
        for (const auto& name : list->array) required.insert(name.str);
      if (const auto* map = term->Find("properties")) {
        for (const auto& [name, child] : map->object) {
          if (!properties.contains(name)) order.push_back(name);
          properties[name].push_back(&child);
        }
      }
      closed |= term->Has("additionalProperties") &&
                !term->GetBool("additionalProperties");
    }
    if (!closed && properties.empty() && required.empty()) return AnyObject();
    for (const auto& name : order) {
      for (const auto* term : terms) {
        if (!term->Has("additionalProperties") ||
            term->GetBool("additionalProperties"))
          continue;
        const auto* map = term->Find("properties");
        if (!map || !map->Has(name)) properties.erase(name);
      }
    }
    for (const auto& name : required)
      if (!properties.contains(name)) return kEmpty;
    if (!closed) throw std::runtime_error("unchecked open object conjunction");
    if (required.size() == properties.size()) {
      std::string body = "\"{\" ws";
      bool first = true;
      for (const auto& name : order) {
        if (!properties.contains(name)) continue;
        if (!first) body += " \",\" ws";
        first = false;
        body += ' ' + Literal(EncodeCustomInput(name)) + " ws \":\" ws " +
                Add(properties.at(name)) + " ws";
      }
      return body + " \"}\"";
    }
    std::array<std::string, 2> tail{"\"\"", "\"\""};
    for (auto it = order.rbegin(); it != order.rend(); ++it) {
      if (!properties.contains(*it)) continue;
      const auto value = Add(properties.at(*it));
      const auto key = Literal(EncodeCustomInput(*it));
      std::array<std::string, 2> next;
      for (size_t used = 0; used < 2; ++used) {
        next[used] = Name();
        const auto member = (used ? "\",\" ws " : "") + key + " ws \":\" ws " +
                            value + " ws " + tail[1];
        Rule(next[used], required.contains(*it)
                             ? member
                             : '(' + member + " | " + tail[used] + ')');
      }
      tail = std::move(next);
    }
    return "\"{\" ws " + tail[0] + "\"}\"";
  }
  std::string AnyObject() {
    const auto value = Any();
    const auto text = Import(strings_({}));
    const auto member = text + " ws \":\" ws " + value;
    return "\"{\" ws (" + member + " (ws \",\" ws " + member + ")*)? ws \"}\"";
  }
  std::string Any() {
    if (!any_.empty()) return any_;
    any_ = Name();
    const auto text = Import(strings_({}));
    Json empty;
    empty.type = Json::Type::kObject;
    const auto number = Import(SchemaNumberGrammar(empty, false));
    Rule(any_,
         Union({"\"null\"", "\"true\"", "\"false\"", text, number,
                "\"[\" ws (" + any_ + " (ws \",\" ws " + any_ + ")*)? ws \"]\"",
                AnyObject()}));
    return any_;
  }

  const OutputSchemaGraph& graph_;
  const SchemaStringCompiler& strings_;
  const OutputSchemaGraph::StringCheck& check_strings_;
  size_t next_ = 0, work_ = 8 * 1024 * 1024;
  std::string rules_, any_;
  std::unordered_map<const Json*, size_t> ids_;
  std::map<std::vector<size_t>, std::string> names_;
  std::unordered_map<std::string, std::string> imports_;
};
}  // namespace

std::string CompileOutputSchemaEbnf(
    const OutputSchemaGraph& graph, const SchemaStringCompiler& strings,
    const OutputSchemaGraph::StringCheck& check_strings) {
  if (graph.Nodes().empty()) throw std::runtime_error("schema graph is empty");
  return Compiler(graph, strings, check_strings).Run();
}

}  // namespace q4t::server
