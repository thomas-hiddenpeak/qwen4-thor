#include "q4t/server/prompt_registry.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>

#include "q4t/server/chat_prompt.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_execution.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("prompt fixture JSON");
  return value;
}
void Set(Json* object, const std::string& key, Json value) {
  for (auto& [name, field] : object->object)
    if (name == key) {
      field = std::move(value);
      return;
    }
  object->object.emplace_back(key, std::move(value));
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s %s %s\n", result.code.c_str(),
                 result.message.c_str(),
                 result.value.GetString("param").c_str());
  return result.ok();
}
StateAccess Access(std::string scope = "client:prompt-a") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
ChatPlanConfig Config() {
  ChatPlanConfig c;
  c.model = "cpu";
  return c;
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(env ? env : ".q4t-work/prompt-cpu");
    std::filesystem::create_directories(root);
    auto path = (root / "prompts-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("prompt fixture path");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
Json Definition(std::string value = "{{value}}") {
  auto definition =
      Parse(R"({"request":{"instructions":"Answer {{name}} exactly.",
    "input":[{"role":"user","content":""}],"reasoning":{"effort":"none"},
    "temperature":0},"variables":{"name":"string","value":"string"}})");
  auto input = *definition.Find("request")->Find("input");
  Json text;
  text.type = Json::Type::kString;
  text.str = std::move(value);
  Set(&input.array[0], "content", text);
  auto request = *definition.Find("request");
  Set(&request, "input", input);
  Set(&definition, "request", request);
  return definition;
}
Json Request(const std::string& id, std::string extra = "") {
  return Parse(
      "{\"prompt\":{\"id\":\"" + id +
      R"(","variables":{"name":"reader","value":"42"}},"input":"current")" +
      extra + "}");
}
void Record(const Json& value) {
  const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!env) return;
  std::filesystem::create_directories(std::filesystem::path(env) / "fixtures");
  std::ofstream stream(
      std::filesystem::path(env) / "fixtures/prompt-wire.jsonl", std::ios::app);
  stream << OutputJson(value) << '\n';
}
ObjectReply Api(ObjectStore& store, std::string method, std::string path,
                Json body = Json(), StateAccess access = Access()) {
  return HandlePromptRequest(store, method, path,
                             body.IsNull() ? "" : OutputJson(body), Config(),
                             access);
}
}  // namespace

Q4T_TEST(prompt_versions_are_immutable_and_default_selection_is_cas) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto first = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(first));
  Record(first.value);
  const auto id = first.value.GetString("id");
  Q4T_CHECK(first.value.GetInt("revision") == 1);
  auto original = registry.Retrieve(id, "1", Access());
  Q4T_CHECK(Ok(original));
  Record(original.value);
  auto second =
      registry.Append(id, 1, Definition("new {{value}}"), Config(), Access());
  Q4T_CHECK(Ok(second) && second.value.GetString("default_version") == "2");
  Q4T_CHECK(
      registry.Append(id, 1, Definition(), Config(), Access()).http_status ==
      409);
  Q4T_CHECK(OutputJson(registry.Retrieve(id, "1", Access()).value) ==
            OutputJson(original.value));
  Q4T_CHECK(registry.Select(id, 1, "1", Access()).http_status == 409);
  auto selected = registry.Select(id, 2, "1", Access());
  Q4T_CHECK(Ok(selected) && selected.value.GetInt("revision") == 3);
  Q4T_CHECK(registry.Select(id, 3, "99", Access()).http_status == 404);
  Q4T_CHECK(registry.Select(id, 3, "1", Access()).value.GetInt("revision") ==
            3);
  auto expanded = registry.Expand(Request(id), Config(), Access());
  Q4T_CHECK(Ok(expanded) &&
            expanded.value.Find("prompt")->GetString("version") == "1");
  return true;
}

Q4T_TEST(prompt_concurrent_append_has_one_committed_version) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto first = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(first));
  const auto id = first.value.GetString("id");
  ObjectReply left, right;
  std::jthread a([&] {
    left =
        registry.Append(id, 1, Definition("a {{value}}"), Config(), Access());
  });
  std::jthread b([&] {
    right =
        registry.Append(id, 1, Definition("b {{value}}"), Config(), Access());
  });
  a.join();
  b.join();
  Q4T_CHECK(left.ok() != right.ok());
  Q4T_CHECK((left.ok() ? right : left).http_status == 409);
  auto read = registry.Retrieve(id, "", Access());
  Q4T_CHECK(Ok(read));
  Q4T_CHECK(read.value.GetInt("revision") == 2 &&
            read.value.GetArray("versions")->array.size() == 2);
  return true;
}

