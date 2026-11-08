#include "q4t/server/schema_prefix.h"

#include <stdexcept>

#include "../src/server/schema_string.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_graph.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
Json Parse(const std::string& text) {
  Json value;
  const auto status =
      q4t::io::ParseJson(text, &value, true, {64, 65536, 1024 * 1024, true});
  if (!status) throw std::runtime_error(status.message());
  return value;
}
Json Object(const std::string& child) {
  return Parse(R"({"type":"object","properties":{"v":)" + child +
               R"(},"required":["v"],"additionalProperties":false})");
}
q4t::Status Prefix(
    const OutputSchemaGraph& graph, std::string_view text,
    const OutputSchemaGraph::StringCheck& strings = CheckSchemaString) {
  size_t work = 8 * 1024 * 1024, consumed = 0;
  SchemaPrefixValue value;
  auto status = ParseSchemaPrefix(text, &value, &consumed, &work);
  if (!status) return status;
  if (consumed != text.size()) return q4t::Status::Fail("unhandled suffix");
  return graph.CheckPrefix(value, &work, strings);
}
OutputSchemaGraph Graph(const Json& schema) {
  OutputSchemaGraph graph;
  std::string path;
  const auto status = graph.Init(schema, true, &path);
  if (!status) throw std::runtime_error(path + ": " + status.message());
  return graph;
}
}  // namespace

Q4T_TEST(schema_prefix_every_byte_utf8_escape_and_nested_values) {
  const std::string text =
      R"({"x":[true,false,null,-12.30e+2,"中😀\n\uD83D\uDE00"],"z":{}})";
  for (size_t end = 0; end <= text.size(); ++end) {
    size_t work = 100000, consumed = 0;
    SchemaPrefixValue value;
    const auto status = ParseSchemaPrefix(std::string_view(text).substr(0, end),
                                          &value, &consumed, &work);
    Q4T_CHECK(status && consumed == end);
    Q4T_CHECK(value.complete == (end == text.size()));
    if (value.complete)
      Q4T_CHECK(OutputJson(value.value) == OutputJson(Parse(text)));
  }
  return true;
}

