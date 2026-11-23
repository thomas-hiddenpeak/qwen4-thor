#include "../src/server/agent_environment_internal.h"
#include "../src/server/schema_string.h"
#include "q4t/io/base64.h"
#include "../src/server/mcp_transport_internal.h"
#include "../src/server/vault_credential_internal.h"
#include "q4t/server/agent_environment.h"
#include "q4t/server/agent_registration.h"
#include "q4t/server/agent_session.h"
#include "q4t/server/agent_vault.h"
#include "q4t/server/files.h"
#include "q4t/server/prompt_registry.h"
#include "q4t/server/state_cipher.h"

#include <algorithm>
#include <cstdlib>
#include <set>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <thread>

#include "q4t/server/chat_prompt.h"
#include "q4t/server/conversation_state.h"
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
void SetRequest(Json* definition, const std::string& key, Json value) {
  auto request = *definition->Find("request");
  Set(&request, key, std::move(value));
  Set(definition, "request", std::move(request));
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

Q4T_TEST(hosted_workspace_prewarms_once_and_installs_private_bytes) {
  Fixture f;
  const std::string root = "/v1/agents/environments";
  const std::string input = R"({"environment":{"type":"openai_hosted","files":[
    {"type":"inline","data":"cTR0","path":"/workspace/project"}]}})";
  std::string id;
  {
    ObjectStore store(f.options);
    auto prewarm = [&](std::string body, StateAccess access = Access()) {
      return HandleHostedEnvironment(store, "POST", root, body, access, "release");
    };
    auto first = prewarm(input);
    Q4T_CHECK(Ok(first) && first.http_status == 201);
    id = first.value.GetString("id");
    Q4T_CHECK(first.value.GetString("status") == "ready");
    auto file = first.value.GetArray("files")->array.at(0);
    Q4T_CHECK(file.GetString("id").starts_with("file_") &&
              file.GetInt("size_bytes") == 3 && !file.Has("data"));
    auto again = prewarm(R"({"environment":{"files":[{"path":"/workspace/project",
      "data":"cTR0","type":"inline"}],"type":"openai_hosted"}})");
    Q4T_CHECK(Ok(again) && OutputJson(again.value) == OutputJson(first.value));
    Q4T_CHECK(prewarm(R"({"environment":{"type":"openai_hosted"}})").http_status == 409);
    auto other = prewarm(input, Access("client:peer"));
    Q4T_CHECK(Ok(other) && other.value.GetString("id") != id);
    Q4T_CHECK(HandleHostedEnvironment(store, "GET", root + "/" + id, "",
                                      Access("client:peer"), "").http_status == 404);
    Q4T_CHECK(HandleHostedEnvironment(store, "GET", root + "/" + id + "/files",
                                      "", Access(), "").http_status == 409);
  }
  ObjectStore reopened(f.options);
  ObjectRequest record;
  record.kind = ObjectKind::kEnvironment;
  record.scope = Access().scope;
  record.id = id;
  auto row = reopened.Execute(record, Access().deadline);
  Q4T_CHECK(Ok(row));
  auto data = *row.value.Find("data");
  Json definition;
  Q4T_CHECK(Ok(environment_detail::OpenDefinition(
      reopened, Access(), id, *data.Find("sealed_definition"), &definition)));
  Q4T_CHECK(definition.GetArray("files")->array.at(0).GetString("data") == "cTR0");
  record.expected_revision = row.value.GetInt("revision");
  record.operation = ObjectOperation::kPut;
  Set(&data, "warm_until", Parse("1"));
  record.data = std::move(data);
  Q4T_CHECK(Ok(reopened.Execute(record, Access().deadline)));
  auto expired = HandleHostedEnvironment(reopened, "POST", root, input, Access(), "release");
  Q4T_CHECK(Ok(expired) && expired.value.GetString("status") == "expired");
  ObjectRequest claim;
  auto env = Parse("{\"type\":\"openai_hosted\",\"environment_id\":\"" + id + "\"}");
  Q4T_CHECK(environment_detail::PrepareAttachment(reopened, Access(), env,
      "agent_session_expired", Parse("[]"), &claim).http_status == 409);
  return true;
}

Q4T_TEST(hosted_workspace_owns_uploaded_bytes_after_source_deletion) {
  Fixture f;
  const std::string root = "/v1/agents/environments";
  std::string environment, uploaded;
  {
    ObjectStore store(f.options);
    const BinaryAccess access{Access().scope, Access().deadline, {}};
    FileUpload upload(store, access, "multipart/form-data; boundary=f");
    Q4T_CHECK(Ok(upload.Feed(
        "--f\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nuser_data\r\n"
        "--f\r\nContent-Disposition: form-data; name=\"file\"; filename=\"record\"\r\n\r\n",
        access.deadline)));
    Q4T_CHECK(Ok(upload.Feed(std::string_view("q\0t", 3), access.deadline)));
    Q4T_CHECK(Ok(upload.Feed("\r\n--f--\r\n", access.deadline)));
    auto file = upload.Finish();
    Q4T_CHECK(Ok(file));
    uploaded = file.value.GetString("id");
    const auto body = "{\"environment\":{\"type\":\"openai_hosted\",\"files\":["
        "{\"type\":\"file_id\",\"path\":\"/workspace/record\",\"file_id\":\"" + uploaded + "\"}]}}";
    Q4T_CHECK(HandleHostedEnvironment(store, "POST", root, body,
                                      Access("client:peer"), "").http_status == 404);
    auto warm = HandleHostedEnvironment(store, "POST", root, body, Access(), "");
    Q4T_CHECK(Ok(warm));
    environment = warm.value.GetString("id");
    Q4T_CHECK(warm.value.GetArray("files")->array.front().GetString("file_id") == uploaded);
    Q4T_CHECK(Ok(Files(store).Erase(access, uploaded)));
  }
  ObjectStore reopened(f.options);
  ObjectRequest read;
  read.kind = ObjectKind::kEnvironment;
  read.id = environment;
  read.scope = Access().scope;
  auto row = reopened.Execute(read, Access().deadline);
  Q4T_CHECK(Ok(row));
  Json snapshot;
  const auto& envelope = *row.value.Find("data")->Find("sealed_definition");
  Q4T_CHECK(Ok(environment_detail::OpenDefinition(
      reopened, Access(), environment, envelope, &snapshot)));
  const auto& file = snapshot.GetArray("files")->array.at(0);
  Q4T_CHECK(file.GetString("type") == "inline" && file.GetString("data") == "cQB0");
  Q4T_CHECK(!environment_detail::OpenDefinition(
      reopened, Access("client:peer"), environment, envelope, &snapshot).ok());
  return true;
}

Q4T_TEST(hosted_workspace_session_turn_and_file_commit_share_revision) {
  Fixture f;
  ObjectStore store(f.options);
  const std::string root = "/v1/agents/environments";
  ResponsesExecution idle(store);
  auto request = Parse(R"({"agent":{"model":"cpu"},"environment":{"type":"openai_hosted"}})");
  auto opened = PrepareAgentSessionExecution(store, request, Config(), Access(),
                                             std::time(nullptr), idle);
  Q4T_CHECK(Ok(opened) && !idle.Prepared());
  const auto id = opened.value.GetString("id");
  const auto env = opened.value.Find("environment")->GetString("id");
  const auto files = root + "/" + env + "/files";
  auto change = [&](std::string path, std::string data = "YQ==") {
    auto body = "{\"type\":\"inline\",\"path\":\"/workspace/" + path +
                "\",\"data\":\"" + data + "\"}";
    return HandleHostedEnvironment(store, "POST", files, body, Access(), "");
  };
  AgentSessionView view;
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
  Q4T_CHECK(view.session.GetString("status") == "idle" && view.turns.array.empty());
  Q4T_CHECK(ReadAgentSessionTurn(store, id, "turn_unknown", Access(), &view).http_status == 404);
  AgentEventCursor cursor;
  Json events;
  bool settled = false;
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &cursor, &events, &settled)) &&
            settled && events.array.size() == 2 &&
            events.array.front().GetString("type") == "agent.session.created" &&
            events.array.back().GetString("type") == "agent.session.idle");
  Q4T_CHECK(Ok(change("a/z")) && Ok(change("a.x")) && Ok(change("a/b", "")));
  Q4T_CHECK(change("a").http_status == 400);
  auto page = [&](std::string query) {
    return HandleHostedEnvironment(store, "GET", files + query, "", Access(), "");
  };
  auto first = page("?order=asc&limit=2");
  Q4T_CHECK(Ok(first) && first.value.GetBool("has_more"));
  auto rows = first.value.GetArray("data");
  Q4T_CHECK(rows->array.at(0).GetString("path") == "/workspace/a/b" &&
            rows->array.at(1).GetString("path") == "/workspace/a/z");
  auto token = first.value.GetString("next");
  auto next = page("?order=asc&limit=2&page=" + token);
  Q4T_CHECK(Ok(next) && !next.value.GetBool("has_more") &&
            next.value.GetArray("data")->array.at(0).GetString("path") == "/workspace/a.x");
  Q4T_CHECK(page("?limit=2&page=" + token).http_status == 400);
  const auto message = Parse(R"({"events":[{"type":"agent.session.input.message",
      "input":[{"role":"user","content":[{"type":"input_text","text":"records"}]}]}]})");
  bool replay = false;
  auto prepare = [&](ResponsesExecution& run) {
    return PrepareAgentSessionEvents(store, id, message, "first-turn", Config(),
                                     Access(), run, &replay);
  };
  auto begin = [&](ResponsesExecution& run) {
    run.BindBackgroundControl(std::make_shared<RequestControl>(
        run.Id(), "", Clock::now() + 5s));
    std::string wire;
    return run.Begin(&wire);
  };
  ResponsesExecution stale(store);
  Q4T_CHECK(Ok(prepare(stale)) && !replay);
  Q4T_CHECK(Ok(change("a/b", "YWJj")));
  Q4T_CHECK(begin(stale).http_status == 409 &&
            ResponsesState(store).Retrieve(stale.Id(), Access()).http_status == 404);
  ResponsesExecution first_turn(store);
  Q4T_CHECK(Ok(prepare(first_turn)) && Ok(begin(first_turn)));
  Q4T_CHECK(change("racing").http_status == 409 && Ok(page("")));
  std::string wire;
  Q4T_CHECK(Ok(first_turn.StartRunning(&wire)) &&
            Ok(first_turn.Finish(false, 0, {2, 0, 0, 0}, std::time(nullptr), &wire)));
  Q4T_CHECK(Ok(change("after", "")));
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
  Q4T_CHECK(view.session.Find("environment")->GetArray("files")->array.size() == 4);
  return true;
}

Q4T_TEST(hosted_workspace_retention_collects_orphan_after_reopen) {
  Fixture f;
  const std::string root = "/v1/agents/environments";
  const auto templates = root + "/templates";
  const auto cutoff = std::time(nullptr) - 30;
  ObjectRequest orphan;
  orphan.kind = ObjectKind::kConversationItem;
  orphan.id = "item_orphan";
  orphan.scope = Access().scope;
  std::string template_id, workspace_id, session_id;
  {
    ObjectStore store(f.options);
    auto item = orphan;
    item.operation = ObjectOperation::kPut;
    item.created_at = cutoff - 1;
    item.ttl_seconds = 0;
    item.data = Parse(R"({"item":{"type":"message","role":"user","content":"old"}})");
    Q4T_CHECK(Ok(store.Execute(item, Access().deadline)));
    auto tmpl = HandleEnvironmentTemplate(store, "POST", templates,
        R"({"files":[{"type":"inline","path":"/workspace/record","data":"cTR0"}]})",
        Access());
    Q4T_CHECK(Ok(tmpl));
    template_id = tmpl.value.GetString("id");
    const auto setup = "{\"environment\":{\"type\":\"openai_hosted\","
                       "\"environment_template_id\":\"" + template_id + "\"}}";
    auto warm = HandleHostedEnvironment(store, "POST", root, setup,
                                        Access(), "gc-replay");
    Q4T_CHECK(Ok(warm));
    workspace_id = warm.value.GetString("id");
    auto request = Parse("{\"agent\":{\"model\":\"cpu\"},\"environment\":{"
                         "\"type\":\"openai_hosted\",\"environment_id\":\"" +
                         workspace_id + "\"}}");
    ResponsesExecution idle(store);
    auto session = PrepareAgentSessionExecution(store, request, Config(),
                                                Access(), cutoff + 30, idle);
    Q4T_CHECK(Ok(session) && !idle.Prepared());
    session_id = session.value.GetString("id");
  }
  ObjectStore reopened(f.options);
  auto plan = reopened.Retention(cutoff, 128, {}, Access().deadline);
  // The encryption keyring has its own internal scope. Only the tenant
  // orphan should appear in the collection plan.
  Q4T_CHECK(Ok(plan) && plan.value.GetInt("eligible_objects") == 1 &&
            plan.value.GetArray("candidates") &&
            plan.value.GetArray("candidates")->array.size() == 1 &&
            plan.value.GetArray("candidates")->array.front().GetString("id") == orphan.id);
  auto apply = reopened.Retention(cutoff, 128, plan.value.GetString("plan_sha256"),
                                  Access().deadline);
  Q4T_CHECK(Ok(apply) && apply.value.GetInt("deleted_objects") == 1 &&
            reopened.Execute(orphan, Access().deadline).http_status == 404);
  AgentSessionView view;
  Q4T_CHECK(Ok(ReadAgentSessionTurn(reopened, session_id, "", Access(), &view)) &&
            view.session.GetString("status") == "idle" && view.turns.array.empty());
  Q4T_CHECK(Ok(HandleEnvironmentTemplate(reopened, "GET",
      templates + "/" + template_id, "", Access())));
  auto files = HandleHostedEnvironment(reopened, "GET",
      root + "/" + workspace_id + "/files", "", Access(), "");
  Q4T_CHECK(Ok(files) && files.value.GetArray("data")->array.size() == 1 &&
            files.value.GetArray("data")->array.front().GetInt("size_bytes") == 3);
  return true;
}

