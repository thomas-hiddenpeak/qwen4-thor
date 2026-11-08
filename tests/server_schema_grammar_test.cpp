#include "../src/server/schema_grammar.h"
#include "../src/server/schema_idna.h"
#include "../src/server/custom_regex.h"

#include <algorithm>
#include <stdexcept>
#include <string>
#include <vector>

#include <xgrammar/compiler.h>
#include <xgrammar/matcher.h>

#include "q4t/server/custom_grammar.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
Json Parse(const std::string& text) {
  Json value;
  const auto status = q4t::io::ParseJson(text, &value, true);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
// This fixture supplies the generic string language. Actual production format
// semantics will be tested through the real string adapter before HTTP wiring.
std::string Strings(const std::vector<const Json*>& terms) {
  int64_t minimum = 0, maximum = 2147483647;
  std::vector<std::string> patterns;
  for (const auto* term : terms) {
    minimum = std::max(minimum, term->GetInt("minLength", 0));
    maximum = std::min(maximum, term->GetInt("maxLength", 2147483647));
    if (const auto* pattern = term->Find("pattern"))
      patterns.push_back("(?s:.*)(?:" + pattern->str + ")(?s:.*)");
    if (term->Has("format")) throw std::runtime_error("format not in fixture");
  }
  patterns.push_back("(?s:.{" + std::to_string(minimum) + ',' +
                     (maximum == 2147483647 ? "" : std::to_string(maximum)) +
                     "})");
  return EncodeJsonStringGrammar(SchemaRegexEbnf(patterns));
}
q4t::Status StringCheck(const Json& schema, std::string_view value,
                        size_t* work, bool* accepted) {
  if (schema.Has("format")) throw std::runtime_error("format not in fixture");
  const auto* pattern = schema.Find("pattern");
  *accepted =
      !pattern ||
      SchemaRegexMatches("(?s:.*)(?:" + pattern->str + ")(?s:.*)", value, work);
  return {};
}
std::vector<std::string> Vocabulary() {
  std::vector<std::string> result(257);
  for (int c = 0; c < 256; ++c) result[c + 1] = char(c);
  return result;
}
bool Language(const Json& schema, bool strict,
              const std::vector<std::string>& yes,
              const std::vector<std::string>& no) {
  OutputSchemaGraph graph;
  std::string path;
  const auto status = graph.Init(schema, strict, &path);
  if (!status) throw std::runtime_error(path + ": " + status.message());
  const auto ebnf = CompileOutputSchemaEbnf(graph, Strings, StringCheck);
  xgrammar::TokenizerInfo tokenizer(Vocabulary(), xgrammar::VocabType::RAW, 257,
                                    std::vector<int32_t>{0}, false);
  xgrammar::GrammarCompiler compiler(tokenizer, 1, false, 0);
  const auto compiled =
      compiler.CompileGrammar(xgrammar::Grammar::FromEBNF(ebnf));
  const auto accepts = [&](const std::string& text) {
    xgrammar::GrammarMatcher matcher(compiled);
    return matcher.AcceptString(text) && matcher.AcceptToken(0) &&
           matcher.IsTerminated();
  };
  for (const auto& text : yes) {
    Q4T_CHECK(graph.Check(Parse(text), StringCheck));
    if (!accepts(text)) {
      std::fprintf(stderr, "schema grammar rejected: %s\n", text.c_str());
      return false;
    }
  }
  for (const auto& text : no) {
    Json value;
    const auto parsed = q4t::io::ParseJson(text, &value, true);
    Q4T_CHECK(!parsed || !graph.Check(value, StringCheck));
    if (accepts(text)) {
      std::fprintf(stderr, "schema grammar accepted invalid: %s\n",
                   text.c_str());
      return false;
    }
  }
  return true;
}
Json Object(const std::string& child) {
  return Parse(R"({"type":"object","properties":{"v":)" + child +
               R"(},"required":["v"],"additionalProperties":false})");
}
}  // namespace

Q4T_TEST(schema_grammar_recursive_anyof_and_nullable_alias) {
  const auto schema = Parse(R"({"type":"object","properties":{
    "head":{"$ref":"#/$defs/node"}},"required":["head"],
    "additionalProperties":false,"$defs":{"node":{"type":"object",
    "properties":{"value":{"type":"integer"},"next":{"anyOf":[
    {"$ref":"#/$defs/node","type":"object"},{"type":"null"}]}},
    "required":["value","next"],"additionalProperties":false}}})");
  Q4T_CHECK(
      Language(schema, true,
               {R"({"head":{"value":1,"next":null}})",
                R"({"head":{"value":1,"next":{"value":2,"next":null}}})"},
               {R"({"head":null})", R"({"head":{"value":"1","next":null}})",
                R"({"head":{"value":1}})"}));
  return true;
}

Q4T_TEST(schema_grammar_conjunction_ref_bounds_and_multiple_lcm) {
  const auto schema = Parse(R"({"type":"object","properties":{"v":{
    "$ref":"#/$defs/base","maximum":1.8,"multipleOf":0.3,
    "anyOf":[{"type":"number","exclusiveMinimum":0.6},
             {"type":"number","const":0.6}]}},"required":["v"],
    "additionalProperties":false,"$defs":{"base":{"type":"number",
    "minimum":0,"multipleOf":0.2}}})");
  Q4T_CHECK(Language(
      schema, true, {R"({"v":0.6})", R"({"v":1.2})", R"({"v":1.800})"},
      {R"({"v":0})", R"({"v":0.9})", R"({"v":2.4})", R"({"v":1.2000000001})"}));
  return true;
}