Q4T_TEST(schema_prefix_invalid_syntax_and_duplicates_are_not_incomplete) {
  for (const auto& text : std::vector<std::string>{
           R"({"x":01})", R"({"x":1e+})", R"({"x":-.1})", R"({"x":truX})",
           R"({"x":1,})", R"([1,])", R"([,)", R"({"x":0,"x")", R"({"x";1})",
           R"({"x":"\uDC00"})", R"({"x":"\uD800x"})", "{\"x\":\"\xc0\x80"}) {
    size_t work = 100000, consumed = 0;
    SchemaPrefixValue value;
    Q4T_CHECK(!ParseSchemaPrefix(text, &value, &consumed, &work));
  }
  for (const auto& text : {R"({"x":-)", R"({"x":1.)", R"({"x":1e+)",
                           R"({"x":"\uD800)", R"({"x":tru)", "[1, "}) {
    size_t work = 100000, consumed = 0;
    SchemaPrefixValue value;
    Q4T_CHECK(ParseSchemaPrefix(text, &value, &consumed, &work));
    Q4T_CHECK(!value.complete && consumed == std::string_view(text).size());
  }
  return true;
}

Q4T_TEST(schema_prefix_completion_reports_protocol_suffix_and_resource) {
  const std::string text = "{\"x\":\"</parameter>\"} \n</parameter>";
  size_t work = 100000, consumed = 0;
  SchemaPrefixValue value;
  Q4T_CHECK(ParseSchemaPrefix(text, &value, &consumed, &work));
  Q4T_CHECK(value.complete && text.substr(consumed) == "</parameter>");
  Q4T_CHECK(value.value.GetString("x") == "</parameter>");
  for (const auto& too_large :
       {std::string(1024 * 1024 + 1, ' '), std::string(65, '[')}) {
    work = 8 * 1024 * 1024;
    const auto status = ParseSchemaPrefix(too_large, &value, &consumed, &work);
    Q4T_CHECK(!status &&
              status.message().starts_with("schema_resource_exhausted:"));
  }
  work = 0;
  const auto status = ParseSchemaPrefix("{}", &value, &consumed, &work);
  Q4T_CHECK(!status &&
            status.message().starts_with("schema_resource_exhausted:"));
  return true;
}

Q4T_TEST(schema_prefix_string_format_checked_at_closing_quote) {
  const auto schema = Parse(R"({"type":"object","properties":{
    "date":{"type":"string","format":"date"},"n":{"type":"integer"}},
    "required":["date","n"],"additionalProperties":false})");
  const auto graph = Graph(schema);
  Q4T_CHECK(Prefix(graph, R"({"date":"2023-02-30)"));
  Q4T_CHECK(!Prefix(graph, R"({"date":"2023-02-30")"));
  Q4T_CHECK(Prefix(graph, R"({"date":"2024-02-29")"));
  Q4T_CHECK(!Prefix(graph, R"({"date":"2024-02-29"})"));
  Q4T_CHECK(Prefix(graph, R"({"date":"2024-02-29","n":1})"));
  return true;
}

Q4T_TEST(schema_prefix_anyof_keeps_valid_branch_and_rejects_bad_value) {
  const auto schema = Object(R"({"anyOf":[
    {"type":"object","properties":{"kind":{"const":"date"},
     "date":{"type":"string","format":"date"}},"required":["kind","date"],"additionalProperties":false},
    {"type":"object","properties":{"kind":{"const":"ip"},
     "ip":{"type":"string","format":"ipv4"}},"required":["kind","ip"],"additionalProperties":false}]})");
  const auto graph = Graph(schema);
  Q4T_CHECK(Prefix(graph, R"({"v":{"kind":"i)"));
  Q4T_CHECK(!Prefix(graph, R"({"v":{"kind":"x)"));
  Q4T_CHECK(Prefix(graph, R"({"v":{"kind":"ip","ip":"192.0.2.1")"));
  Q4T_CHECK(!Prefix(graph, R"({"v":{"kind":"ip","ip":"192.0.2.999")"));
  Q4T_CHECK(!Prefix(graph, R"({"v":{"kind":"ip","date")"));
  Q4T_CHECK(Prefix(graph, R"({"v":{"kind":"date","date":"2024-02-29"}})"));
  return true;
}

Q4T_TEST(schema_prefix_exact_number_and_array_limits_at_completion) {
  const auto schema = Object(R"({"type":"integer","enum":[12300]})");
  const auto graph = Graph(schema);
  Q4T_CHECK(Prefix(graph, R"({"v":123e)"));
  Q4T_CHECK(Prefix(graph, R"({"v":123e2})"));
  Q4T_CHECK(!Prefix(graph, R"({"v":123e1})"));
  const auto array =
      Object(R"({"type":"array","items":{"type":"integer"},"maxItems":1})");
  const auto arrays = Graph(array);
  Q4T_CHECK(Prefix(arrays, R"({"v":[1)"));
  Q4T_CHECK(!Prefix(arrays, R"({"v":[1,)"));
  Q4T_CHECK(Prefix(arrays, R"({"v":[1]})"));
  return true;
}

Q4T_TEST(schema_prefix_recursive_ref_const_and_unicode_length) {
  const auto schema =
      Parse(R"({"type":"object","properties":{"v":{"$ref":"#/$defs/node"}},
    "required":["v"],"additionalProperties":false,"$defs":{"node":{"anyOf":[
      {"type":"null"},{"type":"object","properties":{"name":{"type":"string","maxLength":2},
      "next":{"$ref":"#/$defs/node"}},"required":["name","next"],"additionalProperties":false}]}}})");
  const auto graph = Graph(schema);
  Q4T_CHECK(Prefix(graph, R"({"v":{"name":"中😀","next":{"name":"a")"));
  Q4T_CHECK(!Prefix(graph, R"({"v":{"name":"中😀文)"));
  Q4T_CHECK(Prefix(graph, R"({"v":{"name":"中😀","next":null}})"));
  const auto constant = Object(R"({"const":{"a":[1,2],"b":true}})");
  const auto constants = Graph(constant);
  Q4T_CHECK(Prefix(constants, R"({"v":{"a":[1,)"));
  Q4T_CHECK(!Prefix(constants, R"({"v":{"a":[1,3])"));
  Q4T_CHECK(!Prefix(constants, R"({"v":{"c")"));
  return true;
}

Q4T_TEST(schema_prefix_dependency_and_budget_keep_failure_category) {
  const auto schema = Object(R"({"type":"string","format":"hostname"})");
  const auto graph = Graph(schema);
  const auto unavailable = [](const Json&, std::string_view, size_t*, bool*) {
    return q4t::Status::Fail("schema_dependency_unavailable: fixture");
  };
  const auto status = Prefix(graph, R"({"v":"xn--ll-0ea")", unavailable);
  Q4T_CHECK(!status &&
            status.message().starts_with("schema_dependency_unavailable:"));
  size_t work = 1000, consumed = 0;
  SchemaPrefixValue value;
  Q4T_CHECK(ParseSchemaPrefix(R"({"v":)", &value, &consumed, &work));
  work = 0;
  const auto bounded = graph.CheckPrefix(value, &work, CheckSchemaString);
  Q4T_CHECK(!bounded &&
            bounded.message().starts_with("schema_resource_exhausted:"));
  return true;
}