Q4T_TEST(hosted_workspace_two_claims_commit_only_one_session_and_response) {
  Fixture f;
  ObjectStore store(f.options);
  const std::string root = "/v1/agents/environments";
  auto created = HandleHostedEnvironment(store, "POST", root,
      R"({"environment":{"type":"openai_hosted"}})", Access(), "claim");
  Q4T_CHECK(Ok(created));
  const auto env = created.value.GetString("id");
  auto request = Parse("{\"agent\":{\"model\":\"cpu\"},\"input\":\"hello\","
      "\"environment\":{\"type\":\"openai_hosted\",\"environment_id\":\"" + env + "\"}}");
  ResponsesExecution one(store), two(store);
  auto prepare = [&](ResponsesExecution& run) {
    return PrepareAgentSessionExecution(store, request, Config(), Access(),
                                        std::time(nullptr), run);
  };
  auto first = prepare(one), second = prepare(two);
  Q4T_CHECK(Ok(first) && Ok(second));
  std::string wire;
  for (auto* run : {&one, &two})
    run->BindBackgroundControl(std::make_shared<RequestControl>(
        run->Id(), "", Clock::now() + 5s));
  Q4T_CHECK(Ok(one.Begin(&wire)) && two.Begin(&wire).http_status == 409);
  AgentSessionView view;
  Q4T_CHECK(ReadAgentSessionTurn(store, second.value.GetString("id"), "", Access(), &view).http_status == 404);
  Q4T_CHECK(ResponsesState(store).Retrieve(two.Id(), Access()).http_status == 404);
  auto retry = HandleHostedEnvironment(store, "POST", root,
      R"({"environment":{"type":"openai_hosted"}})", Access(), "claim");
  Q4T_CHECK(Ok(retry) && retry.value.GetString("status") == "connected");
  return true;
}

Q4T_TEST(environment_template_http_bounds_apply_to_input_and_count_unicode) {
  using Shape = EnvironmentTemplateShape;
  auto chinese_name = std::string{};
  for (int i = 0; i < 256; ++i) chinese_name += "界";
  for (const auto shape : {Shape::kCreate, Shape::kUpdate}) {
    ChatPlanError error;
    const auto accepts = [&](const Json& value) {
      return ValidateEnvironmentTemplate(value, shape, &error).ok();
    };
    auto definition = Parse(R"({"name":null,"setup_commands":[{"command":""}]})");
    Q4T_CHECK(accepts(definition));
    Set(&definition, "name", Parse("\"" + chinese_name + "\""));
    Q4T_CHECK(accepts(definition));
    Set(&definition, "name", Parse("\"" + chinese_name + "界\""));
    Q4T_CHECK(!accepts(definition) && error.param == "name");
    Q4T_CHECK(!accepts(Parse(R"({"name":""})")));
    Q4T_CHECK(!accepts(Parse(R"({"env":{"":"private"}})")));
    Q4T_CHECK(accepts(Parse(R"({"env":{"项目":"private"}})")));
    auto commands = Parse("[]");
    for (int i = 0; i < 16; ++i) commands.array.push_back(Parse(R"({"command":""})"));
    Set(&definition, "name", {});
    Set(&definition, "setup_commands", commands);
    Q4T_CHECK(accepts(definition));
    commands.array.push_back(commands.array.front());
    Set(&definition, "setup_commands", commands);
    Q4T_CHECK(!accepts(definition) && error.param == "setup_commands");
    Q4T_CHECK(ValidateEnvironmentTemplate(
        definition, Shape::kStoredDefinition, &error));
  }
  return true;
}

Q4T_TEST(environment_template_inline_limits_count_decoded_file_bytes) {
  constexpr size_t limit = 5 * 1024 * 1024;
  std::string at_limit;
  Q4T_CHECK(q4t::io::EncodeBase64(std::string(limit, 'q'), 6990508,
                                   &at_limit));
  const auto single = [&](const std::string& encoded) {
    auto file = Parse(R"({"type":"inline","path":"/workspace/one","data":""})");
    file.object.back().second.str = encoded;
    return file;
  };
  ChatPlanError error;
  auto file = single(at_limit);
  Q4T_CHECK(ValidateEnvironment(file, EnvironmentShape::kFileCreate, &error));
  // One more byte uses the same base64 string length and removes its padding.
  auto over = at_limit;
  Q4T_CHECK(over.back() == '=');
  over.back() = 'A';
  Q4T_CHECK(!ValidateEnvironment(single(over), EnvironmentShape::kFileCreate,
                                 &error) && error.param == "data");
  auto body = Parse(R"({"files":[]})");
  auto& files = body.object.front().second.array;
  files.push_back(file);
  Set(&file, "path", Parse(R"("/workspace/two")"));
  files.push_back(std::move(file));
  for (auto shape : {EnvironmentTemplateShape::kCreate,
                     EnvironmentTemplateShape::kUpdate})
    Q4T_CHECK(ValidateEnvironmentTemplate(body, shape, &error));
  auto third = single("YQ==");
  Set(&third, "path", Parse(R"("/workspace/three")"));
  files.push_back(std::move(third));
  Q4T_CHECK(!ValidateEnvironmentTemplate(body, EnvironmentTemplateShape::kCreate,
                                         &error) &&
            error.param == "files[2].data");
  Q4T_CHECK(ValidateEnvironmentTemplate(
      body, EnvironmentTemplateShape::kStoredDefinition, &error));
  return true;
}

Q4T_TEST(environment_template_max_inline_file_survives_reopen_and_rejects_mixed_parts) {
  Fixture f;
  constexpr size_t file_bytes = 5 * 1024 * 1024;
  std::string encoded;
  Q4T_CHECK(q4t::io::EncodeBase64(std::string(file_bytes, 'q'), 6990508, &encoded));
  auto file = Parse(R"({"type":"inline","path":"/workspace/records","data":""})");
  auto data = *file.Find("data");
  data.str = encoded;
  Set(&file, "data", std::move(data));
  auto definition = Parse(R"({"files":[]})");
  definition.object.front().second.array.push_back(std::move(file));
  const auto root = std::string("/v1/agents/environments/templates");
  const auto access = [] {
    return StateAccess{"client:large-template", Clock::now() + 60s, {}};
  };
  std::string id;
  ObjectRequest orphan;
  orphan.kind = ObjectKind::kConversationItem;
  orphan.id = "item_unrelated";
  orphan.scope = access().scope;
  const auto cutoff = std::time(nullptr) - 30;
  {
    ObjectStore objects(f.options);
    auto put = orphan;
    put.operation = ObjectOperation::kPut;
    put.created_at = cutoff - 1;
    put.ttl_seconds = 0;
    put.data = Parse(R"({"item":{"type":"message","role":"user","content":"old"}})");
    Q4T_CHECK(Ok(objects.Execute(put, access().deadline)));
    auto result = HandleEnvironmentTemplate(objects, "POST", root,
                                            OutputJson(definition), access());
    Q4T_CHECK(Ok(result) && result.http_status == 201);
    id = result.value.GetString("id");
    Q4T_CHECK(result.value.GetArray("files")->array.front().GetInt("size_bytes") ==
              file_bytes);
  }
  ObjectStore objects(f.options);
  auto plan = objects.Retention(cutoff, 128, {}, access().deadline);
  Q4T_CHECK(Ok(plan) && plan.value.GetInt("eligible_objects") == 1 &&
            plan.value.GetArray("candidates")->array.at(0).GetString("id") ==
                orphan.id);
  auto collected = objects.Retention(cutoff, 128, plan.value.GetString("plan_sha256"),
                                     access().deadline);
  Q4T_CHECK(Ok(collected) && collected.value.GetInt("deleted_objects") == 1 &&
            objects.Execute(orphan, access().deadline).http_status == 404);
  ObjectRequest read;
  read.scope = access().scope;
  read.kind = ObjectKind::kEnvironmentTemplate;
  read.id = id;
  auto row = objects.Execute(read, access().deadline);
  Q4T_CHECK(Ok(row));
  const auto sealed = *row.value.Find("data")->Find("sealed_definition");
  Q4T_CHECK(sealed.GetArray("parts") && sealed.GetArray("parts")->array.size() == 2);
  Json opened;
  auto open = [&](const std::string& target, const Json& envelope) {
    return environment_detail::OpenDefinition(objects, access(), target, envelope, &opened);
  };
  Q4T_CHECK(Ok(open(id, sealed)) &&
            opened.GetArray("files")->array.front().GetString("data") == encoded);
  Q4T_CHECK(open(id + "_other", sealed).http_status == 503);
  auto changed = sealed;
  auto parts = *changed.GetArray("parts");
  std::swap(parts.array.front(), parts.array.back());
  Set(&changed, "parts", parts);
  Q4T_CHECK(open(id, changed).http_status == 503);
  parts.array.pop_back();
  Set(&changed, "parts", parts);
  Q4T_CHECK(open(id, changed).http_status == 503);
  const auto path = root + "/" + id;
  auto updated = HandleEnvironmentTemplate(objects, "POST", path,
      R"({"files":null,"name":"cleared"})", access());
  Q4T_CHECK(Ok(updated) && updated.value.GetArray("files")->array.empty());
  row = objects.Execute(read, access().deadline);
  Q4T_CHECK(Ok(row) && row.value.Find("data")->Find("sealed_definition")->IsString());
  return true;
}