Q4T_TEST(prompt_scope_isolates_lookup_mutation_and_pagination) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto one = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(one));
  auto two = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(two));
  const auto id = one.value.GetString("id");
  auto other = Access("client:prompt-b");
  Q4T_CHECK(registry.Retrieve(id, "", other).http_status == 404);
  Q4T_CHECK(registry.Expand(Request(id), Config(), other).http_status == 404);
  Q4T_CHECK(registry.Append(id, 1, Definition(), Config(), other).http_status ==
            404);
  Q4T_CHECK(registry.Select(id, 1, "1", other).http_status == 404);
  Q4T_CHECK(registry.Erase(id, other).http_status == 404);
  ObjectRequest page;
  page.limit = 1;
  page.descending = false;
  auto start = registry.List(page, Access());
  Q4T_CHECK(Ok(start));
  Record(start.value);
  Q4T_CHECK(start.value.GetBool("has_more") &&
            start.value.GetArray("data")->array.size() == 1);
  page.after = start.value.GetString("last_id");
  auto end = registry.List(page, Access());
  Q4T_CHECK(Ok(end));
  Q4T_CHECK(!end.value.GetBool("has_more") &&
            end.value.GetString("first_id") != page.after);
  page.after.clear();
  auto empty = registry.List(page, other);
  Q4T_CHECK(Ok(empty) && empty.value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(prompt_substitution_is_single_pass_and_preserves_typed_media) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto definition = Parse(R"({"request":{"instructions":"Say {{label}}",
    "input":[{"role":"user","content":[{"type":"input_text","text":"Read {{text}} {{photo}} {{doc}}"}]}]},
    "variables":{"label":"string","text":"input_text","photo":"input_image","doc":"input_file"}})");
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  auto request =
      Parse("{\"prompt\":{\"id\":\"" + created.value.GetString("id") + R"(",
    "variables":{"label":"{{text}} \\\n\"literal\"","text":{"type":"input_text","text":"{{label}}"},
    "photo":{"type":"input_image","image_url":"data:image/png;base64,AA==","detail":"low"},
    "doc":{"type":"input_file","file_id":"file_real"}}}})");
  auto expanded = registry.Expand(request, Config(), Access());
  Q4T_CHECK(Ok(expanded));
  Q4T_CHECK(expanded.value.GetString("instructions").find("{{text}}") !=
            std::string::npos);
  const auto& content =
      expanded.value.GetArray("input")->array[0].GetArray("content")->array;
  Q4T_CHECK(content.size() == 6 && content[1].GetString("text") == "{{label}}");
  Q4T_CHECK(content[3].GetString("image_url") == "data:image/png;base64,AA==");
  Q4T_CHECK(content[5].GetString("file_id") == "file_real");
  auto bad = request;
  auto ref = *bad.Find("prompt");
  auto vars = *ref.Find("variables");
  Set(&vars, "text", Parse("42"));
  Set(&ref, "variables", vars);
  Set(&bad, "prompt", ref);
  Q4T_CHECK(registry.Expand(bad, Config(), Access()).http_status == 400);
  return true;
}

Q4T_TEST(prompt_missing_extra_and_media_instruction_variables_are_rejected) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  for (const auto* values : {"null", "[]", "{}", R"({"name":"n","value":42})",
                             R"({"name":"n","value":"v","extra":"x"})"}) {
    auto request = Request(id);
    auto reference = *request.Find("prompt");
    Set(&reference, "variables", Parse(values));
    Set(&request, "prompt", reference);
    Q4T_CHECK(registry.Expand(request, Config(), Access()).http_status == 400);
  }
  auto media = Parse(
      R"({"request":{"instructions":"{{image}}"},"variables":{"image":"input_image"}})");
  Q4T_CHECK(registry.Create(media, Config(), Access()).http_status == 400);
  auto unused = Parse(
      R"({"request":{"instructions":"literal"},"variables":{"unused":"string"}})");
  Q4T_CHECK(registry.Create(unused, Config(), Access()).http_status == 400);
  return true;
}

Q4T_TEST(
    prompt_large_file_variable_preserves_source_without_inline_copy_limit) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto definition = Parse(R"({"request":{"input":[{"role":"user",
    "content":"Read {{file}}"}]},"variables":{"file":"input_file"}})");
  const auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  std::string encoded(4 * ((50000000ULL + 2) / 3), 'A');
  encoded.back() = '=';
  auto request =
      Parse("{\"prompt\":{\"id\":\"" + created.value.GetString("id") +
            R"(","variables":{"file":{
        "type":"input_file","filename":"capacity.bin","file_data":")" +
            encoded + R"("}}}})");
  const auto result = registry.Expand(request, Config(), Access());
  Q4T_CHECK(Ok(result));
  const auto& content =
      result.value.GetArray("input")->array[0].GetArray("content")->array;
  Q4T_CHECK(content.size() == 2 &&
            content[1].GetString("file_data") == encoded);
  Q4T_CHECK(content[1].GetString("filename") == "capacity.bin");
  return true;
}

