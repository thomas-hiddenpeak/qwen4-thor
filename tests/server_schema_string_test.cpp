#include "../src/server/schema_string.h"
#include "../src/server/schema_idna.h"

#include <filesystem>
#include <fstream>
#include <iterator>
#include <stdexcept>

#include "q4t/server/schema_graph.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using namespace q4t::server;
Json Parse(const std::string& text) {
  Json value;
  const auto status = q4t::io::ParseJson(text, &value, true);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
bool Corpus(const std::string& format) {
  const auto file = std::filesystem::path(__FILE__).parent_path() / "fixtures" /
                    "schema_formats" / (format + ".json");
  std::ifstream input(file);
  if (!input)
    throw std::runtime_error("missing pinned format corpus: " + file.string());
  const std::string bytes{std::istreambuf_iterator<char>(input), {}};
  const auto groups = Parse(bytes);
  size_t count = 0, failures = 0;
  for (const auto& group : groups.array) {
    Q4T_CHECK(group.Find("schema")->GetString("format") == format);
    for (const auto& test : group.Find("tests")->array) {
      const auto* value = test.Find("data");
      Q4T_CHECK(value && test.Find("valid") && test.Find("valid")->IsBool());
      if (!value->IsString()) {
        Q4T_CHECK(test.GetBool("valid"));
        continue;  // Value type dispatch is checked separately through the
                   // graph.
      }
      ++count;
      if (SchemaFormatValid(format, value->str) != test.GetBool("valid")) {
        std::fprintf(stderr, "%s corpus mismatch: %s\n", format.c_str(),
                     test.GetString("description").c_str());
        ++failures;
      }
    }
  }
  std::fprintf(stderr, "%s: %zu external string cases, %zu failures\n",
               format.c_str(), count, failures);
  Q4T_CHECK(count > 0 && failures == 0);
  return true;
}
}  // namespace

Q4T_TEST(schema_string_format_date_corpus) { return Corpus("date"); }
Q4T_TEST(schema_string_format_datetime_corpus) { return Corpus("date-time"); }
Q4T_TEST(schema_string_format_time_corpus) { return Corpus("time"); }
Q4T_TEST(schema_string_format_duration_corpus) { return Corpus("duration"); }
Q4T_TEST(schema_string_format_email_corpus) { return Corpus("email"); }
Q4T_TEST(schema_string_format_hostname_corpus) { return Corpus("hostname"); }
Q4T_TEST(schema_string_format_ipv4_corpus) { return Corpus("ipv4"); }
Q4T_TEST(schema_string_format_ipv6_corpus) { return Corpus("ipv6"); }
Q4T_TEST(schema_string_format_uuid_corpus) { return Corpus("uuid"); }

Q4T_TEST(schema_string_idna_missing_and_resource_are_not_invalid_data) {
  bool missing = false, memory = false, backend = false;
  try {
    (void)CheckSchemaIdnaLabel("xn--ll-0ea", nullptr);
  } catch (const SchemaDependencyUnavailable&) {
    missing = true;
  }
  try {
    (void)CheckSchemaIdnaLabel(
        "xn--ll-0ea",
        [](const uint8_t*, const uint8_t*, uint8_t**, int) { return -100; });
  } catch (const std::bad_alloc&) {
    memory = true;
  }
  try {
    (void)CheckSchemaIdnaLabel("xn--ll-0ea", [](const uint8_t*, const uint8_t*,
                                                uint8_t**, int) { return -1; });
  } catch (const SchemaDependencyUnavailable&) {
    backend = true;
  }
  Q4T_CHECK(missing && memory && backend);
  Q4T_CHECK(!CheckSchemaIdnaLabel(
      "xn--ll-0ea",
      [](const uint8_t*, const uint8_t*, uint8_t**, int) { return -202; }));
  Q4T_CHECK(SchemaFormatValid("hostname", "example.com"));
  Q4T_CHECK(SchemaFormatValid("ipv4", "192.0.2.1"));
  return true;
}

Q4T_TEST(schema_string_format_nullable_graph_and_local_checks) {
  const auto schema = Parse(R"({"type":"object","properties":{"ip":{
    "type":["string","null"],"format":"ipv4"}},"required":["ip"],
    "additionalProperties":false})");
  OutputSchemaGraph graph;
  std::string path;
  Q4T_CHECK(graph.Init(schema, true, &path));
  Q4T_CHECK(graph.Check(Parse(R"({"ip":null})"), CheckSchemaString));
  Q4T_CHECK(graph.Check(Parse(R"({"ip":"192.0.2.1"})"), CheckSchemaString));
  Q4T_CHECK(!graph.Check(Parse(R"({"ip":"192.0.2.999"})"), CheckSchemaString));
  Q4T_CHECK(!SchemaFormatValid("unknown", "x"));
  Q4T_CHECK(!SchemaFormatValid("ipv4", std::string("192.0.2.1\0x", 11)));
  Q4T_CHECK(SchemaFormatValid("date-time", "2017-01-01T00:59:60+01:00"));
  Q4T_CHECK(!SchemaFormatValid("date-time", "2018-01-01T00:59:60+01:00"));
  size_t work = 3;
  bool accepted = true;
  const auto status = CheckSchemaString(Parse(R"({"format":"uuid"})"), "long",
                                        &work, &accepted);
  Q4T_CHECK(!status && !accepted &&
            status.message().starts_with("schema_resource_exhausted:"));
  return true;
}

#ifdef Q4T_ENABLE_XGRAMMAR
Q4T_TEST(schema_string_pattern_and_format_both_apply) {
  const auto schema = Parse(R"({"type":"object","properties":{"ip":{
    "type":"string","format":"ipv4","pattern":"^192\\.0\\.2\\.[0-9]+$"}},
    "required":["ip"],"additionalProperties":false})");
  OutputSchemaGraph graph;
  std::string path;
  Q4T_CHECK(graph.Init(schema, true, &path));
  Q4T_CHECK(graph.Check(Parse(R"({"ip":"192.0.2.1"})"), CheckSchemaString));
  Q4T_CHECK(!graph.Check(Parse(R"({"ip":"198.51.100.1"})"), CheckSchemaString));
  Q4T_CHECK(!graph.Check(Parse(R"({"ip":"192.0.2.999"})"), CheckSchemaString));
  size_t work = 1000000;
  bool accepted = true;
  auto status = CheckSchemaString(Parse(R"({"pattern":"[invalid"})"), "x",
                                  &work, &accepted);
  Q4T_CHECK(!status && !accepted &&
            status.message().starts_with("invalid schema pattern:"));
  return true;
}
#endif
