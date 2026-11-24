#include "q4t/server/evals.h"

#include <cstdlib>
#include <filesystem>

#include "../src/server/eval_grading.h"
#include "../src/server/eval_runs.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/files.h"
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

Q4T_TEST(evals_string_grader_resolves_dataset_once_and_folds_unicode) {
  const auto row = Object(
      {{"sample", Object({{"output_text", String("Straße 157")}})},
       {"item", Object({{"stats", Array({Number(157)})},
                        {"literal", String("{{sample.output_text}}")}})}});
  auto check = [&](std::string operation, std::string input,
                   std::string reference, bool expected) {
    size_t remaining = 4096;
    auto result =
        eval_detail::GradeString(Object({{"name", String("Check")},
                                         {"input", String(input)},
                                         {"operation", String(operation)},
                                         {"reference", String(reference)}}),
                                 row, &remaining);
    return result.ok() && result.value.GetBool("passed") == expected &&
           result.value.GetInt("score") == (expected ? 1 : 0);
  };
  Q4T_CHECK(check("eq", "{{ item.stats.0 }}", "157", true));
  Q4T_CHECK(check("ne", "{{item.literal}}", "{{sample.output_text}}", true));
  Q4T_CHECK(check("like", "{{sample.output_text}}", "STRASSE", false));
  Q4T_CHECK(check("ilike", "{{sample.output_text}}", "STRASSE {{item.stats.0}}",
                  true));
  Q4T_CHECK(check("like", "100% reviewed", "%", true));
  Q4T_CHECK(check("eq", "x ", "x", false));
  Q4T_CHECK(check("like", "x", "", true));
  return true;
}

Q4T_TEST(evals_text_budget_and_missing_fields_never_publish_partial_text) {
  auto row = Object({{"item", Object({{"text", String("1234")}})}});
  for (const std::string name :
       {"{{item.missing}}", "{{other}}", "{{item.text", "{{item..text}}"}) {
    size_t budget = 128;
    std::string out = "previous";
    auto r = eval_detail::RenderText(name, row, "reference", &budget, &out);
    Q4T_CHECK(r.http_status == 400 &&
              r.value.GetString("param") == "reference");
    Q4T_CHECK(out == "previous");
  }
  size_t budget = 4;
  std::string out;
  auto full =
      eval_detail::RenderText("{{item.text}}", row, "input", &budget, &out);
  Q4T_CHECK(full.ok() && budget == 0 && out == "1234");
  auto overflow = eval_detail::RenderText("x", row, "input", &budget, &out);
  Q4T_CHECK(overflow.http_status == 413 && out == "1234");
  return true;
}

Q4T_TEST(evals_runs_persist_real_pass_fail_outputs_and_filter_by_parent) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  auto definition = Request(store, "POST", "/v1/evals", kDefinition);
  Q4T_CHECK(definition.ok());
  const auto eval = "/v1/evals/" + definition.value.GetString("id");
  const auto collection = eval + "/runs";
  const std::string body =
      R"({"name":"Project record","data_source":{"type":"jsonl","source":{
    "type":"file_content","content":[{"item":{"count":157},"sample":{"output_text":"157"}},
    {"item":{"count":88},"sample":{"output_text":"87"}}]}}})";
  auto created = Request(store, "POST", collection, body);
  if (!created.ok())
    std::fprintf(stderr, "eval run create %d %s: %s (%s)\n", created.http_status,
                 created.code.c_str(), created.message.c_str(),
                 created.value.GetString("param").c_str());
  Q4T_CHECK(created.ok() && created.http_status == 201);
  const auto id = created.value.GetString("id");
  const auto path = collection + "/" + id;
  Q4T_CHECK(created.value.GetString("status") == "queued");
  Q4T_CHECK(Request(store, "GET", path, {}, "client:beta").http_status == 404);
  const auto before = Request(store, "GET", collection + "?status=queued");
  Q4T_CHECK(before.ok() && before.value.GetArray("data")->array.size() == 1);
  StateAccess access{
      "client:alpha", std::chrono::steady_clock::now() + 10s, {}};
  ObjectRequest read;
  read.kind = ObjectKind::kEvalRun;
  read.scope = access.scope;
  read.id = id;
  for (int turn = 0; turn < 4; ++turn) {
    auto record = store.Execute(read, access.deadline);
    Q4T_CHECK(record.ok());
    auto step = eval_detail::AdvanceRun(store, access, record.value);
    Q4T_CHECK(step.ok());
    if (turn == 1) {
      auto stale = eval_detail::AdvanceRun(store, access, record.value);
      Q4T_CHECK(stale.http_status == 409);
    }
  }
  auto final = Request(store, "GET", path);
  Q4T_CHECK(final.ok() && final.value.GetString("status") == "completed");
  const auto& counts = Field(final.value, "result_counts");
  Q4T_CHECK(counts.GetInt("passed") == 1 && counts.GetInt("failed") == 1 &&
            counts.GetInt("total") == 2 && counts.GetInt("errored") == 0);
  auto outputs = Request(store, "GET", path + "/output_items?limit=1");
  Q4T_CHECK(outputs.ok() && outputs.value.GetBool("has_more"));
  const auto first = outputs.value.GetString("last_id");
  auto item = Request(store, "GET", path + "/output_items/" + first);
  Q4T_CHECK(item.ok() && item.value.GetString("status") == "pass" &&
            item.value.Find("datasource_item")->GetInt("count") == 157);
  auto fails = Request(store, "GET",
                       path + "/output_items?after=" + first + "&status=fail");
  Q4T_CHECK(fails.ok() && fails.value.GetArray("data")->array.size() == 1);
  const auto& failed = fails.value.GetArray("data")->array[0];
  Q4T_CHECK(failed.GetInt("datasource_item_id") == 1 &&
            failed.GetArray("results")->array[0].GetInt("score") == 0);
  Q4T_CHECK(Request(store, "POST", path + "/cancel").http_status == 409);
  auto another = Request(store, "POST", collection, body);
  Q4T_CHECK(another.ok());
  auto second = collection + "/" + another.value.GetString("id");
  Q4T_CHECK(
      Request(store, "GET", second + "/output_items/" + first).http_status ==
      404);
  Q4T_CHECK(Request(store, "GET", second + "/output_items?after=" + first)
                .http_status == 400);
  auto cancel = Request(store, "POST", second + "/cancel");
  Q4T_CHECK(cancel.ok() && cancel.value.GetString("status") == "canceled");
  Q4T_CHECK(Request(store, "POST", second + "/cancel").ok());
  auto gone = Request(store, "DELETE", path);
  Q4T_CHECK(gone.ok() && gone.value.GetString("run_id") == id);
  Q4T_CHECK(Request(store, "GET", path).http_status == 404);
  read.kind = ObjectKind::kEvalOutput;
  read.id = first;
  Q4T_CHECK(store.Execute(read, access.deadline).http_status == 404);
  Q4T_CHECK(Request(store, "DELETE", eval).ok());
  read.kind = ObjectKind::kEvalRun;
  read.id = another.value.GetString("id");
  Q4T_CHECK(store.Execute(read, access.deadline).http_status == 404);
  return true;
}