Q4T_TEST(environment_template_seals_private_inputs_and_preserves_patch_semantics) {
  Fixture f;
  const std::string root = "/v1/agents/environments/templates";
  std::string id;
  {
    ObjectStore objects(f.options);
    auto created = HandleEnvironmentTemplate(objects, "POST", root,
        R"({"name":"docs","env":{"API_TOKEN":"private-environment-value"},
          "setup_commands":[{"command":"private-setup-token"}],
          "files":[{"type":"inline","path":"/workspace/record","data":"SGVsbG8="}],
          "network":{"access":"restricted","allowed_domains":[],
                     "blocked_domains":["private.example"]},
          "packages":{"python":["pinned-package"]}})", Access());
    Q4T_CHECK(Ok(created));
    id = created.value.GetString("id");
    Q4T_CHECK(created.http_status == 201 && id.starts_with("environment_template_") &&
              created.value.GetString("object") == "agent.environment.template");
    auto wire = OutputJson(created.value);
    for (const auto* confidential : {"private-environment-value", "private-setup-token",
                                      "SGVsbG8=", "private.example"})
      Q4T_CHECK(wire.find(confidential) == std::string::npos);
    Q4T_CHECK(created.value.GetArray("files")->array.front().GetInt("size_bytes") == 5);
    Q4T_CHECK(created.value.Find("packages")->GetArray("system")->array.empty());
    ObjectRequest read;
    read.kind = ObjectKind::kEnvironmentTemplate;
    read.scope = Access().scope;
    read.id = id;
    auto row = objects.Execute(read, Access().deadline);
    Q4T_CHECK(Ok(row) && OutputJson(row.value).find("private-setup-token") == std::string::npos);
    std::string plain;
    StateCipherBinding binding{"q4t.agents", "environment", "template_definition", id};
    const auto sealed = row.value.Find("data")->GetString("sealed_definition");
    Q4T_CHECK(Ok(StateCipher(objects).Open(sealed, binding, Access(), &plain)) &&
              Parse(plain).Find("env")->GetString("API_TOKEN") == "private-environment-value");
    binding.item_id += "_other";
    Q4T_CHECK(!StateCipher(objects).Open(sealed, binding, Access(), &plain).ok());
  }
  ObjectStore objects(f.options);
  const auto path = root + "/" + id;
  const auto call = [&](const char* method, const std::string& route,
                        const std::string& body = "", StateAccess access = Access()) {
    return HandleEnvironmentTemplate(objects, method, route, body, access);
  };
  auto updated = call("POST", path, R"({"name":"new","desktop":{"enabled":true}})");
  Q4T_CHECK(Ok(updated) && updated.http_status == 200 && updated.value.GetString("name") == "new" &&
            updated.value.GetArray("files")->array.size() == 1 &&
            updated.value.Find("desktop")->GetBool("enabled"));
  for (const char* bad : {
           R"({"files":[{"type":"inline","path":"/workspace/record","data":"SB=="}]})",
           R"({"files":[{"type":"inline","path":"/record","data":""}]})",
           R"({"files":[{"type":"inline","path":"/workspace/../record","data":""}]})",
           R"({"network":{"access":"enabled","blocked_domains":["example.org"]}})",
           R"({"network":{"access":"disabled","blocked_domains":["example.org"]}})",
           R"({"network":{"access":"restricted","blocked_domains":["*.example.org"]}})",
           R"({"network":{"access":"restricted","allowed_domains":["a.org"],"blocked_domains":["b.org"]}})",
           R"({"packages":{"python":"unexpected"}})"})
    Q4T_CHECK(call("POST", path, bad).http_status == 400);
  for (const bool reverse : {false, true}) {
    auto files = Parse(R"([{"type":"inline","data":"","path":"/workspace/a"},
                           {"type":"inline","data":"","path":"/workspace/a.extra"},
                           {"type":"inline","data":"","path":"/workspace/a/b"}])");
    if (reverse) std::reverse(files.array.begin(), files.array.end());
    auto request = Parse("{}");
    Set(&request, "files", files);
    Q4T_CHECK(call("POST", path, OutputJson(request)).http_status == 400);
  }
  Q4T_CHECK(OutputJson(call("GET", path).value) == OutputJson(updated.value));
  auto reset = call("POST", path, R"({"name":null,"files":null,"desktop":null,"network":null})");
  Q4T_CHECK(Ok(reset) && reset.value.Find("name")->IsNull() &&
            reset.value.GetArray("files")->array.empty() &&
            reset.value.Find("network")->GetString("access") == "disabled" &&
            !reset.value.Find("desktop")->GetBool("enabled"));
  auto second = call("POST", root);
  Q4T_CHECK(Ok(second));
  auto page = call("GET", root+"?limit=1&order=asc");
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more"));
  auto after = call("GET", root+"?after="+page.value.GetString("last_id")+"&order=asc");
  Q4T_CHECK(Ok(after) && !after.value.GetBool("has_more") &&
            page.value.GetString("last_id") != after.value.GetString("last_id"));
  for (const auto* method : {"GET", "DELETE"})
    Q4T_CHECK(call(method, path, "", Access("client:other")).http_status == 404);
  Q4T_CHECK(call("GET", root, "", Access("client:other")).value.GetArray("data")->array.empty());
  Q4T_CHECK(Ok(call("DELETE", path)) && call("GET", path).http_status == 404);
  return true;
}

Q4T_TEST(agent_session_wire_shapes_follow_the_sdk_transport_fields) {
  ChatPlanError error;
  for (const char* input : {
           R"({"agent":{"model":"cpu"},"environment":{"type":"none"}})",
           R"({"agent_id":"agent_1","environment":{"type":"none"},
               "stream":true,"input":"hello"})"}) {
    Q4T_CHECK(ValidateAgentSession(Parse(input), AgentSessionShape::kCreate,
                                   &error));
  }
  for (const char* input : {
           R"({"agent_id":"agent_1"})",
           R"({"environment":{"type":"none","container_size":"medium"}})",
           R"({"environment":{"type":"none"},"toolHandlers":{}})",
           R"({"environment":{"type":"none"},"stream":"true"})"}) {
    Q4T_CHECK(!ValidateAgentSession(Parse(input), AgentSessionShape::kCreate,
                                    &error));
  }
  Q4T_CHECK(ValidateAgentSession(
      Parse(R"({"id":"as_1","object":"agent.session.deleted","deleted":true})"),
      AgentSessionShape::kDeleted, &error));
  Q4T_CHECK(!ValidateAgentSession(
      Parse(R"({"id":"as_1","object":"agent.deleted","deleted":true})"),
      AgentSessionShape::kDeleted, &error));
  return true;
}

Q4T_TEST(agent_session_prepares_native_input_and_validates_public_snapshot) {
  Fixture f;
  ObjectStore store(f.options);
  AgentSessionStart start;
  auto begin = [&](const Json& request) {
    return PrepareDirectAgentSession(store, request, Config(), Access(), "session_1",
                                     123, &start);
  };
  auto request = Parse(R"({"agent":{"model":"cpu"},
    "environment":{"type":"none"},"input":"project records",
    "metadata":{"project":"q4t"},"spend_control":{"limit":25}})");
  Q4T_CHECK(Ok(begin(request)));
  Q4T_CHECK(start.session.GetString("object") == "agent.session" &&
            start.session.GetString("status") == "in_progress" &&
            start.session.GetInt("last_active_at") == 123);
  Q4T_CHECK(start.session.Find("agent")->GetString("model") == "cpu");
  Q4T_CHECK(start.session.Find("spend_control")->Find("consumed")->IsNull());
  Q4T_CHECK(start.input.array[0].Find("content")->array[0].GetString("text") ==
            "project records");
  for (const char* invalid : {"null", "[]", R"("")",
                               R"([{"role":"user","content":[]}])"}) {
    auto bad = request;
    Set(&bad, "input", Parse(invalid));
    Q4T_CHECK(begin(bad).http_status == 400);
  }
  Set(&request, "spend_control", Parse(R"({"limit":0})"));
  Q4T_CHECK(begin(request).http_status == 400);
  return true;
}

Q4T_TEST(agent_session_snapshot_owns_settings_and_omits_transport_secrets) {
  Fixture f;
  ObjectStore store(f.options);
  auto source = HandleAgentRegistration(
      store, "POST", "/v1/agents", R"({"model":"cpu","name":"records",
        "instructions":"original","tools":[{"type":"mcp","server_label":"docs",
        "transport":{"type":"http","server_url":"https://example.org",
        "headers":{"x-correlation":"private-config"}}}]})", Config(), Access());
  Q4T_CHECK(Ok(source));
  auto request = Parse(R"({"environment":{"type":"none"},"input":"hello",
                            "agent":{"instructions":"one session"}})");
  Set(&request, "agent_id", *source.value.Find("id"));
  Q4T_CHECK(ResolveSessionAgent(store, request, Config(), Access("client:other"))
                .http_status == 404);
  auto snapshot = ResolveSessionAgent(store, request, Config(), Access());
  Q4T_CHECK(Ok(snapshot) && snapshot.value.GetString("name") == "records" &&
            snapshot.value.GetString("instructions") == "one session");
  Q4T_CHECK(!snapshot.value.Has("metadata") &&
            OutputJson(snapshot.value).find("private-config") == std::string::npos);
  auto saved = HandleAgentRegistration(store, "GET",
      "/v1/agents/" + source.value.GetString("id"), "", Config(), Access());
  Q4T_CHECK(Ok(saved) && saved.value.GetString("instructions") == "original" &&
            OutputJson(saved.value).find("private-config") != std::string::npos);
  auto inline_request = Parse(R"({"environment":{"type":"none"},"input":"hi",
    "agent":{"model":"cpu","tools":[{"type":"mcp","server_label":"docs",
    "transport":{"type":"http","server_url":"https://example.org",
    "authorization":"private-inline"}}]}})");
  ChatPlanError error;
  Q4T_CHECK(ValidateAgentSession(inline_request, AgentSessionShape::kCreate,
                                 &error));
  auto ephemeral = ResolveSessionAgent(store, inline_request, Config(), Access());
  Q4T_CHECK(Ok(ephemeral) && ephemeral.value.GetString("id").starts_with("agent_") &&
            OutputJson(ephemeral.value).find("private-inline") == std::string::npos);
  Q4T_CHECK(ResolveSessionAgent(store, Parse("{}"), Config(), Access())
                .http_status == 400);
  return true;
}

Q4T_TEST(agent_session_turn_shares_queue_receipt_and_reopens_native_output) {
  Fixture f;
  const auto request = Parse(R"({"agent":{"model":"cpu"},
    "environment":{"type":"none"},"input":"what is six times seven?"})");
  std::string id, turn, trace_before_restart;
  {
    ObjectStore store(f.options);
    ResponsesExecution run(store);
    auto prepared = PrepareAgentSessionExecution(store, request, Config(), Access(),
                                                 std::time(nullptr), run);
    Q4T_CHECK(Ok(prepared));
    id = prepared.value.GetString("id");
    AgentSessionView view;
    Q4T_CHECK(ReadAgentSessionTurn(store, id, "", Access(), &view).http_status == 404);
    run.BindBackgroundControl(std::make_shared<RequestControl>(
        run.Id(), "", Clock::now() + 5s));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
    turn = view.turn.GetString("id");
    Q4T_CHECK(view.turn.GetString("status") == "queued" &&
              view.items.array.size() == 1 &&
              view.turn.Find("started_at")->IsNull());
    Q4T_CHECK(Ok(run.StartRunning(&wire)));
    ResponseDelta delta;
    delta.kind = ResponseDelta::Kind::kContent;
    delta.text = "42";
    Q4T_CHECK(run.Append(delta, "null", &wire));
    Q4T_CHECK(Ok(run.Finish(false, 0, {12, 1, 2, 0}, std::time(nullptr), &wire)));
    Q4T_CHECK(ResponsesState(store).Erase(run.Id(), Access()).http_status == 409);
    auto traces = ListAgentSessionTraces(store, id, {}, Access());
    Q4T_CHECK(Ok(traces) && traces.value.GetString("first_id") == turn);
    trace_before_restart = OutputJson(traces.value);
  }
  ObjectStore store(f.options);
  auto traces = ListAgentSessionTraces(store, id, {}, Access());
  Q4T_CHECK(Ok(traces) && OutputJson(traces.value) == trace_before_restart);
  AgentSessionView view;
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, turn, Access(), &view)));
  Q4T_CHECK(view.session.GetString("status") == "idle" &&
            view.turn.GetString("status") == "completed" &&
            view.turn.GetInt("completed_at") >= view.turn.GetInt("started_at") &&
            view.turn.GetInt("started_at") > 0);
  Q4T_CHECK(view.items.array.size() == 2 &&
            view.items.array.back().GetArray("content")->array[0].GetString("text") == "42");
  Q4T_CHECK(view.session.Find("usage")->GetInt("total_tokens") == 14 &&
            !view.session.Find("usage")->Find("input_tokens_details")->Has("cache_write_tokens"));
  Q4T_CHECK(ReadAgentSessionTurn(store, id, "turn_other", Access(), &view).http_status == 404);
  Q4T_CHECK(ReadAgentSessionTurn(store, id, turn, Access("client:other"), &view).http_status == 404);
  return true;
}