Q4T_TEST(prompt_text_substitution_preserves_exact_cache_boundary) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto definition = Parse(R"({"request":{"input":[{"role":"user","content":[
    {"type":"input_text","text":"start {{x}} end","prompt_cache_breakpoint":{"mode":"explicit","ttl":"12h"}}]}]},
    "variables":{"x":"input_text"}})");
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  auto request = Parse(
      "{\"prompt\":{\"id\":\"" + created.value.GetString("id") +
      R"(","variables":{"x":{"type":"input_text","text":"value","prompt_cache_breakpoint":{"mode":"explicit","ttl":"30m"}}}}})");
  auto expanded = registry.Expand(request, Config(), Access());
  Q4T_CHECK(Ok(expanded));
  const auto& parts =
      expanded.value.GetArray("input")->array[0].GetArray("content")->array;
  Q4T_CHECK(parts.size() == 3 && !parts[0].Has("prompt_cache_breakpoint"));
  Q4T_CHECK(parts[1].Find("prompt_cache_breakpoint")->GetString("ttl") ==
            "30m");
  Q4T_CHECK(parts[2].Find("prompt_cache_breakpoint")->GetString("ttl") ==
            "12h");
  return true;
}

Q4T_TEST(prompt_version_capacity_and_cancel_preserve_existing_state) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  for (int64_t version = 1; version < 64; ++version)
    Q4T_CHECK(
        Ok(registry.Append(id, version, Definition(), Config(), Access())));
  auto before = registry.Retrieve(id, "", Access());
  Q4T_CHECK(Ok(before));
  Q4T_CHECK(registry.Append(id, 64, Definition(), Config(), Access()).code ==
            "prompt_version_capacity");
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(!registry.Select(id, 64, "1", cancelled).ok());
  auto expired = Access();
  expired.deadline = Clock::now() - 1ms;
  Q4T_CHECK(!registry.Select(id, 64, "1", expired).ok());
  auto after = registry.Retrieve(id, "", Access());
  Q4T_CHECK(Ok(after));
  Q4T_CHECK(OutputJson(before.value) == OutputJson(after.value));
  return true;
}

Q4T_TEST(prompt_definition_cannot_set_authority_or_rewrite_message_roles) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  for (const auto* field : {"store", "background", "conversation", "prompt",
                            "client_id", "request_timeout_ms"}) {
    auto definition = Definition();
    auto request = *definition.Find("request");
    Set(&request, field, Parse("null"));
    Set(&definition, "request", request);
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  auto role = Parse(
      R"({"request":{"input":[{"role":"{{role}}","content":"x"}]},"variables":{"role":"string"}})");
  Q4T_CHECK(registry.Create(role, Config(), Access()).http_status == 400);
  auto text_extra = Parse(
      R"({"request":{"input":[{"role":"user","content":[{"type":"input_text","text":"{{x}}","unknown":1}]}]},"variables":{"x":"string"}})");
  Q4T_CHECK(registry.Create(text_extra, Config(), Access()).http_status == 400);
  return true;
}

Q4T_TEST(prompt_effective_request_uses_overrides_and_actual_context) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  auto request =
      Request(created.value.GetString("id"),
              R"(,"instructions":"override","temperature":0.5,"store":false)");
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
  Q4T_CHECK(run.Envelope().options.GetString("instructions") == "override");
  Q4T_CHECK(run.Envelope().options.Find("temperature")->number == 0.5);
  Q4T_CHECK(!run.Envelope().store);
  const auto& chat = run.Generation().generation_request;
  std::string prompt, param, code;
  std::vector<VisionItem> media;
  Q4T_CHECK(PrepareChatPrompt(run.Generation().chat.Request(chat),
                              run.Generation().chat.constraints, &prompt,
                              &media, &param, &code));
  Q4T_CHECK(prompt.find("override") != prompt.npos &&
            prompt.find("42") != prompt.npos &&
            prompt.find("current") != prompt.npos);
  Q4T_CHECK(prompt.find("Answer reader") == prompt.npos &&
            prompt.find("{{") == prompt.npos);
  Q4T_CHECK(run.Envelope().options.Find("prompt")->GetString("version") == "1");
  const auto* reference = run.Output().Snapshot().Find("prompt");
  Q4T_CHECK(reference && OutputJson(*reference) ==
                            OutputJson(*run.Envelope().options.Find("prompt")));
  return true;
}