Q4T_TEST(schema_grammar_optional_object_and_closed_intersections) {
  const auto schema = Parse(R"({"type":"object","properties":{
    "a":{"type":"integer"},"b":{"type":"boolean"},"c":{"type":"null"}},
    "required":["b"],"additionalProperties":false})");
  Q4T_CHECK(Language(schema, false,
                     {R"({"b":true})", R"({"a":1,"b":false})",
                      R"({"b":true,"c":null})", R"({"a":1,"b":true,"c":null})"},
                     {R"({})", R"({"a":1})", R"({"b":true,"other":null})"}));
  const auto overlap = Parse(R"({"type":"object","properties":{"v":{
    "$ref":"#/$defs/base","type":"object","properties":{
    "b":{"type":"number","minimum":1}},"additionalProperties":false}},
    "required":["v"],"additionalProperties":false,"$defs":{"base":{
    "type":"object","properties":{"a":{"type":"integer"},"b":{"type":"integer"}},
    "additionalProperties":false}}})");
  Q4T_CHECK(
      Language(overlap, false, {R"({"v":{}})", R"({"v":{"b":2}})"},
               {R"({"v":{"a":1}})", R"({"v":{"b":0}})", R"({"v":{"b":1.5}})"}));
  return true;
}

Q4T_TEST(schema_grammar_enum_exact_values_and_delimiter_strings) {
  Q4T_CHECK(Language(Object(R"({"type":"number","enum":[1.1,9007199254740993],
                                "minimum":2})"),
                     true, {R"({"v":9007199254740993})"},
                     {R"({"v":1.1})", R"({"v":9007199254740992})"}));
  Q4T_CHECK(Language(
      Object(
          R"({"type":["string","null"],"enum":[null,"null",">", "</parameter>"],
                                "minLength":2})"),
      true,
      {R"({"v":null})", R"({"v":"null"})", R"({"v":"\u003c/parameter>"})"},
      {R"({"v":">"})", R"({"v":"NULL"})"}));
  Q4T_CHECK(
      Language(Object(R"({"type":"string","pattern":"^[中😀]+$",
                                "minLength":2,"maxLength":3})"),
               true, {R"({"v":"中😀"})", R"({"v":"😀中😀"})"},
               {R"({"v":"中"})", R"({"v":"中😀中😀"})", R"({"v":"中国"})"}));
  return true;
}

Q4T_TEST(schema_grammar_array_bounds_and_nonproductive_alias_branch) {
  Q4T_CHECK(Language(Object(R"({"type":"array","items":{"type":"integer"},
                                "minItems":2,"maxItems":3})"),
                     true, {R"({"v":[1,2]})", R"({"v":[1,2,3]})"},
                     {R"({"v":[]})", R"({"v":[1]})", R"({"v":[1,2,3,4]})",
                      R"({"v":[1,2.5]})"}));
  const auto schema = Parse(R"({"type":"object","properties":{"v":{
    "anyOf":[{"$ref":"#/$defs/loop"},{"type":"boolean"}]}},"required":["v"],
    "additionalProperties":false,"$defs":{"loop":{"$ref":"#/$defs/loop"}}})");
  Q4T_CHECK(Language(schema, true, {R"({"v":true})", R"({"v":false})"},
                     {R"({"v":0})", R"({"v":null})"}));
  return true;
}

Q4T_TEST(schema_grammar_official_property_scale_5000) {
  std::string properties, required, output;
  for (int i = 0; i < 5000; ++i) {
    const auto key = "\"p" + std::to_string(i) + "\"";
    if (i) {
      properties += ',';
      required += ',';
      output += ',';
    }
    properties += key + R"(:{"type":"boolean"})";
    required += key;
    output += key + ":true";
  }
  const auto schema = Parse("{\"type\":\"object\",\"properties\":{" +
                            properties + "},\"required\":[" + required +
                            "],\"additionalProperties\":false}");
  Q4T_CHECK(Language(schema, true, {'{' + output + '}'}, {"{}"}));
  return true;
}

Q4T_TEST(schema_grammar_dependency_error_is_not_unsatisfiable_schema) {
  const auto schema = Object(R"({"type":"string","enum":["xn--ll-0ea"],
    "anyOf":[{"type":"string","format":"hostname"}]})");
  OutputSchemaGraph graph;
  std::string path;
  Q4T_CHECK(graph.Init(schema, true, &path));
  const auto unavailable = [](const Json&, std::string_view, size_t*, bool*) {
    return q4t::Status::Fail("schema_dependency_unavailable: fixture");
  };
  const auto status = graph.Check(Parse(R"({"v":"xn--ll-0ea"})"), unavailable);
  Q4T_CHECK(!status &&
            status.message().starts_with("schema_dependency_unavailable:"));
  bool retained = false;
  try {
    (void)CompileOutputSchemaEbnf(graph, Strings, unavailable);
  } catch (const SchemaDependencyUnavailable&) {
    retained = true;
  }
  Q4T_CHECK(retained);
  return true;
}