Q4T_TEST(evals_file_run_keeps_its_input_after_file_deletion_and_reopen) {
  Fixture fixture;
  std::string eval, run, id, file;
  auto deadline = [] { return std::chrono::steady_clock::now() + 10s; };
  {
    ObjectStore store(fixture.options);
    auto def = Request(store, "POST", "/v1/evals", kDefinition);
    Q4T_CHECK(def.ok());
    eval = "/v1/evals/" + def.value.GetString("id");
    BinaryAccess access{"client:alpha", deadline(), {}};
    FileUpload upload(store, access, "multipart/form-data; boundary=f");
    const std::string wire =
        "--f\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nevals"
        "\r\n--f\r\nContent-Disposition: form-data; name=\"file\"; "
        "filename=\"records.jsonl\"\r\n\r\n"
        "{\"item\":{\"count\":157},\"sample\":{\"output_text\":\"157\"}}\r\n"
        "{\"item\":{\"count\":88},\"sample\":{\"output_text\":\"88\"}}\r\n--f--"
        "\r\n";
    Q4T_CHECK(upload.Feed(wire, deadline()).ok());
    auto done = upload.Finish();
    Q4T_CHECK(done.ok());
    file = done.value.GetString("id");
    auto body = OutputJson(
        Object({{"data_source",
                 Object({{"type", String("jsonl")},
                         {"source", Object({{"type", String("file_id")},
                                            {"id", String(file)}})}})}}));
    auto foreign =
        Request(store, "POST", "/v1/evals", kDefinition, "client:beta");
    Q4T_CHECK(foreign.ok());
    const auto foreign_path =
        "/v1/evals/" + foreign.value.GetString("id") + "/runs";
    Q4T_CHECK(
        Request(store, "POST", foreign_path, body, "client:beta").http_status ==
        404);
    auto created = Request(store, "POST", eval + "/runs", body);
    Q4T_CHECK(created.ok());
    id = created.value.GetString("id");
    run = eval + "/runs/" + id;
    Q4T_CHECK(Files(store).Erase(access, file).ok());
    auto edit =
        Request(store, "POST", eval, R"({"metadata":{"review":"complete"}})");
    Q4T_CHECK(edit.ok());
    Q4T_CHECK(Request(store, "GET", eval + "/runs")
                  .value.GetArray("data")
                  ->array.size() == 1);
  }
  ObjectStore reopened(fixture.options);
  ObjectRequest read;
  read.scope = "client:alpha";
  read.kind = ObjectKind::kEvalRun;
  read.id = id;
  StateAccess access{read.scope, deadline(), {}};
  for (int step = 0; step < 4; ++step) {
    auto record = reopened.Execute(read, deadline());
    Q4T_CHECK(record.ok());
    auto result = eval_detail::AdvanceRun(reopened, access, record.value);
    Q4T_CHECK(result.ok());
  }
  auto finished = Request(reopened, "GET", run);
  Q4T_CHECK(finished.ok() && finished.value.GetString("status") == "completed");
  Q4T_CHECK(finished.value.Find("result_counts")->GetInt("passed") == 2);
  auto removed = Request(reopened, "DELETE", eval);
  Q4T_CHECK(removed.ok() &&
            reopened.Execute(read, deadline()).http_status == 404);
  return true;
}

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