Q4T_TEST(agent_session_turn_reports_the_original_failure_category) {
  Fixture f;
  ObjectStore store(f.options);
  const auto request = Parse(R"({"agent":{"model":"cpu"},
    "environment":{"type":"none"},"input":"lookup"})");
  struct Case {
    int status;
    const char* native;
    const char* projected;
    bool retryable;
  };
  const Case cases[] = {
      {429, "client_rpm_exhausted", "rate_limit_exceeded", true},
      {429, "client_tpm_exhausted", "rate_limit_exceeded", true},
      {408, "request_deadline_exceeded", "request_timeout", true},
      {400, "context_length_exceeded", "context_length_exceeded", false},
      {503, "service_draining", "server_overloaded", true},
      {429, "client_capacity_exhausted", "server_overloaded", true},
      {500, "client_rate_invalid", "server_error", false},
  };
  for (const auto& item : cases) {
    ResponsesExecution run(store);
    auto prepared = PrepareAgentSessionExecution(store, request, Config(),
                                                Access(), std::time(nullptr), run);
    Q4T_CHECK(Ok(prepared));
    const auto id = prepared.value.GetString("id");
    run.BindBackgroundControl(std::make_shared<RequestControl>(
        run.Id(), "", Clock::now() + 5s));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
    ResponsesExecution::ObserveError(&run, item.status, "original message",
                                     item.native);
    Q4T_CHECK(Ok(run.CloseUnfinished()));
    AgentSessionView view;
    Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
    Q4T_CHECK(view.session.GetString("status") ==
                  (item.retryable ? "idle" : "failed") &&
              view.session.Find("error")->IsNull() == item.retryable);
    const auto* error = view.turn.Find("error");
    Q4T_CHECK(view.turn.GetString("status") == "failed" && error &&
              error->GetString("code") == item.projected &&
              error->GetString("message") == "original message");
    auto traces = ListAgentSessionTraces(store, id, {}, Access());
    Q4T_CHECK(Ok(traces) && traces.value.GetArray("data")->array.size() == 1);
    const auto* resources = traces.value.GetArray("data")->array.front()
                                .Find("otlp")->GetArray("resourceSpans");
    Q4T_CHECK(resources && resources->array.size() == 1);
    const auto& spans = resources->array.front().GetArray("scopeSpans")
                            ->array.front().GetArray("spans")->array;
    Q4T_CHECK(spans.size() == 2 &&
              spans.front().Find("status")->GetInt("code") == 2 &&
              spans.back().Find("status")->GetInt("code") == 2 &&
              OutputJson(traces.value).find("original message") ==
                  std::string::npos);
  }
  return true;
}

Q4T_TEST(agent_session_tool_action_requires_a_completed_owned_call) {
  Fixture f;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  const auto request = Parse(R"({"agent":{"model":"cpu","tools":[
    {"type":"function","name":"lookup","description":"Read a project record",
     "parameters":{"type":"object","properties":{"revision":{"type":"integer"}}}}]},
    "environment":{"type":"none"},"input":"Look up revision 442."})");
  const auto now = std::time(nullptr);
  auto config = Config();
  Q4T_CHECK(PrepareAgentSessionExecution(store, request, config, Access(),
                                         now, run).code ==
            "structured_backend_unavailable");
  config.structured = true;
  auto prepared = PrepareAgentSessionExecution(store, request, config,
                                               Access(), now, run);
  Q4T_CHECK(Ok(prepared));
  const auto id = prepared.value.GetString("id");
  run.BindBackgroundControl(std::make_shared<RequestControl>(
      run.Id(), "", Clock::now() + 5s));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.call = {0, "call_lookup", "lookup", R"({"revision":442})"};
  Q4T_CHECK(run.Append(delta, "null", &wire));
  Q4T_CHECK(Ok(run.Checkpoint()));
  AgentSessionView view;
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
  Q4T_CHECK(view.session.GetArray("required_actions")->array.empty());
  AgentEventCursor active_cursor;
  Json active_events;
  bool active_terminal = false;
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &active_cursor,
                                        &active_events, &active_terminal)));
  Q4T_CHECK(!active_terminal);
  for (const auto& event : active_events.array)
    Q4T_CHECK(event.GetString("type") != "agent.session.turn.item.added" ||
              event.Find("item")->GetString("type") != "function_call");
  Q4T_CHECK(Ok(run.Finish(false, 1, {9, 0, 8, 0}, std::time(nullptr), &wire)));
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
  const auto& actions = view.session.GetArray("required_actions")->array;
  Q4T_CHECK(view.session.GetString("status") == "requires_action" &&
            view.turn.GetString("status") == "waiting" &&
            view.turn.Find("completed_at")->IsNull() && actions.size() == 1);
  Q4T_CHECK(actions[0].GetString("call_id") == "call_lookup" &&
            actions[0].Find("arguments")->GetInt("revision") == 442 &&
            actions[0].GetString("turn_id") == view.turn.GetString("id"));
  auto results = Parse(R"({"events":[{"type":"agent.session.input.tool_result",
    "turn_id":"unknown","call_id":"call_lookup","success":true,
    "output":"revision 442"}]})");
  ResponsesExecution next(store);
  bool replay = false;
  auto submit = [&](const Json& events, const char* key, ResponsesExecution& owner) {
    return PrepareAgentSessionEvents(store, id, events, key, config, Access(),
                                     owner, &replay);
  };
  Q4T_CHECK(submit(results, "lookup-wrong", next).http_status == 409);
  Set(&results.object[0].second.array[0], "turn_id", *view.turn.Find("id"));
  Q4T_CHECK(Ok(submit(results, "lookup-1", next)) && next.Prepared() && !replay);
  ResponsesExecution racing(store);
  Q4T_CHECK(Ok(submit(results, "lookup-1", racing)) && !replay);
  racing.BindBackgroundControl(std::make_shared<RequestControl>(
      racing.Id(), "", Clock::now() + 5s));
  next.BindBackgroundControl(std::make_shared<RequestControl>(
      next.Id(), "", Clock::now() + 5s));
  Q4T_CHECK(Ok(next.Begin(&wire)));
  auto lost_race = racing.Begin(&wire);
  Q4T_CHECK(lost_race.code == "object_revision_conflict");
  Q4T_CHECK(Ok(ReconcileAgentEventReceipt(store, id, results, "lookup-1",
                                          Access(), lost_race)));
  Q4T_CHECK(ResponsesState(store).Retrieve(racing.Id(), Access()).http_status == 404);
  ResponsesExecution retry(store);
  Q4T_CHECK(Ok(submit(results, "lookup-1", retry)) && replay && !retry.Prepared());
  auto conflict = results;
  Set(&conflict.object[0].second.array[0], "output", Parse(R"("different")"));
  Q4T_CHECK(submit(conflict, "lookup-1", retry).code == "idempotency_key_conflict");
  Q4T_CHECK(Ok(next.StartRunning(&wire)));
  delta = {};
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "442";
  Q4T_CHECK(next.Append(delta, "null", &wire));
  Q4T_CHECK(Ok(next.Finish(false, 0, {15, 0, 1, 0}, std::time(nullptr), &wire)));
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
  Q4T_CHECK(view.turns.array.size() == 1 &&
            view.turn.GetString("status") == "completed" &&
            view.items.array.size() == 4 &&
            view.items.array[2].GetString("type") == "function_call_output" &&
            view.items.array[2].GetString("output") == "revision 442" &&
            view.session.GetArray("required_actions")->array.empty() &&
            view.session.Find("usage")->GetInt("total_tokens") == 33);
  AgentEventCursor cursor;
  Json events;
  bool settled = false;
  std::set<std::string> identities;
  std::vector<std::string> types;
  std::string streamed_text;
  for (int batch = 0; batch < 8 && !settled; ++batch) {
    Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &cursor,
                                          &events, &settled)));
    Q4T_CHECK(!events.array.empty());
    for (const auto& event : events.array) {
      ChatPlanError invalid;
      Q4T_CHECK(ValidateAgentSession(event, AgentSessionShape::kEvent, &invalid));
      const auto event_id = event.GetString("event_id");
      Q4T_CHECK(event_id.starts_with("evt_") && event_id.find(run.Id()) == event_id.npos &&
                identities.insert(event_id).second);
      types.push_back(event.GetString("type"));
      if (types.back() == "agent.session.turn.item.added" &&
          event.Find("item")->GetString("type") == "function_call")
        Q4T_CHECK(event.Find("item")->Find("arguments")->GetInt("revision") == 442 &&
                  event.Find("item")->GetString("status") == "completed");
      if (types.back() == "agent.session.turn.output_text.delta")
        streamed_text += event.GetString("delta");
    }
  }
  Q4T_CHECK(settled && streamed_text == "442" &&
            types.front() == "agent.session.created" &&
            types.back() == "agent.session.idle");
  for (const auto* once : {"agent.session.turn.created", "agent.session.requires_action",
                           "agent.session.turn.completed"})
    Q4T_CHECK(std::count(types.begin(), types.end(), once) == 1);
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &cursor, &events, &settled)) &&
            events.array.empty() && !settled);
  AgentEventCursor peer;
  Q4T_CHECK(ReplayAgentSessionEvents(store, id, Access("client:other"), &peer,
                                     &events, &settled).http_status == 404);
  AgentEventCursor subscription;
  Q4T_CHECK(Ok(OpenAgentEventSubscription(store, id, Access(), &subscription)));
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &subscription, &events, &settled)) &&
            events.array.empty());
  const auto first_turn = view.turn.GetString("id");
  const auto follow = Parse(R"({"events":[{"type":"agent.session.input.message",
    "input":[{"role":"user","content":[{"type":"input_text","text":"again"}]}]}]})");
  ResponsesExecution later(store);
  Q4T_CHECK(Ok(submit(follow, "follow-1", later)) && later.Prepared() && !replay);
  later.BindBackgroundControl(std::make_shared<RequestControl>(
      later.Id(), "", Clock::now() + 5s));
  Q4T_CHECK(Ok(later.Begin(&wire)) &&
            Ok(ReadAgentSessionTurn(store, id, "", Access(), &view, true)));
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &subscription, &events, &settled)) &&
            !events.array.empty() && events.array.front().GetString("type") == "agent.session.turn.created" &&
            events.array.front().GetString("turn_id") != first_turn);
  Q4T_CHECK(view.turns.array.size() == 2 && view.turn.GetString("id") != first_turn &&
            view.items.array.size() == 5);
  ItemViewOptions paging;
  paging.limit = 1;
  auto page = ListAgentSessionTraces(store, id, paging, Access());
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more"));
  const auto pending = page.value.GetArray("data")->array.front();
  paging.after = pending.GetString("id");
  Q4T_CHECK(paging.after == view.turn.GetString("id") &&
            pending.Find("otlp")->GetArray("resourceSpans")->array.empty());
  page = ListAgentSessionTraces(store, id, paging, Access());
  Q4T_CHECK(Ok(page) && !page.value.GetBool("has_more") &&
            page.value.GetString("first_id") == first_turn);
  const auto& trace = page.value.GetArray("data")->array.front();
  Q4T_CHECK(trace.GetString("session_id") == id &&
            trace.GetString("object") == "agent.session.trace");
  const auto& resources = trace.Find("otlp")->GetArray("resourceSpans")->array;
  Q4T_CHECK(resources.size() == 1);
  const auto& spans = resources.front()
                          .GetArray("scopeSpans")
                          ->array.front()
                          .GetArray("spans")
                          ->array;
  Q4T_CHECK(spans.size() == 3);
  const auto& root = spans.back();
  Q4T_CHECK(root.GetString("traceId").size() == 32 &&
            root.GetString("spanId").size() == 16 &&
            root.GetString("name") == "q4t.agent.turn" &&
            !root.Has("parentSpanId"));
  for (size_t i = 0; i < 2; ++i)
    Q4T_CHECK(spans[i].GetString("traceId") == root.GetString("traceId") &&
              spans[i].GetString("parentSpanId") == root.GetString("spanId") &&
              spans[i].GetString("endTimeUnixNano") >=
                  spans[i].GetString("startTimeUnixNano"));
  Q4T_CHECK(spans[0].GetString("spanId") != spans[1].GetString("spanId") &&
            OutputJson(trace).find("revision 442") == std::string::npos);
  paging.descending = false;
  paging.after = first_turn;
  Q4T_CHECK(ListAgentSessionTraces(store, id, paging, Access())
                .value.GetString("last_id") == pending.GetString("id"));
  paging.after = "turn_unknown";
  Q4T_CHECK(ListAgentSessionTraces(store, id, paging, Access()).http_status ==
                400 &&
            ListAgentSessionTraces(store, id, paging, Access("client:other"))
                    .http_status == 404);
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, first_turn, Access(), &view)) &&
            view.items.array.size() == 4 && view.turn.GetString("status") == "completed");
  Q4T_CHECK(Ok(later.CloseUnfinished()));
  return true;
}

