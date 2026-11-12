#include "q4t/server/schema_graph.h"

#include <stdexcept>
#include <tuple>
#include <vector>

#include "q4t/server/output_schema.h"
#include "q4t/server/schema_decimal.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using q4t::server::OutputSchemaGraph;
using q4t::server::SchemaDecimal;
Json Parse(const std::string& text) {
  Json value;
  auto status = q4t::io::ParseJson(text, &value, true);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
SchemaDecimal Decimal(std::string_view text) {
  SchemaDecimal value;
  auto status = SchemaDecimal::Parse(text, &value);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
Json Schema(const std::string& property) {
  return Parse(R"({"type":"object","properties":{"v":)" + property +
               R"(},"required":["v"],"additionalProperties":false})");
}
}  // namespace

Q4T_TEST(schema_decimal_exact_equality_order_and_integer) {
  Q4T_CHECK(Decimal("1").Compare(Decimal("1.00e+0")) == 0);
  Q4T_CHECK(Decimal("-0.00e18").Canonical() == "0");
  Q4T_CHECK(Decimal("9007199254740993").Compare(Decimal("9007199254740992")) >
            0);
  Q4T_CHECK(Decimal("1.0000000000000001").Compare(Decimal("1")) > 0);
  Q4T_CHECK(Decimal("-1e-200").Compare(Decimal("-1e-201")) < 0);
  Q4T_CHECK(Decimal("1.01e2").IsInteger());
  Q4T_CHECK(!Decimal("1.0000000000000001").IsInteger());
  std::string plain;
  Q4T_CHECK(Decimal("-125e-2").Plain(&plain) && plain == "-1.25");
  Q4T_CHECK(Decimal("1e3").Plain(&plain) && plain == "1000");
  Q4T_CHECK(Decimal("12e-4").Plain(&plain) && plain == "0.0012");
  return true;
}

Q4T_TEST(schema_decimal_multiples_have_no_float_tolerance) {
  for (const auto& [input, step, expected] :
       std::vector<std::tuple<std::string, std::string, bool>>{
           {"0.3", "0.1", true},
           {"0.30000000000000004", "0.1", false},
           {"-2.5", "0.25", true},
           {"1", "0.25", true},
           {"1", "0.3", false},
           {"0", "1e200", true},
           {"1e100", "2.5e99", true},
           {"1e100", "3e99", false},
           {"9007199254740993", "3", true},
           {"9007199254740992", "3", false},
           {"0.05", "0.1", false}}) {
    size_t work = 1000000;
    bool actual;
    Q4T_CHECK(Decimal(input).MultipleOf(Decimal(step), &actual, &work));
    Q4T_CHECK(actual == expected);
  }
  // Independent integer modulo oracle over scaled decimal spellings.
  for (int a = -50; a <= 50; ++a) {
    for (int b = 1; b <= 17; ++b) {
      size_t work = 100000;
      bool actual;
      Q4T_CHECK(
          Decimal(std::to_string(a) + "e-2")
              .MultipleOf(Decimal(std::to_string(b) + "e-3"), &actual, &work));
      Q4T_CHECK(actual == ((a * 10) % b == 0));
    }
  }
  return true;
}

Q4T_TEST(schema_decimal_invalid_and_capacity_are_distinct) {
  for (const auto* text : {"", "+1", "01", "1.", "1e", "--1", "nan"}) {
    SchemaDecimal value;
    Q4T_CHECK(!SchemaDecimal::Parse(text, &value));
  }
  SchemaDecimal value;
  Q4T_CHECK(!SchemaDecimal::Parse("1e1000001", &value));
  size_t work = 0;
  bool result;
  auto status = Decimal("17").MultipleOf(Decimal("3"), &result, &work);
  Q4T_CHECK(!status &&
            status.message().starts_with("schema_resource_exhausted:"));
  Q4T_CHECK(!Decimal("1").MultipleOf(Decimal("0"), &result, &work));
  Q4T_CHECK(!Decimal("1").MultipleOf(Decimal("-1"), &result, &work));
  return true;
}

Q4T_TEST(schema_graph_recursive_ref_anyof_and_required) {
  const auto schema = Parse(R"({"type":"object","properties":{
    "head":{"$ref":"#/$defs/node"}},"required":["head"],
    "additionalProperties":false,"$defs":{"node":{"type":"object",
    "properties":{"value":{"type":"integer"},"next":{"anyOf":[
    {"$ref":"#/$defs/node"},{"type":"null"}]}},
    "required":["value","next"],"additionalProperties":false}}})");
  OutputSchemaGraph graph;
  std::string error;
  Q4T_CHECK(graph.Init(schema, true, &error));
  Q4T_CHECK(graph.Check(
      Parse(R"({"head":{"value":1,"next":{"value":2,"next":null}}})")));
  Q4T_CHECK(!graph.Check(Parse(R"({"head":{"value":1}})")));
  Q4T_CHECK(!graph.Check(Parse(R"({"head":{"value":1.5,"next":null}})")));
  Q4T_CHECK(!graph.Check(Parse(R"({"head":{"value":1,"next":"null"}})")));
  Q4T_CHECK(!graph.Check(Parse(R"({"head":{"value":1,"next":null,"x":0}})")));
  return true;
}