Q4T_TEST(prompt_expansion_cannot_bypass_client_output_or_input_bounds) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto definition = Definition();
  auto req = *definition.Find("request");
  Set(&req, "max_output_tokens", Parse("128"));
  Set(&definition, "request", req);
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  auto config = Config();
  ClientLimits limits;
  limits.max_output_tokens = 8;
  config.client_limits = &limits;
  ResponsesExecution run(store);
  auto result =
      run.Prepare(Request(created.value.GetString("id")), config, Access(), 42);
  Q4T_CHECK(result.http_status == 400 &&
            result.code == "client_output_limit_exceeded");
  auto override =
      Request(created.value.GetString("id"), R"(,"max_output_tokens":8)");
  ResponsesExecution allowed(store);
  Q4T_CHECK(Ok(allowed.Prepare(override, config, Access(), 42)));
  auto too_large = Definition(std::string(262144, 'x') + "{{value}}");
  Q4T_CHECK(registry.Create(too_large, Config(), Access()).http_status == 413);
  std::string repeated;
  for (int i = 0; i < 512; ++i) repeated += "{{value}}";
  auto amplified = registry.Create(Definition(repeated), Config(), Access());
  Q4T_CHECK(Ok(amplified));
  auto big = Request(amplified.value.GetString("id"));
  auto ref = *big.Find("prompt");
  auto vars = *ref.Find("variables");
  Json text;
  text.type = Json::Type::kString;
  text.str.assign(400000, 'x');
  Set(&vars, "value", text);
  Set(&ref, "variables", vars);
  Set(&big, "prompt", ref);
  Q4T_CHECK(registry.Expand(big, Config(), Access()).code ==
            "prompt_expansion_limit");
  return true;
}

Q4T_TEST(
    prompt_queue_restores_frozen_version_after_registry_change_and_delete) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  ResponsesExecution queued(store);
  Q4T_CHECK(
      Ok(queued.Prepare(Request(id, R"(,"background":true,"stream":true)"),
                        Config(), Access(), 42, true)));
  const auto original = OutputJson(queued.Generation().generation_request);
  std::string wire;
  Q4T_CHECK(Ok(queued.Begin(&wire)));
  Q4T_CHECK(Ok(registry.Append(id, 1, Definition("changed {{value}}"), Config(),
                               Access())));
  Q4T_CHECK(Ok(registry.Erase(id, Access())));
  ResponsesExecution restored(store);
  Q4T_CHECK(Ok(restored.RestoreQueued(queued.Id(), Config(), Access())));
  Q4T_CHECK(OutputJson(restored.Generation().generation_request) == original);
  Q4T_CHECK(restored.Envelope().options.Find("prompt")->GetString("version") ==
            "1");
  Q4T_CHECK(Ok(restored.StartRunning(&wire)));
  ResponseDelta text;
  text.kind = ResponseDelta::Kind::kContent;
  text.text = "42";
  Q4T_CHECK(restored.Append(text, "null", &wire));
  Q4T_CHECK(Ok(restored.Finish(false, 0, {12, 0, 2, 0}, 43, &wire)));
  const auto* reference = restored.Output().Snapshot().Find("prompt");
  Q4T_CHECK(reference && reference->GetString("id") == id &&
            reference->GetString("version") == "1");
  Record(restored.Output().Snapshot());
  auto record = ResponsesState(store).ReadRecord(queued.Id(), Access());
  Q4T_CHECK(Ok(record));
  Q4T_CHECK(record.value.Find("request")->Find("prompt")->GetString(
                "version") == "1");
  Q4T_CHECK(record.value.GetArray("history")->array.size() == 2);
  for (const auto& event : record.value.GetArray("events")->array) {
    if (const auto* response = event.Find("response")) {
      Q4T_CHECK(response->Find("prompt") &&
                OutputJson(*response->Find("prompt")) == OutputJson(*reference));
    }
    Record(event);
  }
  return true;
}