Q4T_TEST(
    agent_session_cancel_preserves_saved_results_and_never_retargets_a_retry) {
  Fixture f;
  ObjectStore store(f.options);
  auto config = Config();
  config.structured = true;
  const auto start = Parse(R"({"agent":{"model":"cpu","tools":[
    {"type":"function","name":"lookup","description":"Read a record","parameters":{"type":"object","properties":{}}}]},
    "environment":{"type":"none"},"input":"look up both project releases"})");
  ResponsesExecution run(store);
  auto session = PrepareAgentSessionExecution(store, start, config, Access(),
                                              std::time(nullptr), run);
  Q4T_CHECK(Ok(session));
  const auto id = session.value.GetString("id");
  run.BindBackgroundControl(
      std::make_shared<RequestControl>(run.Id(), "", Clock::now() + 5s));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
  for (size_t i = 0; i < 2; ++i) {
    ResponseDelta delta;
    delta.kind = ResponseDelta::Kind::kTool;
    delta.call = {i, "call_" + std::to_string(i), "lookup", "{}"};
    Q4T_CHECK(run.Append(delta, "null", &wire));
  }
  Q4T_CHECK(Ok(run.Finish(false, 2, {9, 0, 8, 0}, std::time(nullptr), &wire)));
  AgentSessionView view;
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)) &&
            view.session.GetArray("required_actions")->array.size() == 2);
  const auto turn = view.turn.GetString("id");
  auto result = Parse(R"({"events":[{"type":"agent.session.input.tool_result",
    "turn_id":"","call_id":"call_0","success":true,"output":"release 442"}]})");
  Set(&result.object[0].second.array.front(), "turn_id", *view.turn.Find("id"));
  ResponsesExecution partial(store);
  bool replay = false;
  Q4T_CHECK(Ok(PrepareAgentSessionEvents(store, id, result, "saved", config,
                                         Access(), partial, &replay)) &&
            !partial.Prepared());
  AgentEventCursor live;
  Q4T_CHECK(Ok(OpenAgentEventSubscription(store, id, Access(), &live)) &&
            live.waiting);
  auto cancel = Parse(R"({"events":[{"type":"agent.session.input.cancel"}]})");
  Q4T_CHECK(CancelAgentSessionTurn(store, nullptr, id, cancel, "stop",
                                   Access("client:other"))
                .http_status == 404);
  Q4T_CHECK(
      CancelAgentSessionTurn(store, nullptr, id, cancel, "saved", Access())
          .http_status == 409);
  Q4T_CHECK(
      Ok(CancelAgentSessionTurn(store, nullptr, id, cancel, "stop", Access())));
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, turn, Access(), &view)) &&
            view.turn.GetString("status") == "cancelled" &&
            view.session.GetString("status") == "idle" &&
            view.session.GetArray("required_actions")->array.empty() &&
            view.items.array.back().GetString("output") == "release 442");
  Json events;
  bool settled = false;
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &live, &events,
                                        &settled)) &&
            settled && events.array.size() == 2 &&
            events.array.front().GetString("type") ==
                "agent.session.turn.cancelled" &&
            events.array.back().GetString("type") == "agent.session.idle");
  const auto once = events.array.front().GetString("event_id");
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &live, &events,
                                        &settled)) &&
            events.array.empty() && !settled);
  AgentEventCursor history;
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &history, &events,
                                        &settled)) &&
            settled);
  Q4T_CHECK(events.array.at(events.array.size() - 2).GetString("event_id") ==
            once);
  for (const auto& ev : events.array)
    Q4T_CHECK(ev.GetString("type") != "agent.session.turn.item.added" ||
              ev.Find("item")->GetString("type") != "function_call");
  auto follow =
      Parse(R"({"events":[{"type":"agent.session.input.message","input":[
    {"role":"user","content":[{"type":"input_text","text":"what was the saved result?"}]}]}]})");
  ResponsesExecution next(store);
  Q4T_CHECK(Ok(PrepareAgentSessionEvents(store, id, follow, "follow", config,
                                         Access(), next, &replay)));
  next.BindBackgroundControl(
      std::make_shared<RequestControl>(next.Id(), "", Clock::now() + 5s));
  Q4T_CHECK(Ok(next.Begin(&wire)));
  auto row = ResponsesState(store).ReadRecord(next.Id(), Access());
  Q4T_CHECK(Ok(row));
  const auto* inherited = row.value.GetArray("history");
  Q4T_CHECK(inherited &&
            std::ranges::count_if(
                inherited->array,
                [](const auto& item) {
                  return item.GetString("type") == "function_call_output" &&
                         item.GetString("output") == "release 442";
                }) == 1 &&
            std::ranges::none_of(inherited->array, [](const auto& item) {
              return item.GetString("call_id") == "call_1";
            }));
  Q4T_CHECK(
      Ok(CancelAgentSessionTurn(store, nullptr, id, cancel, "stop", Access())));
  bool cancelled = true;
  Q4T_CHECK(Ok(next.CheckParentCancellation(&cancelled)) && !cancelled);
  // Lose the signalling executor after writing the intent. Restored queued work
  // observes the same durable intent before starting the model.
  Q4T_CHECK(
      CancelAgentSessionTurn(store, nullptr, id, cancel, "stop-next", Access())
          .http_status == 503);
  ResponsesExecution restored(store);
  Q4T_CHECK(Ok(restored.RestoreQueued(next.Id(), config, Access())));
  Q4T_CHECK(Ok(restored.CheckParentCancellation(&cancelled)) && cancelled);
  Q4T_CHECK(Ok(next.Cancel()) &&
            Ok(CancelAgentSessionTurn(store, nullptr, id, cancel, "stop-next",
                                      Access())));
  int terminals = 0;
  for (int i = 0; i < 4; ++i) {
    Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &live, &events, &settled)));
    for (const auto& event : events.array)
      terminals += event.GetString("type") == "agent.session.turn.cancelled";
  }
  Q4T_CHECK(terminals == 1);
  return true;
}

Q4T_TEST(agent_session_turn_exhausted_object_quota_leaves_no_orphan) {
  Fixture f;
  f.options.max_client_objects = 1;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  const auto request = Parse(R"({"agent":{"model":"cpu"},
    "environment":{"type":"none"},"input":"project records"})");
  auto prepared = PrepareAgentSessionExecution(store, request, Config(), Access(),
                                               std::time(nullptr), run);
  Q4T_CHECK(Ok(prepared));
  run.BindBackgroundControl(std::make_shared<RequestControl>(run.Id(), "", Clock::now() + 5s));
  std::string wire;
  auto rejected = run.Begin(&wire);
  Q4T_CHECK(!rejected.ok() && !rejected.commit_unknown && rejected.http_status == 429);
  AgentSessionView view;
  Q4T_CHECK(ReadAgentSessionTurn(store, prepared.value.GetString("id"), "", Access(), &view).http_status == 404);
  Q4T_CHECK(ResponsesState(store).Retrieve(run.Id(), Access()).http_status == 404);
  return true;
}

Q4T_TEST(agent_session_settings_use_cas_filtered_pages_and_immutable_step_settings) {
  Fixture f;
  ObjectStore store(f.options);
  auto config = Config();
  config.structured = true;
  auto start = Parse(R"({"agent":{"model":"cpu"},"environment":{"type":"none"},
    "metadata":{"team":"unfiltered"},"input":"hello"})");
  for (int index = 0; index < 10; ++index) {
    ResponsesExecution run(store);
    auto created = PrepareAgentSessionExecution(store, start, config, Access(),
                                               std::time(nullptr), run);
    Q4T_CHECK(Ok(created));
    run.BindBackgroundControl(std::make_shared<RequestControl>(
        run.Id(), "", Clock::now() + 5s));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
    Q4T_CHECK(Ok(run.Finish(false, 0, {4, 0, 0, 0}, std::time(nullptr), &wire)));
    if (index != 0 && index != 9) continue;
    const auto id = created.value.GetString("id");
    auto update = Parse(R"({"metadata":{"team":"records"},"review":"auto_review",
      "spend_control":{"limit":25},"agent":{"reasoning":{"effort":"low"}}})");
    Q4T_CHECK(UpdateAgentSession(store, id, update, config, Access("client:other")).http_status == 404);
    auto updated = UpdateAgentSession(store, id, update, config, Access());
    Q4T_CHECK(Ok(updated) && updated.value.GetString("status") == "idle" &&
              updated.value.Find("spend_control")->GetInt("limit") == 25 &&
              updated.value.Find("agent")->Find("reasoning")->GetString("effort") == "low");
    AgentEventCursor cursor;
    Json events;
    bool settled = false;
    Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &cursor, &events, &settled)));
    const auto* snapshot = events.array.front().Find("session");
    Q4T_CHECK(snapshot && snapshot->GetString("review") == "none" &&
              snapshot->Find("metadata")->GetString("team") == "unfiltered");
  }
  auto query = Parse(R"({"metadata[team]":"records","limit":1,"order":"asc"})");
  auto page = ListAgentSessions(store, query, Access());
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more") &&
            page.value.GetArray("data")->array.size() == 1);
  const auto first = page.value.GetString("last_id");
  Set(&query, "after", *page.value.Find("last_id"));
  page = ListAgentSessions(store, query, Access());
  Q4T_CHECK(Ok(page) && !page.value.GetBool("has_more") &&
            page.value.GetArray("data")->array.size() == 1 &&
            page.value.GetString("last_id") != first);
  auto peer = ListAgentSessions(store, Parse("{}"), Access("client:other"));
  Q4T_CHECK(Ok(peer) && peer.value.GetArray("data")->array.empty());
  auto reset = UpdateAgentSession(store, first,
      Parse(R"({"metadata":null,"spend_control":{"limit":null},"agent":{"tools":null}})"),
      config, Access());
  Q4T_CHECK(Ok(reset) && reset.value.Find("metadata")->object.empty() &&
            !reset.value.Has("spend_control"));
  auto invalid = UpdateAgentSession(store, first, Parse(R"({"agent":{"model":"foreign"}})"),
                                    config, Access());
  Q4T_CHECK(invalid.http_status == 404);
  return true;
}