Q4T_TEST(schema_graph_local_pointer_escaping_and_exact_enum) {
  const auto schema = Parse(R"({"type":"object","properties":{"v":
    {"$ref":"#/$defs/%61~1b~0c"}},"required":["v"],
    "additionalProperties":false,"$defs":{"a/b~c":{"type":"integer",
    "enum":[9007199254740993]}}})");
  OutputSchemaGraph graph;
  std::string error;
  Q4T_CHECK(graph.Init(schema, true, &error));
  Q4T_CHECK(graph.Check(Parse(R"({"v":9007199254740993})")));
  Q4T_CHECK(graph.Check(Parse(R"({"v":9007199254740993.0})")));
  Q4T_CHECK(!graph.Check(Parse(R"({"v":9007199254740992})")));
  for (const auto* ref :
       {"#/$defs/missing", "#/$defs/%q0", "https://host/schema", "#/$defs/a~2b",
        "#/required/00"}) {
    auto invalid = Schema(std::string(R"({"$ref":")") + ref + R"("})");
    Q4T_CHECK(!graph.Init(invalid, true, &error));
    Q4T_CHECK(error == R"(schema.properties.v["$ref"])");
    Q4T_CHECK(!graph.Check(Parse(R"({"v":1})")));
  }
  return true;
}

Q4T_TEST(schema_graph_numeric_intersections_are_exact) {
  const auto schema = Schema(R"({"type":"number","minimum":0.1,
    "exclusiveMaximum":0.5,"multipleOf":0.1})");
  OutputSchemaGraph graph;
  std::string error;
  Q4T_CHECK(graph.Init(schema, true, &error));
  for (const auto* value : {"0.1", "0.3", "4e-1"})
    Q4T_CHECK(graph.Check(Parse(std::string("{\"v\":") + value + '}')));
  for (const auto* value :
       {"0", "0.5", "0.30000000000000004", "0.09999999999999999"})
    Q4T_CHECK(!graph.Check(Parse(std::string("{\"v\":") + value + '}')));
  const auto adjacent =
      Schema(R"({"type":"number","minimum":1.0000000000000001})");
  Q4T_CHECK(graph.Init(adjacent, true, &error));
  Q4T_CHECK(!graph.Check(Parse(R"({"v":1})")));
  Q4T_CHECK(graph.Check(Parse(R"({"v":1.0000000000000001})")));
  return true;
}

Q4T_TEST(schema_graph_const_equality_and_annotations_do_not_fill_values) {
  const auto schema = Schema(R"({"anyOf":[{"type":"number"},{"type":"null"}],
    "const":1.0,"default":1,"examples":[1]})");
  OutputSchemaGraph graph;
  std::string error;
  Q4T_CHECK(graph.Init(schema, true, &error));
  Q4T_CHECK(graph.Check(Parse(R"({"v":1e0})")));
  Q4T_CHECK(!graph.Check(Parse(R"({"v":null})")));
  Q4T_CHECK(!graph.Check(Parse(R"({})")));
  auto duplicate = Schema(R"({"type":"number","enum":[1,1.0]})");
  Q4T_CHECK(!graph.Init(duplicate, true, &error));
  Q4T_CHECK(error == "schema.properties.v.enum[1]");
  const auto object = Schema(R"({"const":{"a":1,"b":[2]}})");
  Q4T_CHECK(graph.Init(object, true, &error));
  Q4T_CHECK(graph.Check(Parse(R"({"v":{"b":[2.0],"a":1.0}})")));
  Q4T_CHECK(!graph.Check(Parse(R"({"v":{"a":1,"b":[3]}})")));
  return true;
}

