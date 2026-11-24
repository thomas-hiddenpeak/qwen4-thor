#include "q4t/server/evals.h"

#include <cstdlib>
#include <filesystem>

#include "../src/server/object_protocol.h"
#include "q4t/server/http_connection.h"
#include "q4t/test.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;

struct Fixture {
  std::filesystem::path directory;
  ObjectStoreOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto base = std::filesystem::absolute(output ? output : ".q4t-work/evals");
    std::filesystem::create_directories(base);
    auto pattern = (base / "eval-XXXXXX").string();
    if (!mkdtemp(pattern.data())) throw std::runtime_error("Eval fixture");
    directory = pattern;
    options.directory = (directory / "objects").string();
  }
  ~Fixture() { std::filesystem::remove_all(directory); }
};

ObjectReply Request(ObjectStore& store, std::string_view method,
                    std::string path, std::string body = {},
                    std::string scope = "client:alpha") {
  StateAccess access{
      std::move(scope), std::chrono::steady_clock::now() + 10s, {}};
  return HandleEval(store, access, method, path, body);
}

const char* kDefinition = R"({
  "name":"Scope report",
  "data_source_config":{"type":"custom","item_schema":{
    "type":"object","properties":{"count":{"type":"integer"}},"required":["count"]},
    "include_sample_schema":true},
  "testing_criteria":[{"name":"Count check","type":"string_check","input":"{{sample.output_text}}",
    "operation":"eq","reference":"{{item.count}}"}],
  "metadata":{"project":"q4t"}})";

Q4T_TEST(evals_definitions_reopen_isolate_update_and_delete) {
  Fixture fixture;
  std::string path;
  {
    ObjectStore objects(fixture.options);
    auto created = Request(objects, "POST", "/v1/evals", kDefinition);
    Q4T_CHECK(created.ok() && created.http_status == 201);
    const auto id = created.value.GetString("id");
    Q4T_CHECK(id.starts_with("eval_"));
    path = "/v1/evals/" + id;
    const auto& source = Field(created.value, "data_source_config");
    const auto* schema = source.Find("schema");
    Q4T_CHECK(schema && schema->GetArray("required")->array.size() == 2);
    Q4T_CHECK(schema->Find("properties")
                  ->Find("item")
                  ->Find("properties")
                  ->Has("count"));
    Q4T_CHECK(schema->Find("properties")
                  ->Find("sample")
                  ->Find("properties")
                  ->Has("choices"));
  }
  ObjectStore objects(fixture.options);
  auto prior = Request(objects, "GET", path);
  Q4T_CHECK(prior.ok() && prior.value.GetString("name") == "Scope report");
  for (const char* verb : {"GET", "POST", "DELETE"})
    Q4T_CHECK(Request(objects, verb, path,
                      std::string_view(verb) == "POST" ? "{}" : "",
                      "client:other")
                  .http_status == 404);
  auto updated = Request(objects, "POST", path,
                         R"({"name":"Published scope","metadata":null})");
  Q4T_CHECK(updated.ok() && updated.value.Find("metadata")->IsNull());
  Q4T_CHECK(updated.value.GetInt("created_at") ==
            prior.value.GetInt("created_at"));
  Q4T_CHECK(OutputJson(*updated.value.Find("testing_criteria")) ==
            OutputJson(*prior.value.Find("testing_criteria")));
  auto removed = Request(objects, "DELETE", path);
  Q4T_CHECK(removed.ok() && removed.value.GetBool("deleted") &&
            removed.value.GetString("eval_id") == prior.value.GetString("id"));
  Q4T_CHECK(Request(objects, "GET", path).http_status == 404);
  return true;
}

Q4T_TEST(evals_list_orders_by_actual_update_and_scopes_cursors) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto a = Request(objects, "POST", "/v1/evals", kDefinition);
  auto b = Request(objects, "POST", "/v1/evals", kDefinition);
  Q4T_CHECK(a.ok() && b.ok());
  const auto first = a.value.GetString("id"), second = b.value.GetString("id");
  Q4T_CHECK(
      Request(objects, "POST", "/v1/evals/" + first, R"({"name":"new"})").ok());
  auto page = Request(objects, "GET", "/v1/evals?limit=1");
  Q4T_CHECK(page.ok() && page.value.GetString("first_id") == first &&
            page.value.GetBool("has_more"));
  page = Request(objects, "GET", "/v1/evals?order_by=updated_at&limit=1");
  Q4T_CHECK(page.ok() && page.value.GetString("last_id") == second &&
            page.value.GetBool("has_more"));
  page =
      Request(objects, "GET", "/v1/evals?order_by=updated_at&after=" + second);
  Q4T_CHECK(page.ok() && page.value.GetString("first_id") == first &&
            !page.value.GetBool("has_more"));
  page = Request(objects, "GET", "/v1/evals?order=desc&order_by=updated_at");
  Q4T_CHECK(page.ok() && page.value.GetString("first_id") == first);
  page = Request(objects, "GET", "/v1/evals", "", "client:empty");
  Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.empty() &&
            page.value.Find("last_id")->IsString());
  Q4T_CHECK(
      Request(objects, "GET", "/v1/evals?after=" + first, "", "client:empty")
          .http_status == 400);
  return true;
}

Q4T_TEST(evals_definitions_validate_http_grader_schema_and_sources) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  for (
      const char* body :
      {R"({"data_source_config":{"type":"custom"},"testing_criteria":[]})",
       R"({"data_source_config":{"type":"custom","item_schema":{"type":"invalid"}},"testing_criteria":[]})",
       R"({"data_source_config":{"type":"logs"},"testing_criteria":[{"type":"string_check"}]})",
       R"({"data_source_config":{"type":"logs","metadata":{"x":12}},"testing_criteria":[]})",
       R"({"data_source_config":{"type":"logs"},"testing_criteria":[],"extra":true})"})
    Q4T_CHECK(Request(objects, "POST", "/v1/evals", body).http_status == 400);
  for (const char* type : {"logs", "stored_completions"}) {
    auto body = Object(
        {{"data_source_config",
          Object({{"type", String(type)},
                  {"metadata", Object({{"project", String("scope")}})}})},
         {"testing_criteria", Array()}});
    auto created = Request(objects, "POST", "/v1/evals", OutputJson(body));
    Q4T_CHECK(created.ok());
    const auto& config = Field(created.value, "data_source_config");
    Q4T_CHECK(config.GetString("type") == type &&
              config.Find("schema")->Find("properties")->Has("sample"));
    Q4T_CHECK(config.Find("metadata")->GetString("project") == "scope");
  }
  for (const char* query : {"limit=-1", "order_by=invented", "after=x&after=y",
                            "limit=3.1", "unexpected=1"})
    Q4T_CHECK(Request(objects, "GET", std::string("/v1/evals?") + query)
                  .http_status == 400);
  Q4T_CHECK(Request(objects, "DELETE", "/v1/evals").http_status == 405);
  Q4T_CHECK(!IsEvalTarget("/v1/evals/eval_one/another") &&
            !IsEvalTarget("/v1/evals/eval_one#bad"));
  Q4T_CHECK(IsEvalObservation("GET", "/v1/evals") &&
            !IsEvalObservation("POST", "/v1/evals"));
  return true;
}
}  // namespace
}  // namespace q4t::server