Q4T_TEST(vault_credentials_cipher_binding_rotation_scope_and_cascade) {
  Fixture f;
  ObjectStore objects(f.options);
  auto api = [&](std::string method, std::string path, std::string body = "",
                 StateAccess access = Access()) {
    return HandleAgentVault(objects, method, path, body, access);
  };
  const auto vault = api("POST", "/v1/vaults", "{}").value.GetString("id");
  const auto path = "/v1/vaults/" + vault;
  const auto credentials = path + "/credentials";
  auto entry = api("POST", credentials,
      R"({"name":" bearer ","auth":{"type":"static_bearer","token":"fixture-bearer-original","mcp_server_url":"https://records.example.test/mcp"},"metadata":{"project":"q4t"}})");
  Q4T_CHECK(Ok(entry));
  const auto id = entry.value.GetString("id");
  const auto resource = credentials + "/" + id;
  Q4T_CHECK(id.starts_with("cred_") && entry.value.GetString("name") == "bearer" &&
            !entry.value.Find("auth")->Has("token"));
  auto raw = [&] {
    ObjectRequest r;
    r.kind = ObjectKind::kVault;
    r.id = vault;
    r.scope = Access().scope;
    return objects.Execute(r, Access().deadline).value;
  };
  Json saved = raw();
  Q4T_CHECK(OutputJson(saved).find("fixture-bearer-original") == std::string::npos);
  auto sealed = saved.Find("data")->GetArray("credentials")->array.front().GetString("sealed");
  std::string plain;
  const StateCipherBinding binding{"q4t-vault", vault, "credential-auth", id};
  Q4T_CHECK(Ok(StateCipher(objects).Open(sealed, binding, Access(), &plain)) &&
            Parse(plain).GetString("token") == "fixture-bearer-original");
  auto wrong = binding;
  wrong.template_id = "vault_wrong";
  Q4T_CHECK(!StateCipher(objects).Open(sealed, wrong, Access(), &plain).ok());
  wrong = binding;
  wrong.item_id = "cred_other";
  Q4T_CHECK(!StateCipher(objects).Open(sealed, wrong, Access(), &plain).ok());
  Q4T_CHECK(!StateCipher(objects).Open(sealed, binding, Access("client:peer"), &plain).ok());
  Q4T_CHECK(api("POST", resource,
      R"({"auth":{"type":"environment_variable","secret_value":"different-method"}})").http_status == 400);
  auto rotated = api("POST", resource,
      R"({"auth":{"type":"static_bearer","token":"fixture-bearer-next"},"metadata":{}})");
  Q4T_CHECK(Ok(rotated) && rotated.value.Find("metadata")->object.empty());
  Q4T_CHECK(api("GET", resource, "", Access("client:peer")).http_status == 404);
  saved = raw();
  sealed = saved.Find("data")->GetArray("credentials")->array.front().GetString("sealed");
  Q4T_CHECK(Ok(StateCipher(objects).Open(sealed, binding, Access(), &plain)) &&
            Parse(plain).GetString("token") == "fixture-bearer-next");
  Q4T_CHECK(Ok(api("DELETE", path)));
  saved = raw();
  Q4T_CHECK(!saved.Find("data")->GetArray("credentials")->array.front().Has("sealed"));
  auto archived = api("GET", credentials + "?status=archived");
  Q4T_CHECK(Ok(archived) && archived.value.GetString("last_id") == id);
  Q4T_CHECK(api("POST", resource, R"({"metadata":{}})").http_status == 409);
  for (const auto& p : std::filesystem::recursive_directory_iterator(f.options.directory)) {
    if (!p.is_regular_file()) continue;
    std::ifstream file(p.path(), std::ios::binary);
    const std::string content{std::istreambuf_iterator<char>(file), {}};
    Q4T_CHECK(content.find("fixture-bearer-") == std::string::npos);
  }
  return true;
}

Q4T_TEST(agent_vault_refresh_forms_bind_authentication_method_and_resource) {
  const auto grant = Parse(R"({"client_id":"a b:c","refresh_token":"r+&",
    "scope":"one two","resource":"https://r/x","token_endpoint_auth":
    {"type":"client_secret_basic","client_secret":"p:+"}})");
  std::map<std::string, std::string> headers;
  std::string body;
  Q4T_CHECK(Ok(mcp_detail::BuildGrant(grant, &headers, &body)));
  Q4T_CHECK(headers.at("Authorization") == "Basic YStiJTNBYzpwJTNBJTJC" &&
            headers.at("Content-Type") == "application/x-www-form-urlencoded");
  Q4T_CHECK(body ==
            "grant_type=refresh_token&refresh_token=r%2B%26"
            "&resource=https%3A%2F%2Fr%2Fx&scope=one+two");
  auto saved = grant;
  Set(&saved, "token_endpoint_auth",
      Parse(R"({"type":"client_secret_post","client_secret":"p:+"})"));
  Q4T_CHECK(Ok(mcp_detail::BuildGrant(saved, &headers, &body)) &&
            !headers.contains("Authorization") &&
            body.find("&client_id=a+b%3Ac&client_secret=p%3A%2B&") !=
                body.npos);
  Set(&saved, "token_endpoint_auth", Parse(R"({"type":"none"})"));
  Q4T_CHECK(Ok(mcp_detail::BuildGrant(saved, &headers, &body)) &&
            !headers.contains("Authorization") &&
            body.find("client_secret") == body.npos);
  Set(&saved, "token_endpoint_auth", Parse(R"({"type":"client_secret_post"})"));
  Q4T_CHECK(!mcp_detail::BuildGrant(saved, &headers, &body).ok());
  return true;
}

Q4T_TEST(agent_vault_refresh_validates_bearer_expiry_and_rotated_grant) {
  Json result;
  auto decode = [&](std::string_view fields) {
    return mcp_detail::DecodeGrant("{" + std::string(fields) + "}", 100,
                                   &result);
  };
  Q4T_CHECK(
      Ok(decode(R"("token_type":"bEaReR","access_token":"new","expires_in":60,
               "refresh_token":"next","scope":"read")")));
  Q4T_CHECK(result.GetString("expires_at") == "1970-01-01T00:02:40Z" &&
            result.Find("refresh")->GetString("refresh_token") == "next" &&
            !result.Find("refresh")->Has("scope"));
  Q4T_CHECK(Ok(decode(R"("token_type":"Bearer","access_token":"next")")) &&
            !result.Has("refresh") && !result.Has("expires_at"));
  for (const auto* fields :
       {R"("token_type":"Other","access_token":"t")", R"("access_token":"t")",
        R"("token_type":"bearer","access_token":"a b")",
        R"("token_type":"bearer","access_token":"t","expires_in":-1)",
        R"("token_type":"bearer","access_token":"t","expires_in":1.5)",
        R"("token_type":"bearer","access_token":"t","expires_in":253402300800)",
        R"("token_type":"bearer","access_token":"t","refresh_token":null)",
        R"("token_type":"bearer","access_token":"t","access_token":"x")"})
    Q4T_CHECK(decode(fields).code == "oauth_refresh_response_invalid");
  return true;
}

Q4T_TEST(agent_vault_refresh_defers_only_explicit_issuer_refusal) {
  const std::string retry = R"({"error":"temporarily_unavailable"})";
  for (const auto& [header, expected] :
       std::vector<std::pair<std::string, int64_t>>{
           {"0", 101},
           {"20", 120},
           {"", 105},
           {"invalid", 105},
           {"Thu, 01 Jan 1970 00:03:20 GMT", 200},
           {"Thu, 01 Jan 1970 00:00:00 GMT", 101},
           {"123456789012345678901234567890", 253402300799LL}}) {
    for (const auto status : {400, 429, 503}) {
      McpTransportReceipt receipt;
      receipt.outcome_unknown = true;
      auto result =
          mcp_detail::DecodeGrantRefusal(status, retry, header, 100, &receipt);
      Q4T_CHECK(result.http_status == 503 && !receipt.outcome_unknown &&
                receipt.result.GetInt("retry_at") == expected);
    }
  }
  for (
      const auto& [status, body] : std::vector<std::pair<int, std::string>>{
          {502, retry},
          {401, retry},
          {503, "{"},
          {400, R"({"error":"invalid_grant"})"},
          {503,
           R"({"error":"temporarily_unavailable","access_token":"issued"})"},
          {429,
           R"({"error":"temporarily_unavailable","error":"invalid_grant"})"},
          {503,
           R"({"error":"temporarily_unavailable","error_description":false})"}}) {
    McpTransportReceipt receipt;
    receipt.outcome_unknown = true;
    Q4T_CHECK(mcp_detail::DecodeGrantRefusal(status, body, "5", 100, &receipt)
                      .http_status == 502 &&
              receipt.outcome_unknown && !receipt.result.Has("retry_at"));
  }
  return true;
}

Q4T_TEST(agent_vault_refresh_inspection_and_uncertain_grants_do_not_replay) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto vault =
      HandleAgentVault(objects, "POST", "/v1/vaults", "{}", Access())
          .value.GetString("id");
  const std::string path = "/v1/vaults/" + vault + "/credentials";
  const std::string target = "https://records.example/mcp";
  const auto body =
      R"({"name":"read","auth":{"type":"mcp_oauth","access_token":"expired",
    "mcp_server_url":"https://records.example/mcp","expires_at":"2001-01-01T00:00:00Z",
    "refresh":{"token_endpoint":"https://issuer.example/token","client_id":"reader",
    "refresh_token":"original","token_endpoint_auth":{"type":"none"}}}})";
  const auto cred = HandleAgentVault(objects, "POST", path, body, Access())
                        .value.GetString("id");
  const auto attached = Parse("[\"" + vault + "\"]");
  std::string token = "discard";
  bool needs_refresh = false;
  auto inspect = [&] {
    return vault_detail::ResolveMcpToken(objects, Access(), attached, cred,
                                         target, &token, nullptr,
                                         &needs_refresh);
  };
  Q4T_CHECK(!cred.empty() && Ok(inspect()) && needs_refresh && token.empty());
  ObjectRequest req;
  req.kind = ObjectKind::kVault;
  req.scope = Access().scope;
  req.id = vault;
  auto marker = [&](const char* name, const Json& value) {
    auto saved = objects.Execute(req, Access().deadline, {});
    if (!saved.ok()) return false;
    auto write = req;
    write.data = *saved.value.Find("data");
    auto entries = *write.data.Find("credentials");
    auto& row = entries.array.front();
    std::erase_if(row.object, [](const auto& field) {
      return field.first == "refreshing" || field.first == "retry_at";
    });
    Set(&row, name, value);
    Set(&write.data, "credentials", std::move(entries));
    write.operation = ObjectOperation::kPut;
    write.expected_revision = saved.value.GetInt("revision");
    write.ttl_seconds = 0;
    return Ok(objects.Execute(write, Access().deadline, {}));
  };
  for (const auto* malformed :
       {"0", "1.5", "253402300800", "null", "\"123\""}) {
    Q4T_CHECK(marker("retry_at", Parse(malformed)) &&
              inspect().code == "credential_state_invalid" && token.empty());
  }
  Q4T_CHECK(marker("refreshing", Parse("false")) &&
            inspect().code == "credential_state_invalid");
  Q4T_CHECK(marker("retry_at", Parse("253402300799")) &&
            inspect().code == "oauth_refresh_unavailable" && token.empty());
  Q4T_CHECK(Ok(HandleAgentVault(objects, "POST", path + "/" + cred,
                                R"({"metadata":{"a":"b"}})", Access())));
  Q4T_CHECK(inspect().code == "oauth_refresh_unavailable");
  Q4T_CHECK(marker("retry_at", Parse("1")) && Ok(inspect()) && needs_refresh);
  Q4T_CHECK(marker("refreshing", Parse("true")));
  Q4T_CHECK(inspect().code == "oauth_refresh_pending" && token.empty());
  Q4T_CHECK(Ok(HandleAgentVault(objects, "POST", path + "/" + cred,
                                R"({"metadata":{"a":"b"}})", Access())));
  Q4T_CHECK(inspect().code == "oauth_refresh_pending" && token.empty());
  Q4T_CHECK(Ok(HandleAgentVault(
      objects, "POST", path + "/" + cred,
      R"({"auth":{"type":"mcp_oauth","refresh":{"refresh_token":"replacement"}}})",
      Access())));
  Q4T_CHECK(Ok(inspect()) && needs_refresh && token.empty());
  Q4T_CHECK(Ok(HandleAgentVault(objects, "DELETE", "/v1/vaults/" + vault, "",
                                Access())));
  Q4T_CHECK(inspect().http_status == 404 && token.empty());
  return true;
}

Q4T_TEST(agent_vault_renewable_binding_changes_on_owner_auth_edits_only) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto vault =
      HandleAgentVault(objects, "POST", "/v1/vaults", "{}", Access())
          .value.GetString("id");
  const auto path = "/v1/vaults/" + vault + "/credentials";
  const auto body =
      R"({"name":"read","auth":{"type":"mcp_oauth","access_token":"same",
    "mcp_server_url":"https://records.example/mcp",
    "refresh":{"token_endpoint":"https://issuer.example/token","client_id":"reader",
    "refresh_token":"original","token_endpoint_auth":{"type":"none"}}}})";
  const auto a = HandleAgentVault(objects, "POST", path, body, Access())
                     .value.GetString("id");
  const auto b = HandleAgentVault(objects, "POST", path, body, Access())
                     .value.GetString("id");
  auto attached = Parse("[\"" + vault + "\"]");
  std::string token, binding;
  auto get = [&](const std::string& id, StateAccess access = Access()) {
    return vault_detail::ResolveMcpToken(objects, access, attached, id,
                                         "https://records.example/mcp", &token,
                                         nullptr, nullptr, &binding);
  };
  Q4T_CHECK(Ok(get(a)) && binding.size() == 64 && token == "same");
  const auto first = binding;
  Q4T_CHECK(Ok(get(b)) && binding != first && token == "same");
  Q4T_CHECK(Ok(HandleAgentVault(objects, "POST", path + "/" + a,
                                R"({"metadata":{"x":"1"}})", Access())));
  Q4T_CHECK(Ok(get(a)) && binding == first);
  Q4T_CHECK(Ok(HandleAgentVault(
      objects, "POST", path + "/" + a,
      R"({"auth":{"type":"mcp_oauth","access_token":"same"}})", Access())));
  Q4T_CHECK(Ok(get(a)) && binding != first && token == "same");
  Q4T_CHECK(get(a, Access("client:other")).http_status == 404 &&
            binding.empty() && token.empty());
  Q4T_CHECK(
      Ok(HandleAgentVault(objects, "DELETE", path + "/" + a, "", Access())));
  Q4T_CHECK(get(a).http_status == 404 && binding.empty() && token.empty());
  return true;
}