Q4T_TEST(schema_graph_lengths_count_unicode_and_require_string_checker) {
  const auto schema = Schema(R"({"type":"array","minItems":1,"maxItems":2e0,
    "items":{"type":"string","minLength":2,"maxLength":2}})");
  OutputSchemaGraph graph;
  std::string error;
  Q4T_CHECK(graph.Init(schema, true, &error));
  Q4T_CHECK(graph.Check(Parse(R"({"v":["中😀"]})")));
  Q4T_CHECK(!graph.Check(Parse(R"({"v":["中😀文"]})")));
  const auto constrained = Schema(R"({"type":"string","format":"uuid"})");
  Q4T_CHECK(graph.Init(constrained, true, &error));
  auto status = graph.Check(Parse(R"({"v":"not-a-uuid"})"));
  Q4T_CHECK(!status && status.message() == "schema string checker is required");
  const auto fractional =
      Schema(R"({"type":"array","minItems":2.0000000000000001,
    "items":{"type":"null"}})");
  Q4T_CHECK(!graph.Init(fractional, true, &error));
  return true;
}

Q4T_TEST(schema_graph_official_size_limits_and_reference_depth) {
  OutputSchemaGraph graph;
  std::string error;
  const auto object = [](size_t count) {
    std::string properties;
    for (size_t i = 0; i < count; ++i) {
      if (i) properties += ',';
      properties += "\"x" + std::to_string(i) + "\":{\"type\":\"null\"}";
    }
    return Parse("{\"type\":\"object\",\"properties\":{" + properties +
                 "},\"additionalProperties\":false}");
  };
  const auto allowed = object(5000), over = object(5001);
  Q4T_CHECK(graph.Init(allowed, false, &error));
  Q4T_CHECK(!graph.Init(over, false, &error));
  std::string nested = R"({"type":"null"})";
  for (int i = 0; i < 10; ++i) nested = q4t::server::OutputJson(Schema(nested));
  const auto ten = Parse(nested), eleven = Schema(nested);
  Q4T_CHECK(graph.Init(ten, true, &error));
  Q4T_CHECK(!graph.Init(eleven, true, &error));
  const auto alias = Parse(R"({"$ref":"#/$defs/root","$defs":{"root":
    {"type":"object","properties":{},"required":[],"additionalProperties":false}}})");
  Q4T_CHECK(graph.Init(alias, true, &error));
  Q4T_CHECK(graph.Check(Parse("{}")));
  return true;
}

Q4T_TEST(schema_graph_long_reference_chains_are_bounded_before_worker) {
  std::string definitions;
  for (int i = 0; i < 80; ++i) {
    if (i) definitions += ',';
    definitions += "\"n" + std::to_string(i) + "\":{\"$ref\":\"#/$defs/n" +
                   std::to_string(i + 1) + "\"}";
  }
  definitions += R"(,"n80":{"type":"object","properties":{},"required":[],"additionalProperties":false})";
  const auto root = Parse("{\"$ref\":\"#/$defs/n0\",\"$defs\":{" + definitions + "}}");
  const auto nested = Parse(R"({"type":"object","properties":{"v":{"$ref":"#/$defs/n0"}},
    "required":["v"],"additionalProperties":false,"$defs":{)" + definitions + "}}");
  for (const auto* schema : {&root, &nested}) {
    OutputSchemaGraph graph;
    std::string path;
    const auto status = graph.Init(*schema, true, &path);
    Q4T_CHECK(!status && status.message().starts_with("schema_resource_exhausted:"));
    Q4T_CHECK(!path.empty());
  }
  return true;
}