Q4T_TEST(prompt_registry_restart_preserves_versions_and_default) {
  Fixture f;
  std::string id, previous;
  {
    ObjectStore store(f.options);
    PromptRegistry registry(store);
    auto created = registry.Create(Definition(), Config(), Access());
    Q4T_CHECK(Ok(created));
    id = created.value.GetString("id");
    Q4T_CHECK(Ok(registry.Append(id, 1, Definition("v2 {{value}}"), Config(),
                                 Access())));
    previous = OutputJson(registry.Retrieve(id, "1", Access()).value);
  }
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto read = registry.Retrieve(id, "", Access());
  Q4T_CHECK(Ok(read));
  Q4T_CHECK(read.value.GetString("default_version") == "2");
  Q4T_CHECK(OutputJson(registry.Retrieve(id, "1", Access()).value) == previous);
  auto expanded = registry.Expand(Request(id), Config(), Access());
  Q4T_CHECK(Ok(expanded));
  Q4T_CHECK(OutputJson(expanded.value).find("v2 ") != std::string::npos);
  return true;
}

Q4T_TEST(prompt_storage_failure_does_not_block_plain_stateless_generation) {
  Fixture f;
  f.options.directory = "/proc/q4t-prompts-no-storage";
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto failed = registry.Expand(Request("prompt_none"), Config(), Access());
  Q4T_CHECK(!failed.ok());
  ResponsesExecution plain(store);
  Q4T_CHECK(Ok(plain.Prepare(
      Parse(
          R"({"input":"ordinary","store":false,"reasoning":{"effort":"none"}})"),
      Config(), Access(), 42)));
  return true;
}

Q4T_TEST(prompt_api_enforces_routes_revision_body_and_query) {
  Fixture f;
  ObjectStore store(f.options);
  Json body = Parse("{}");
  Set(&body, "definition", Definition());
  auto created = Api(store, "POST", "/v1/prompts", body);
  Q4T_CHECK(Ok(created));
  const auto path = "/v1/prompts/" + created.value.GetString("id");
  Q4T_CHECK(Api(store, "POST", path + "/versions", body).http_status == 400);
  Set(&body, "expected_revision", Parse("1"));
  auto append = Api(store, "POST", path + "/versions", body);
  Q4T_CHECK(Ok(append));
  Q4T_CHECK(Api(store, "POST", path + "/versions", body).http_status == 409);
  auto version = Api(store, "GET", path + "/versions/2");
  Q4T_CHECK(Ok(version));
  Record(version.value);
  Q4T_CHECK(Api(store, "POST", path,
                Parse(R"({"default_version":"1","expected_revision":2})"))
                .ok());
  for (const auto* suffix :
       {"?owner=other", "/versions/02", "/versions/2/more", "#fragment"})
    Q4T_CHECK(!Api(store, "GET", path + suffix).ok());
  Q4T_CHECK(Api(store, "GET", path, Parse("{}")).http_status == 400);
  Q4T_CHECK(Api(store, "PUT", path, body).http_status == 405);
  Q4T_CHECK(Api(store, "POST", path + "/versions/2", body).http_status == 405);
  Q4T_CHECK(Api(store, "GET", "/v1/prompts?limit=0").http_status == 400);
  Q4T_CHECK(Api(store, "GET", "/v1/prompts?limit=1&limit=2").http_status ==
            400);
  Q4T_CHECK(Api(store, "GET", "/v1/prompts?order=unknown").http_status == 400);
  auto list = Api(store, "GET", "/v1/prompts?limit=1&order=asc");
  Q4T_CHECK(Ok(list));
  Record(list.value);
  auto erased = Api(store, "DELETE", path);
  Q4T_CHECK(Ok(erased));
  Record(erased.value);
  Q4T_CHECK(Api(store, "GET", path).http_status == 404);
  return true;
}

Q4T_TEST(prompt_management_route_partitions_reads_and_writes) {
  for (const auto* path : {"/v1/prompts", "/v1/prompts/prompt_a",
                           "/v1/prompts/prompt_a/versions/1"}) {
    Q4T_CHECK(PromptMethodAllowed("GET", path));
    Q4T_CHECK(ClassifyHttpConnection("GET", path) ==
              HttpConnectionClass::kObservation);
    Q4T_CHECK(!IsCancellationRequest("GET", path));
  }
  for (const auto* path : {"/v1/prompts", "/v1/prompts/prompt_a",
                           "/v1/prompts/prompt_a/versions"}) {
    Q4T_CHECK(PromptMethodAllowed("POST", path));
    Q4T_CHECK(ClassifyHttpConnection("POST", path) ==
              HttpConnectionClass::kBusiness);
  }
  Q4T_CHECK(!PromptMethodAllowed("DELETE", "/v1/prompts/prompt_a/versions/1"));
  Q4T_CHECK(!PromptMethodAllowed("GET", "/v1/prompts/../versions/1"));
  Q4T_CHECK(
      !PromptMethodAllowed("GET", "/v1/prompts/prompt_a/versions/1/extra"));
  return true;
}