Q4T_TEST(agent_mcp_resolves_only_attached_active_exact_url_credentials) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto vault = HandleAgentVault(objects, "POST", "/v1/vaults", "{}", Access()).value.GetString("id");
  const auto path = "/v1/vaults/" + vault + "/credentials";
  const auto body = R"({"name":"test","auth":{"type":"static_bearer","token":"fixture-only",
     "mcp_server_url":"https://SERVER.example:443/tools?project=one"}})";
  const auto first = HandleAgentVault(objects, "POST", path, body, Access()).value.GetString("id");
  const std::string target = "https://server.example/tools?project=one";
  const auto attached = Parse("[\"" + vault + "\"]");
  std::string token;
  auto get = [&](const std::string& id, const std::string& url) {
    return vault_detail::ResolveMcpToken(objects, Access(), attached, id, url, &token);
  };
  Q4T_CHECK(!first.empty() && Ok(get("", target)) && token == "fixture-only");
  Q4T_CHECK(Ok(get(first, target)) && token == "fixture-only");
  for (const auto* url : {"https://server.example/other?project=one",
                          "https://server.example/tools?project=two",
                          "https://server.example:444/tools?project=one",
                          "http://server.example/tools?project=one",
                          "https://other.example/tools?project=one"}) {
    Q4T_CHECK(get(first, url).http_status == 404 && token.empty());
    Q4T_CHECK(Ok(get("", url)) && token.empty());
  }
  Q4T_CHECK(get(first, target + "#ignored").http_status == 400 && token.empty());
  Q4T_CHECK(vault_detail::ResolveMcpToken(objects, Access("client:other"), attached,
      first, target, &token).http_status == 404 && token.empty());
  Q4T_CHECK(vault_detail::ResolveMcpToken(objects, Access(), Parse("[]"), first,
      target, &token).http_status == 404 && token.empty());
  const auto second = HandleAgentVault(objects, "POST", path, body, Access()).value.GetString("id");
  Q4T_CHECK(!second.empty() && get("", target).code == "mcp_credential_ambiguous" && token.empty());
  Q4T_CHECK(Ok(get(first, target)) && token == "fixture-only");
  Q4T_CHECK(Ok(HandleAgentVault(objects, "DELETE", path + "/" + first, "", Access())));
  Q4T_CHECK(get(first, target).http_status == 404 && token.empty());
  Q4T_CHECK(Ok(get("", target)) && token == "fixture-only");
  Q4T_CHECK(Ok(HandleAgentVault(objects, "DELETE", path + "/" + second, "", Access())));
  Q4T_CHECK(get("", target).http_status == 404 && token.empty());
  const auto oauth = HandleAgentVault(objects, "POST", path,
    R"({"name":"exp","auth":{"type":"mcp_oauth","access_token":"expired",
    "mcp_server_url":"https://server.example/tools?project=one","expires_at":"2000-01-01T00:00:00Z"}})", Access());
  Q4T_CHECK(Ok(oauth) && get("", target).code == "mcp_credential_expired" && token.empty());
  Q4T_CHECK(Ok(HandleAgentVault(objects, "POST", path + "/" + oauth.value.GetString("id"),
    R"({"auth":{"type":"mcp_oauth","access_token":"current","expires_at":"9999-12-31T23:59:59Z"}})", Access())));
  Q4T_CHECK(Ok(get("", target)) && token == "current");
  return true;
}

Q4T_TEST(agent_vault_oauth_expiry_uses_offset_fraction_and_full_year_range) {
  using namespace std::chrono;
  const auto now = sys_days{year{2026}/November/23} + 500ms;
  Q4T_CHECK(!SchemaDateTimeAfter("2026-11-23T08:00:00.50+08:00", now));
  Q4T_CHECK(SchemaDateTimeAfter("2026-11-22T19:00:00.5000000001-05:00", now));
  Q4T_CHECK(!SchemaDateTimeAfter("2026-11-23T00:00:00.499999999Z", now));
  Q4T_CHECK(SchemaDateTimeAfter("9999-12-31T00:00:00Z", now));
  Q4T_CHECK(!SchemaDateTimeAfter("0001-01-01T00:00:00Z", now));
  Q4T_CHECK(!SchemaDateTimeAfter("2026-02-31T00:00:00Z", now));
  return true;
}

Q4T_TEST(vault_credentials_oauth_environment_projection_and_negative_inputs) {
  Fixture f;
  ObjectStore objects(f.options);
  auto api = [&](std::string method, std::string path, std::string body = "") {
    return HandleAgentVault(objects, method, path, body, Access());
  };
  auto vault = api("POST", "/v1/vaults", "{}").value.GetString("id");
  const auto path = "/v1/vaults/" + vault + "/credentials";
  auto oauth = api("POST", path, R"({"name":"OAuth","auth":{"type":"mcp_oauth",
    "mcp_server_url":"https://m.example.test","access_token":"fixture-access-1",
    "expires_at":"2027-01-01T00:00:00Z","refresh":{"client_id":"client",
    "refresh_token":"fixture-refresh-1","token_endpoint":"https://i.example.test/token",
    "token_endpoint_auth":{"type":"client_secret_basic","client_secret":"fixture-secret-1"}}}})");
  Q4T_CHECK(Ok(oauth) && OutputJson(oauth.value).find("fixture-") == std::string::npos);
  const auto id = oauth.value.GetString("id");
  auto rotated = api("POST", path + "/" + id,
      R"({"auth":{"type":"mcp_oauth","access_token":"fixture-access-2","refresh":{"refresh_token":null,"scope":"read","token_endpoint_auth":{"type":"client_secret_basic","client_secret":"fixture-secret-2"}}}})");
  Q4T_CHECK(Ok(rotated));
  auto auth = rotated.value.Find("auth");
  Q4T_CHECK(auth->Find("expires_at")->IsNull() &&
            auth->Find("refresh")->GetString("scope") == "read" &&
            !auth->Find("refresh")->Has("refresh_token"));
  auto bad = [&](const char* text) { return api("POST", path, text).http_status == 400; };
  Q4T_CHECK(bad(R"({"name":"bad","auth":{"type":"static_bearer","token":"x","mcp_server_url":"http://a.example.test"}})"));
  Q4T_CHECK(bad(R"({"name":"bad","auth":{"type":"static_bearer","token":"x","mcp_server_url":"https://a@b.example.test"}})"));
  Q4T_CHECK(bad(R"({"name":"bad","auth":{"type":"environment_variable","secret_name":"CODEX_HOME","secret_value":"x","networking":{"type":"unrestricted"}}})"));
  Q4T_CHECK(bad(R"({"name":"bad","auth":{"type":"environment_variable","secret_name":"VALID","secret_value":"line\nfeed","networking":{"type":"unrestricted"}}})"));
  auto env = api("POST", path,R"({"name":"env","auth":{"type":"environment_variable",
    "secret_name":"PROJECT_KEY","secret_value":"fixture-env-1",
    "networking":{"type":"limited","allowed_hosts":["EXAMPLE.COM"]}}})");
  Q4T_CHECK(Ok(env) && !env.value.Find("auth")->Has("secret_value") &&
            env.value.Find("auth")->Find("networking")->GetArray("allowed_hosts")->array.front().str == "example.com");
  auto env_path = path + "/" + env.value.GetString("id");
  Q4T_CHECK(Ok(api("POST", env_path, R"({"auth":{"type":"environment_variable","secret_value":"fixture-env-2"}})")));
  Q4T_CHECK(Ok(api("DELETE", env_path)));
  auto remaining = api("GET", path + "?status=active");
  Q4T_CHECK(Ok(remaining) && remaining.value.GetArray("data")->array.size() == 1 &&
            remaining.value.GetString("first_id") == id);
  return true;
}

Q4T_TEST(agent_vault_sdk_fields_owner_archive_and_session_binding) {
  Fixture f;
  ObjectStore store(f.options);
  const std::string base = "/v1/vaults";
  auto api = [&](std::string method, std::string path, std::string body = "",
                 StateAccess access = Access()) {
    return HandleAgentVault(store, method, path, body, access);
  };
  auto first = api("POST", base,
      R"({"name":"　 项目资料　","metadata":{"team":"资料","a":"1"}})");
  Q4T_CHECK(Ok(first));
  const auto id = first.value.GetString("id");
  const auto path = base + "/" + id;
  Q4T_CHECK(first.value.GetString("name") == "项目资料" && first.value.object.size() == 5);
  Q4T_CHECK(api("GET", path, "", Access("client:peer")).http_status == 404);
  auto blank = api("POST", base, "null");
  Q4T_CHECK(Ok(blank) && blank.value.Find("name")->IsNull());
  Q4T_CHECK(api("POST", base, R"({"name":null})").http_status == 400);
  Q4T_CHECK(api("POST", base, R"({"name":" \t "})").http_status == 400);
  Q4T_CHECK(api("POST", base, "{\"name\":\"" + std::string(257, 'x') + "\"}").http_status == 400);
  Q4T_CHECK(api("POST", path, R"({"metadata":null})").http_status == 400);
  Q4T_CHECK(api("POST", path, R"({"metadata":{"":"bad"}})").http_status == 400);
  Q4T_CHECK(api("POST", path, R"({"secret":"value"})").http_status == 400);
  auto update = api("POST", path, R"({"name":null})");
  Q4T_CHECK(Ok(update) && update.value.Find("name")->IsNull());
  auto matching = api("GET", base + "?metadata%5Bteam%5D=资料&metadata%5Ba%5D=1&status=active");
  Q4T_CHECK(Ok(matching) && matching.value.GetArray("data")->array.size() == 1);
  auto request = Parse(R"({"agent":{"model":"cpu"},"environment":{"type":"none"},"input":"lookup"})");
  Set(&request, "vault_ids", Parse("[\"" + id + "\"]"));
  AgentSessionStart start;
  Q4T_CHECK(Ok(PrepareDirectAgentSession(store, request, Config(), Access(), "s", 1, &start)));
  auto deleted = api("DELETE", path);
  Q4T_CHECK(Ok(deleted) && deleted.value.GetBool("deleted"));
  Q4T_CHECK(api("POST", path, "{}").http_status == 409);
  auto recovered = api("GET", path);
  Q4T_CHECK(Ok(recovered) && !recovered.value.Has("archived"));
  Q4T_CHECK(PrepareDirectAgentSession(store, request, Config(), Access(), "s", 1, &start).http_status == 404);
  auto archived = api("GET", base + "?status%5B%5D=archived");
  Q4T_CHECK(Ok(archived) && archived.value.GetString("first_id") == id);
  auto both = api("GET", base + "?status[]=active&status[]=archived&limit=1&order=asc");
  Q4T_CHECK(Ok(both) && both.value.GetBool("has_more"));
  auto next = api("GET", base + "?order=asc&after=" + both.value.GetString("last_id"));
  Q4T_CHECK(Ok(next) && next.value.GetArray("data")->array.size() == 1 &&
            next.value.GetString("first_id") != both.value.GetString("first_id"));
  Q4T_CHECK(api("GET", base + "?status=other").http_status == 400);
  Q4T_CHECK(api("GET", base + "?status=active&status[]=archived").http_status == 400);
  Q4T_CHECK(api("GET", base + "?metadata%5B%5D=x").http_status == 400);
  return true;
}

Q4T_TEST(agent_session_vaults_require_owned_identity_and_spend_limit_null_resets) {
  Fixture f;
  ObjectStore store(f.options);
  auto request = Parse(R"({"agent":{"model":"cpu"},"environment":{"type":"none"},
    "input":"records","vault_ids":["vault_1"],"spend_control":{"limit":null}})");
  ObjectRequest vault;
  vault.kind = ObjectKind::kVault;
  vault.operation = ObjectOperation::kPut;
  vault.scope = Access().scope;
  vault.id = "vault_1";
  vault.data = Parse(R"({"id":"vault_1","object":"vault"})");
  Q4T_CHECK(Ok(store.Execute(vault, Access().deadline)));
  AgentSessionStart start;
  Q4T_CHECK(Ok(PrepareDirectAgentSession(store, request, Config(), Access(), "s1", 1, &start)));
  Q4T_CHECK(!start.session.Has("spend_control") &&
            start.session.GetArray("vault_ids")->array.at(0).str == "vault_1");
  Q4T_CHECK(PrepareDirectAgentSession(store, request, Config(), Access("client:other"), "s1", 1, &start).http_status == 404);
  return true;
}

Q4T_TEST(agent_registration_owned_crud_defaults_and_stored_pagination) {
  Fixture f;
  ObjectStore store(f.options);
  const auto api = [&](std::string method, std::string path, std::string body = "",
                       StateAccess access = Access()) {
    return HandleAgentRegistration(store, method, path, body, Config(), access);
  };
  auto first = api("POST", "/v1/agents", R"({"model":"cpu","metadata":{"a":"1"},
    "multi_agent":{"enabled":true},"compaction":{},"tools":[{"type":"function",
    "name":"lookup","description":"Query","parameters":{}},{"type":"web_search"}]})");
  Q4T_CHECK(Ok(first));
  const auto id = first.value.GetString("id");
  const auto path = "/v1/agents/" + id;
  Q4T_CHECK(id.starts_with("agent_") && first.value.GetString("object") == "agent");
  Q4T_CHECK(first.value.Find("compaction")->GetBool("async"));
  Q4T_CHECK(first.value.Find("multi_agent")->GetInt("max_concurrent_subagents") == 6);
  Q4T_CHECK(!first.value.GetArray("tools")->array[0].GetBool("defer_loading"));
  Q4T_CHECK(first.value.GetArray("tools")->array[1].GetString("mode") == "live");
  Q4T_CHECK(api("GET", path, "", Access("client:foreign")).http_status == 404);
  auto changed = api("POST", path, R"({"name":"reader","metadata":null})");
  Q4T_CHECK(Ok(changed) && changed.value.GetString("name") == "reader" &&
            changed.value.Find("metadata")->object.empty());
  Q4T_CHECK(changed.value.GetArray("tools")->array.size() == 2);
  Q4T_CHECK(api("POST", path, R"({"multi_agent":{"enabled":true,"max_concurrent_subagents":0}})").http_status == 400);
  Q4T_CHECK(api("POST", path, R"({"model":"missing"})").http_status == 404);
  Q4T_CHECK(api("POST", path, R"({"tools":[{"type":"mcp","server_label":"docs",
    "transport":{"type":"http","server_url":"https://example.org","headers":{"Authorization":"secret"}}}]})").http_status == 400);
  Q4T_CHECK(api("GET", "/v1/agents?include=x").http_status == 400);
  Q4T_CHECK(api("GET", "/v1/agents/agent_").http_status == 404);
  Q4T_CHECK(api("PUT", path).http_status == 405);
  Q4T_CHECK(api("DELETE", path, "", Access("client:foreign")).http_status == 404);
  Q4T_CHECK(api("POST", path, R"({"unknown":true})").http_status == 400);
  Q4T_CHECK(api("POST", path, R"({"tools":[{"type":"mcp","server_label":"docs",
    "transport":{"type":"http","server_url":"https://exa mple.org"}}]})").http_status == 400);
  Q4T_CHECK(api("POST", path, R"({"tools":[{"type":"mcp","server_label":"docs",
    "transport":{"type":"http","server_url":"https://example.org","headers":{"x-note":"a\u0000b"}}}]})").http_status == 400);
  auto second = api("POST", "/v1/agents", R"({"model":"cpu"})");
  Q4T_CHECK(Ok(second));
  auto page = api("GET", "/v1/agents?order=asc&limit=1");
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more"));
  const auto cursor = page.value.GetString("last_id");
  auto next = api("GET", "/v1/agents?order=asc&limit=1&after=" + cursor);
  Q4T_CHECK(Ok(next) && !next.value.GetBool("has_more") &&
            next.value.GetString("last_id") != cursor);
  auto removed = api("DELETE", path);
  Q4T_CHECK(Ok(removed) && removed.value.GetString("object") == "agent.deleted" &&
            removed.value.GetString("id") == id && removed.value.GetBool("deleted"));
  Q4T_CHECK(api("GET", path).http_status == 404);
  return true;
}

Q4T_TEST(prompt_realtime_actual_business_definitions_follow_native_wire_schema) {
  const auto path = std::filesystem::path(__FILE__).parent_path().parent_path() /
                    "tools/serve_realtime/prompt_definitions.json";
  std::ifstream file(path);
  Q4T_CHECK(file.good());
  std::ostringstream bytes;
  bytes << file.rdbuf();
  auto definitions = Parse(bytes.str());
  Q4T_CHECK(definitions.IsObject() && definitions.object.size() == 5);
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  for (const auto& [name, definition] : definitions.object) {
    (void)name;
    Q4T_CHECK(Ok(registry.Create(definition, Config(), Access())));
    auto bad = definition;
    SetRequest(&bad, "reasoning", Parse(R"({"effort":"none"})"));
    // Retain the actual original business failure as a negative: the Realtime
    // wire enum differs from internal Responses' default no-reasoning mapping.
    const auto result = registry.Create(bad, Config(), Access());
    Q4T_CHECK(result.http_status == 400 && result.code == "invalid_prompt");
  }
  return true;
}

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
  Q4T_CHECK(reference &&
            OutputJson(*reference) ==
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
                OutputJson(*response->Find("prompt")) ==
                    OutputJson(*reference));
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

Q4T_TEST(prompt_snapshot_pins_version_and_survives_registry_deletion) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  const auto reference = *Request(id).Find("prompt");
  PromptSnapshot original;
  Q4T_CHECK(Ok(registry.Resolve(reference, Access(), &original)));
  const auto bytes = OutputJson(original.request);
  Q4T_CHECK(original.api == PromptApi::kResponses &&
            original.scope == Access().scope &&
            original.reference.GetString("version") == "1");
  Q4T_CHECK(Ok(
      registry.Append(id, 1, Definition("new {{value}}"), Config(), Access())));
  PromptSnapshot latest;
  Q4T_CHECK(Ok(registry.Resolve(reference, Access(), &latest)));
  Q4T_CHECK(latest.reference.GetString("version") == "2" &&
            OutputJson(latest.request) != bytes);
  Q4T_CHECK(Ok(registry.Select(id, 2, "1", Access())));
  Q4T_CHECK(Ok(registry.Erase(id, Access())));
  Q4T_CHECK(OutputJson(original.request) == bytes &&
            original.reference.GetString("version") == "1");
  Q4T_CHECK(registry.Resolve(reference, Access(), &original).http_status ==
            404);
  Q4T_CHECK(OutputJson(original.request) == bytes);
  return true;
}

Q4T_TEST(prompt_snapshot_scope_cancellation_and_deadline_are_atomic) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto reference = *Request(created.value.GetString("id")).Find("prompt");
  PromptSnapshot snapshot;
  Q4T_CHECK(Ok(registry.Resolve(reference, Access(), &snapshot)));
  const auto before = OutputJson(snapshot.request);
  Q4T_CHECK(registry.Resolve(reference, Access("client:other"), &snapshot)
                .http_status == 404);
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(!registry.Resolve(reference, cancelled, &snapshot).ok());
  auto expired = Access();
  expired.deadline = Clock::now() - 1ms;
  Q4T_CHECK(!registry.Resolve(reference, expired, &snapshot).ok());
  Q4T_CHECK(snapshot.scope == Access().scope &&
            OutputJson(snapshot.request) == before &&
            snapshot.reference.GetString("version") == "1");
  Q4T_CHECK(registry.Resolve(reference, Access(), nullptr).http_status == 500);
  return true;
}

Q4T_TEST(prompt_realtime_definition_keeps_session_fields_and_hidden_prefix) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto definition = Parse(R"({"api":"realtime","variables":{
    "name":"string","task":"string"},"request":{
    "instructions":"Address {{name}}", "input":[{"role":"user",
    "content":"{{task}}"}], "output_modalities":["text"],
    "max_output_tokens":45,"parallel_tool_calls":false,
    "reasoning":{"effort":"low"},"truncation":"disabled",
    "tools":[{"type":"function","name":"lookup","parameters":{
      "type":"object","properties":{"key":{"type":"string"}}}}],
    "tool_choice":{"type":"function","name":"lookup"},
    "audio":{"input":{"noise_reduction":null,"transcription":null}},
    "tracing":null}})");
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  auto reference = Parse("{\"id\":\"" + created.value.GetString("id") +
                         R"(","variables":{"name":"reader",
                             "task":"{{name}} stays literal"}})");
  PromptSnapshot snapshot;
  Q4T_CHECK(Ok(registry.Resolve(reference, Access(), &snapshot)));
  Q4T_CHECK(snapshot.api == PromptApi::kRealtime &&
            snapshot.request.GetString("type") == "realtime" &&
            snapshot.request.GetString("instructions") == "Address reader");
  Q4T_CHECK(snapshot.request.GetArray("input")
                ->array[0]
                .GetArray("content")
                ->array[0]
                .GetString("text") == "{{name}} stays literal");
  for (const auto* key :
       {"output_modalities", "max_output_tokens", "parallel_tool_calls",
        "reasoning", "truncation", "tools", "tool_choice", "audio", "tracing"})
    Q4T_CHECK(OutputJson(*snapshot.request.Find(key)) ==
              OutputJson(*definition.Find("request")->Find(key)));
  return true;
}

Q4T_TEST(prompt_realtime_management_persists_api_across_restart) {
  Fixture f;
  std::string id;
  {
    ObjectStore store(f.options);
    auto created = Api(store, "POST", "/v1/prompts", Parse(R"({
      "definition":{"api":"realtime","request":{
        "instructions":"Use the actual tool result","max_output_tokens":"inf"
      },"variables":{}}})"));
    Q4T_CHECK(Ok(created));
    id = created.value.GetString("id");
  }
  ObjectStore restored(f.options);
  auto version = Api(restored, "GET", "/v1/prompts/" + id + "/versions/1");
  Q4T_CHECK(Ok(version) &&
            version.value.Find("definition")->GetString("api") == "realtime");
  PromptRegistry registry(restored);
  PromptSnapshot snapshot;
  Q4T_CHECK(Ok(
      registry.Resolve(Parse("{\"id\":\"" + id + "\"}"), Access(), &snapshot)));
  Q4T_CHECK(snapshot.api == PromptApi::kRealtime &&
            snapshot.request.GetString("max_output_tokens") == "inf");
  return true;
}

Q4T_TEST(prompt_api_types_do_not_silently_reinterpret_templates) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto definition = Parse(R"({"api":"realtime","request":{
    "instructions":"template"},"variables":{}})");
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  auto request = Parse("{\"prompt\":{\"id\":\"" + id + "\"}}");
  Q4T_CHECK(registry.Expand(request, Config(), Access()).http_status == 400);
  auto responses = Definition();
  Set(&responses, "api", Parse("\"responses\""));
  auto response_prompt = registry.Create(responses, Config(), Access());
  Q4T_CHECK(Ok(response_prompt));
  Q4T_CHECK(Ok(registry.Expand(Request(response_prompt.value.GetString("id")),
                               Config(), Access())));
  for (const auto* api : {"null", "false", "7", "\"other\""}) {
    Set(&definition, "api", Parse(api));
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  return true;
}

Q4T_TEST(prompt_realtime_rejects_authority_recursive_prompt_and_bad_shape) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto base = Parse(R"({"api":"realtime","request":{},"variables":{}})");
  for (const auto* field : {"prompt", "store", "background", "conversation",
                            "client_id", "request_timeout_ms", "id"}) {
    auto definition = base;
    SetRequest(&definition, field, Parse("null"));
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  for (const auto* request : {R"({"type":"transcription"})",
                              R"({"audio":{"input":{"unknown":true}}})",
                              R"({"output_modalities":["video"]})"}) {
    auto definition = base;
    Set(&definition, "request", Parse(request));
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  return true;
}

Q4T_TEST(prompt_realtime_credentials_cannot_be_persisted_in_tools_or_prefix) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto base = Parse(R"({"api":"realtime","request":{"tools":[{
    "type":"mcp","server_label":"docs","server_url":"https://example.test/mcp"
  }]},"variables":{}})");
  Q4T_CHECK(Ok(registry.Create(base, Config(), Access())));
  for (const auto* field : {"authorization", "headers"}) {
    auto definition = base;
    auto tools = *definition.Find("request")->Find("tools");
    Set(&tools.array[0], field,
        Parse(std::string(field) == "headers"
                  ? R"({"Authorization":"fixture-credential"})"
                  : R"("fixture-credential")"));
    SetRequest(&definition, "tools", tools);
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  auto definition = base;
  SetRequest(&definition, "input", Parse(R"([{
    "type":"tool_search_output","tools":[{"type":"mcp",
    "server_label":"other","server_url":"https://example.test/mcp",
    "authorization":"fixture-credential"}]}])"));
  Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status == 400);
  return true;
}

Q4T_TEST(prompt_realtime_token_limit_does_not_accept_fraction_or_overflow) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  for (const auto* value : {"0", "1.5", "4097", "null", "true"}) {
    auto definition =
        Parse(R"({"api":"realtime","variables":{},"request":{}})");
    SetRequest(&definition, "max_output_tokens", Parse(value));
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  return true;
}
