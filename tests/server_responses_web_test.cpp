#include "q4t/server/responses_execution.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/agent_session.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/http_response.h"
#include "q4t/server/chat_web_search.h"
#include "q4t/server/chat_file_inputs.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/chat_moderation.h"
#include "q4t/server/batch_budget.h"
#include "q4t/server/batch_preparation.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr auto kUrl = "https://example.com/source";
constexpr auto kPassage = "alpha VERIFIED_PRIVATE_PASSAGE";
StateAccess Access(std::string scope = "client:web-api") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
BinaryAccess Binary() {
  const auto access = Access();
  return {access.scope, access.deadline, access.cancelled};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
Json Read(const std::string& raw) {
  Json value;
  if (!Parse(raw, &value)) throw std::runtime_error("web API fixture JSON");
  return value;
}
bool Save(std::string_view name, const Json& value, const std::string& wire) {
  const auto* directory = std::getenv("Q4T_WEB_API_OUTPUT");
  if (!directory) return true;
  const auto root = std::filesystem::path(directory);
  std::filesystem::create_directories(root);
  const auto json = root / (std::string(name) + ".json");
  const auto stream = root / (std::string(name) + ".wire");
  if (std::filesystem::exists(json) || std::filesystem::exists(stream))
    return false;
  std::ofstream a(json), b(stream);
  a << OutputJson(value) << '\n';
  b << wire;
  return a.good() && b.good();
}
struct Fixture {
  ObjectStoreOptions objects;
  std::shared_ptr<WebDeploymentPolicy> policy =
      std::make_shared<WebDeploymentPolicy>();
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/responses-web-cpu");
    std::filesystem::create_directories(root);
    auto name = (root / "responses-web-XXXXXX").string();
    if (!mkdtemp(name.data()))
      throw std::runtime_error("web API fixture directory");
    objects.directory = name + "/objects";
    objects.restart_backoff_ms = 0;
    if (!ParseWebDeploymentPolicy(
            R"({"version":1,"scopes":["client:web-api"],"seeds":["https://example.com/source"]})",
            policy.get()))
      throw std::runtime_error("web API fixture policy");
  }
  ChatPlanConfig Config() const {
    ChatPlanConfig config;
    config.model = "cpu";
    config.structured = true;
    config.web_policy = policy;
    return config;
  }
  bool Seed(ObjectStore& store) const {
    WebPageReceipt receipt;
    receipt.source.urls = {kUrl};
    receipt.source.content.bytes = kPassage;
    receipt.source.content.sha256 = Digest(kPassage);
    receipt.page = {kUrl, "Actual source", kPassage, Digest(kPassage), {}, {}};
    WebIndexSnapshot snapshot;
    return Ok(WebIndexStore(store).Publish(
        Binary(), WebIndexIdentity(*policy, {}),
        std::vector<WebPageReceipt>{receipt}, 0, 3600, &snapshot));
  }
};
Json Request(bool include = false) {
  auto request =
      Read(R"({"input":"Find alpha","max_output_tokens":256,"max_tool_calls":1,
    "reasoning":{"effort":"none"},"stream":true,"stream_options":{"include_obfuscation":false},
    "tools":[{"type":"web_search","external_web_access":false}],"tool_choice":{"type":"web_search"}})");
  if (include)
    Set(&request, "include", Array({String("web_search_call.action.sources")}));
  return request;
}
ResponseDelta Call(size_t index = 0, bool start = true) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.tool_start = start;
  delta.call.index = index;
  delta.call.id = "call_web_" + std::to_string(index);
  delta.call.name = "q4t_web_search_0";
  delta.call.arguments =
      R"({"action":"search","queries":["alpha"],"url":"","pattern":""})";
  return delta;
}
ResponseDelta Text(std::string value) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = std::move(value);
  return delta;
}
bool Search(ResponsesExecution& run, const Fixture& fixture,
            Json request = Request(), std::string* all = nullptr) {
  if (!Ok(run.Prepare(request, fixture.Config(), Access(), 42))) return false;
  std::string wire;
  if (!Ok(run.Begin(&wire))) return false;
  if (all) *all += run.PublicWire(wire);
  if (!run.Append(Call(), "null", &wire)) return false;
  return Ok(run.RunWebSearches(1, [&](const std::string& data) {
    if (all) *all += run.PublicWire(data);
    return true;
  }));
}
bool Finish(ResponsesExecution& run, const Fixture& fixture,
            std::string* all = nullptr) {
  ResponsesGenerationPlan next;
  if (!Ok(run.ContinueFileSearch(20, fixture.Config(), {}, &next)))
    return false;
  std::string wire;
  if (!run.Append(Text("答案 " + WebCitationMarker(kUrl)), "null", &wire))
    return false;
  if (all) *all += run.PublicWire(wire);
  if (!run.CiteWebSearchSources(&wire)) return false;
  if (all) *all += run.PublicWire(wire);
  const auto result = run.Finish(false, 0, {120, 0, 25, 0}, 43, &wire);
  if (all) *all += run.PublicWire(wire);
  return Ok(result);
}
Q4T_TEST(agent_session_web_search_honors_cached_disabled_and_empty_domains) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  Q4T_CHECK(fixture.Seed(objects));
  for (const std::string mode : {"disabled", "cached", "live"}) {
    auto request = Read(R"({"agent":{"model":"cpu","tools":[{"type":"web_search",
        "context_size":"low","location":{"country":"CN"}}]},
        "environment":{"type":"none"},"input":"Find alpha"})");
    auto& tool = Field(Field(request, "agent"), "tools").array.front();
    Set(&tool, "mode", String(mode));
    if (mode == "live") Set(&tool, "allowed_domains", Array());
    auto config = fixture.Config();
    if (mode == "disabled") config.web_policy.reset();
    ResponsesExecution run(objects);
    auto created = PrepareAgentSessionExecution(objects, request, config,
                                                Access(), std::time(nullptr), run);
    Q4T_CHECK(Ok(created) && run.HasWebSearch() == (mode != "disabled"));
    if (mode == "disabled") continue;
    const auto& native = run.Envelope().request.GetArray("tools")->array.at(0);
    Q4T_CHECK(native.GetString("search_context_size") == "low" &&
              native.GetBool("external_web_access") == (mode == "live") &&
              native.Find("user_location")->GetString("country") == "CN");
    run.BindBackgroundControl(std::make_shared<RequestControl>(
        run.Id(), "", Clock::now() + 10s));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
    Q4T_CHECK(run.Append(Call(), "null", &wire));
    Q4T_CHECK(Ok(run.RunWebSearches(1, [](const auto&) { return true; })));
    ResponsesGenerationPlan next;
    Q4T_CHECK(Ok(run.ContinueFileSearch(1, config, {}, &next)));
    const auto prompt = OutputJson(next.generation_request);
    Q4T_CHECK((prompt.find(kPassage) != prompt.npos) == (mode == "cached"));
    Q4T_CHECK(run.Append(Text("complete"), "null", &wire));
    Q4T_CHECK(Ok(run.Finish(false, 0, {50, 0, 8, 0}, std::time(nullptr), &wire)));
    AgentSessionView view;
    const auto id = created.value.GetString("id");
    Q4T_CHECK(Ok(ReadAgentSessionTurn(objects, id, "", Access(), &view)));
    Q4T_CHECK(view.items.array.size() == 3 && view.turn.GetString("status") == "completed");
    const auto& item = view.items.array[1];
    const auto& action = *item.Find("action");
    Q4T_CHECK(item.GetString("type") == "web_search_call" &&
              item.GetString("status") == "completed" && !action.Has("sources") &&
              action.GetString("query") == "alpha" && action.GetArray("queries"));
    Q4T_CHECK(ReadAgentSessionTurn(objects, id, "", Access("client:other"), &view)
                  .http_status == 404);
  }
  return true;
}

Json ResultItem() {
  return Read(R"({"id":"ws_cpu","type":"web_search_call","status":"completed",
    "action":{"type":"search","query":"alpha","queries":["alpha"],
      "sources":[{"type":"url","url":"https://example.com/source"}]}})");
}
WebExecutionResult Result() {
  WebExecutionResult result;
  result.sources = {
      {kUrl, "Actual source", kPassage, Digest(kPassage), {}, {}}};
  result.context = Object(
      {{"results",
        Array({Object({{"url", String(kUrl)},
                       {"title", String("Actual source")},
                       {"text", String(kPassage)},
                       {"source_sha256", String(Digest(kPassage))}})})}});
  return result;
}
}  // namespace

Q4T_TEST(responses_web_binding_aliases_collision_and_tool_choice) {
  auto request = Request();
  auto& tools = Field(request, "tools").array;
  tools.push_back(Read(
      R"({"type":"function","name":"q4t_web_search_0","parameters":{"type":"object","properties":{},"additionalProperties":false},"strict":true})"));
  tools.push_back(Read(R"({"type":"web_search_preview_2025_03_11"})"));
  std::vector<ResponsesWebBinding> bindings;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesWebBindings(request, &bindings, &error) &&
            bindings.size() == 2 &&
            bindings[0].model_name != "q4t_web_search_0");
  Json model, choice;
  Q4T_CHECK(NormalizeResponsesTools(request, &model, &choice, &error));
  Q4T_CHECK(choice.Find("function")->GetString("name") ==
            bindings[0].model_name);
  Set(&request, "tool_choice",
      Read(
          R"({"type":"allowed_tools","mode":"required","tools":[{"type":"web_search_preview"},{"type":"function","name":"q4t_web_search_0"}]})"));
  Q4T_CHECK(NormalizeResponsesTools(request, &model, &choice, &error));
  Q4T_CHECK(choice.Find("allowed_tools")->GetArray("tools")->array.size() == 2);
  Set(&request, "tool_choice", String("none"));
  Q4T_CHECK(NormalizeResponsesTools(request, &model, &choice, &error) &&
            choice.str == "none");
  return true;
}

Q4T_TEST(responses_web_actions_do_not_accept_policy_or_invalid_arguments) {
  WebAction action;
  Q4T_CHECK(PlanResponsesWebAction(Read(Call().call.arguments), &action));
  for (
      const auto* raw :
      {R"({"action":"search","queries":[],"url":"","pattern":""})",
       R"({"action":"search","queries":["alpha"],"url":"","pattern":"","external_web_access":true})",
       R"({"action":"open_page","queries":[],"url":"file:///etc/passwd","pattern":""})",
       R"({"action":"find_in_page","queries":[],"url":"https://example.com/","pattern":""})"})
    Q4T_CHECK(!PlanResponsesWebAction(Read(raw), &action) &&
              action.queries.empty());
  Q4T_CHECK(PlanResponsesWebAction(
      Read(
          R"({"action":"find_in_page","queries":[],"url":"https://example.com/","pattern":"中文"})"),
      &action));
  Q4T_CHECK(action.kind == WebAction::Kind::kFind && action.pattern == "中文");
  return true;
}

Q4T_TEST(responses_web_output_lifecycle_matches_standard_shapes) {
  for (const auto kind : {WebAction::Kind::kSearch, WebAction::Kind::kOpen,
                          WebAction::Kind::kFind}) {
    ResponsesOutput output;
    std::string wire;
    Q4T_CHECK(output.Init(Object(), "resp_cpu", 42, false) &&
              output.Start(&wire));
    WebAction action{kind, {}, kUrl, "alpha"};
    if (kind == WebAction::Kind::kSearch) action.queries = {"alpha"};
    size_t index;
    Q4T_CHECK(output.StartWebSearch(ResponsesWebAction(action), &index, &wire));
    Q4T_CHECK(wire.find("response.web_search_call.in_progress") != wire.npos);
    Q4T_CHECK(!output.Finish(false, 0, {}, 43, &wire));
    Q4T_CHECK(output.SearchWeb(index, &wire));
    Q4T_CHECK(output.CompleteWebSearch(
        index, ResponsesWebAction(action, Result().sources), &wire));
    Q4T_CHECK(wire.find("response.web_search_call.completed") != wire.npos &&
              wire.find("response.output_item.done") != wire.npos);
    ChatPlanError error;
    Q4T_CHECK(ValidateResponsesItemShape(
        output.Snapshot().GetArray("output")->array[index],
        ResponsesItemShape::kOutput, &error));
    Q4T_CHECK(output.Finish(false, 0, {}, 43, &wire));
  }
  return true;
}

Q4T_TEST(responses_web_context_exact_item_binding_and_conflict) {
  Json bindings;
  auto item = ResultItem();
  Q4T_CHECK(Ok(BindWebContext(item, Result(), &bindings)) &&
            FindWebContext(bindings, item));
  auto changed = item;
  Set(&Field(changed, "action"), "query", String("other"));
  Q4T_CHECK(!FindWebContext(bindings, changed));
  Json selected;
  Q4T_CHECK(MergeWebContexts(bindings, Array({changed}), &selected).code ==
            "web_context_invalid");
  auto forged = Result();
  Set(&Field(forged.context, "results").array[0], "url",
      String("https://forged.invalid/"));
  Q4T_CHECK(BindWebContext(item, forged, &selected).code ==
            "web_context_invalid");
  Q4T_CHECK(Ok(MergeWebContexts(bindings, Array(), &selected)) &&
            selected.array.empty());
  return true;
}

Q4T_TEST(responses_web_native_execution_continues_with_real_owned_passages) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  ResponsesExecution run(store);
  Q4T_CHECK(Search(run, fixture) && run.HasWebSearch() &&
            run.VariableInputSteps() && !run.PendingWebSearch());
  BatchTokenCodec codec;
  codec.encode = [](std::string_view text, std::vector<uint32_t>* tokens) {
    tokens->assign(text.size(), 1);
    return q4t::Status{};
  };
  codec.decode = [](const std::vector<uint32_t>& ids, std::string* text) {
    text->assign(ids.size(), 'x');
    return q4t::Status{};
  };
  BatchRateBudget rate;
  Q4T_CHECK(Ok(MeasureBatchResponseRate(run, fixture.Config(), Binary(), codec,
                                        &rate)) &&
            !rate.input_exact &&
            rate.prompt_capacity == 7ULL * fixture.Config().max_len);
  // Keep the lifetime of the snapshot while inspecting its canonical choice.
  const auto public_response = run.PublicResponse();
  const auto* selection = public_response.Find("tool_choice");
  Q4T_CHECK(selection && selection->GetString("type") == "allowed_tools" &&
            selection->GetString("mode") == "required" &&
            selection->GetArray("tools")->array[0].GetString("type") ==
                "web_search");
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(run.ContinueFileSearch(20, fixture.Config(), {}, &next)));
  Q4T_CHECK(OutputJson(next.generation_request).find(kPassage) !=
                std::string::npos &&
            next.chat.prepared_source_data);
  Q4T_CHECK(next.generation_request.GetString("tool_choice") == "none");
  std::string wire;
  const auto text = "证据 " + WebCitationMarker(kUrl) + " unknown " +
                    WebCitationMarker("https://fake.invalid/");
  Q4T_CHECK(run.Append(Text(text), "null", &wire) &&
            run.CiteWebSearchSources(&wire));
  Q4T_CHECK(wire.find("response.output_text.annotation.added") != wire.npos);
  const auto& part = run.Output()
                         .Snapshot()
                         .GetArray("output")
                         ->array.back()
                         .GetArray("content")
                         ->array[0];
  Q4T_CHECK(part.GetString("text") == text &&
            part.GetArray("annotations")->array.size() == 1 &&
            part.GetArray("annotations")->array[0].GetInt("start_index") == 3);
  Q4T_CHECK(run.CiteWebSearchSources(&wire) && wire.empty());
  Q4T_CHECK(Ok(run.Finish(false, 0, {120, 0, 30, 0}, 43, &wire)));
  return true;
}

Q4T_TEST(responses_web_include_projects_json_sse_and_stored_replay) {
  for (bool include : {false, true}) {
    Fixture fixture;
    ObjectStore store(fixture.objects);
    Q4T_CHECK(fixture.Seed(store));
    ResponsesExecution run(store);
    std::string wire;
    Q4T_CHECK(Search(run, fixture, Request(include), &wire));
    Q4T_CHECK((wire.find("\"sources\"") != wire.npos) == include &&
              wire.find(kPassage) == wire.npos);
    const auto value = run.PublicResponse();
    Q4T_CHECK(value.GetArray("output")->array[0].Find("action")->Has(
                  "sources") == include);
    Q4T_CHECK(Finish(run, fixture, &wire));
    Q4T_CHECK(run.ResultJson() == OutputJson(run.PublicResponse()));
    Q4T_CHECK(Save(include ? "response-included" : "response",
                   run.PublicResponse(), wire));
    ResponsesReadPlan read;
    read.id = run.Id();
    read.stream = true;
    read.obfuscation = false;
    if (include) read.view.include = {"web_search_call.action.sources"};
    ResponsesReplay replay;
    Q4T_CHECK(Ok(ReadResponsesState(store, read, Access(), &replay)) &&
              replay.terminal &&
              (replay.wire.find("\"sources\"") != replay.wire.npos) ==
                  include &&
              replay.wire.find(kPassage) == replay.wire.npos);
  }
  return true;
}

Q4T_TEST(responses_web_previous_response_recovers_context_after_store_restart) {
  Fixture fixture;
  std::string id;
  {
    ObjectStore store(fixture.objects);
    Q4T_CHECK(fixture.Seed(store));
    ResponsesExecution run(store);
    Q4T_CHECK(Search(run, fixture) && Finish(run, fixture));
    id = run.Id();
  }
  ObjectStore store(fixture.objects);
  const auto request = Object({{"input", String("Continue from evidence")},
                               {"previous_response_id", String(id)}});
  ResponsesExecution next(store);
  Q4T_CHECK(Ok(next.Prepare(request, fixture.Config(), Access(), 44)));
  Q4T_CHECK(OutputJson(next.Generation().generation_request).find(kPassage) !=
            std::string::npos);
  ResponsesExecution foreign(store);
  Q4T_CHECK(
      foreign.Prepare(request, fixture.Config(), Access("client:other"), 44)
          .http_status == 404);
  return true;
}

Q4T_TEST(responses_web_conversation_import_and_item_reference_keep_context) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  ResponsesExecution run(store);
  Q4T_CHECK(Search(run, fixture) && Finish(run, fixture));
  const auto item = run.Output().Snapshot().GetArray("output")->array[0];
  auto conversation =
      ConversationState(store).Create(Object(), Array({item}), Access());
  Q4T_CHECK(Ok(conversation));
  ResponsesExecution next(store);
  Q4T_CHECK(Ok(next.Prepare(
      Object({{"conversation", String(conversation.value.GetString("id"))},
              {"input", String("Use the sources")}}),
      fixture.Config(), Access(), 44)));
  Q4T_CHECK(OutputJson(next.Generation().generation_request).find(kPassage) !=
            std::string::npos);
  ResponsesExecution reference(store);
  Q4T_CHECK(Ok(reference.Prepare(
      Object({{"input", Array({Object({{"type", String("item_reference")},
                                       {"id", *item.Find("id")}})})}}),
      fixture.Config(), Access(), 44)));
  Q4T_CHECK(
      OutputJson(reference.Generation().generation_request).find(kPassage) !=
      std::string::npos);
  return true;
}

Q4T_TEST(responses_web_projected_item_can_recover_but_changed_action_cannot) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  ResponsesExecution run(store);
  Q4T_CHECK(Search(run, fixture) && Finish(run, fixture));
  auto item = run.PublicResponse().GetArray("output")->array[0];
  Q4T_CHECK(!item.Find("action")->Has("sources"));
  ResponsesExecution next(store);
  Q4T_CHECK(Ok(next.Prepare(Object({{"input", Array({item})}}),
                            fixture.Config(), Access(), 44)));
  Q4T_CHECK(OutputJson(next.Generation().generation_request).find(kPassage) !=
            std::string::npos);
  Set(&Field(item, "action"), "query", String("changed"));
  ResponsesExecution changed(store);
  Q4T_CHECK(changed
                .Prepare(Object({{"input", Array({item})}}), fixture.Config(),
                         Access(), 44)
                .code == "item_identity_conflict");
  return true;
}

Q4T_TEST(
    responses_web_user_tool_budget_skips_extra_calls_without_empty_success) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.Prepare(Request(), fixture.Config(), Access(), 42)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && run.Append(Call(0), "null", &wire) &&
            run.Append(Call(1), "null", &wire));
  Q4T_CHECK(Ok(run.RunWebSearches(2, {})));
  const auto& output = run.Output().Snapshot().GetArray("output")->array;
  Q4T_CHECK(output.size() == 2 &&
            output[0].GetString("status") == "completed" &&
            output[1].GetString("status") == "incomplete" &&
            output[1].Find("action")->GetArray("sources")->array.empty());
  return true;
}

Q4T_TEST(
    responses_web_incomplete_arguments_and_disconnected_subscriber_never_execute) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.Prepare(Request(), fixture.Config(), Access(), 42)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && run.Append(Call(), "null", &wire));
  Q4T_CHECK(run.RunWebSearches(0, {}).code == "web_search_incomplete" &&
            run.Output().Snapshot().GetArray("output")->array.empty());
  Q4T_CHECK(
      run.RunWebSearches(1, [](const auto&) { return false; }).http_status ==
      499);
  Q4T_CHECK(run.RunWebSearches(1, {}).code == "web_search_already_dispatched");
  WebIndexSnapshot snapshot;
  Q4T_CHECK(
      WebIndexStore(store)
          .Read(Binary(), WebIndexIdentity(*fixture.policy, {}), &snapshot)
          .code == "web_index_unavailable");
  return true;
}

Q4T_TEST(
    responses_web_missing_backend_is_request_local_and_plain_chat_still_works) {
  Fixture fixture;
  auto config = fixture.Config();
  config.web_policy.reset();
  ObjectStore store(fixture.objects);
  ResponsesExecution run(store);
  std::string wire;
  Q4T_CHECK(Ok(run.Prepare(Request(), config, Access(), 42)) &&
            Ok(run.Begin(&wire)) && run.Append(Call(), "null", &wire));
  Q4T_CHECK(run.RunWebSearches(1, {}).code == "web_search_not_configured" &&
            run.Output().Snapshot().GetArray("output")->array[0].GetString(
                "status") == "failed");
  ResponsesExecution plain(store);
  Q4T_CHECK(Ok(plain.Prepare(Object({{"input", String("Hello")}}), config,
                             Access(), 42)) &&
            Ok(plain.Begin(&wire)));
  Q4T_CHECK(plain.Append(Text("Hello"), "null", &wire) &&
            Ok(plain.Finish(false, 0, {2, 0, 1, 0}, 43, &wire)));
  return true;
}

Q4T_TEST(
    responses_web_interruption_preserves_completed_sources_without_replay) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  std::string id;
  {
    ResponsesExecution run(store);
    Q4T_CHECK(Search(run, fixture) && Ok(run.Checkpoint()));
    id = run.Id();
  }
  auto interrupted = ResponsesState(store).Interrupt(id, Access(), "owner_lost",
                                                     "Owned executor exited");
  Q4T_CHECK(Ok(interrupted));
  auto stored = ResponsesState(store).Retrieve(id, Access());
  Q4T_CHECK(Ok(stored) && stored.value.GetString("status") == "failed" &&
            stored.value.GetArray("output")->array[0].GetString("status") ==
                "completed");
  ResponsesExecution next(store);
  Q4T_CHECK(Ok(next.Prepare(Object({{"previous_response_id", String(id)},
                                    {"input", String("Continue")}}),
                            fixture.Config(), Access(), 44)));
  Q4T_CHECK(OutputJson(next.Generation().generation_request).find(kPassage) !=
            std::string::npos);
  return true;
}

Q4T_TEST(responses_web_policy_cli_is_optional_and_validated_transactionally) {
  ServerOptions options;
  options.model_dir = "/readonly-model";
  const std::vector<std::string_view> args{"--web-policy-file",
                                           "/private/web.json"};
  Q4T_CHECK(ParseServerOptions(args, &options) &&
            options.web_policy_file == "/private/web.json");
  Q4T_CHECK(!ParseServerOptions(
                std::vector<std::string_view>{"--web-policy-file"}, &options) &&
            options.web_policy_file == "/private/web.json");
  options.web_policy_file = std::string("bad\0path", 8);
  Q4T_CHECK(!ValidateServerOptions(options));
  Fixture fixture;
  const auto description = WebDeploymentPolicyDescription(fixture.policy.get());
  Q4T_CHECK(description.GetBool("configured") &&
            description.GetInt("seed_count") == 1 &&
            !description.GetBool("core_readiness_dependency") &&
            OutputJson(description).find(kUrl) == std::string::npos);
  Q4T_CHECK(Save("config",
                 Read(EffectiveServeConfigJson(
                     ServerOptions{}, fixture.Config(), false, true, 64)),
                 ""));
  return true;
}

Q4T_TEST(responses_web_queued_inputs_restore_without_replaying_search) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  ResponsesExecution run(store);
  Q4T_CHECK(Search(run, fixture) && Finish(run, fixture));
  ResponsesExecution next(store);
  Q4T_CHECK(Ok(next.Prepare(Object({{"previous_response_id", String(run.Id())},
                                    {"input", String("Continue")}}),
                            fixture.Config(), Access(), 44)));
  Json frozen;
  Q4T_CHECK(Ok(next.FreezeInputs(&frozen)) &&
            frozen.GetArray("web_content")->array.size() == 1);
  auto config = fixture.Config();
  config.web_policy.reset();
  ResponsesExecution restored(store);
  Q4T_CHECK(Ok(restored.RestoreInputs(frozen, config, Access(), 45)));
  Q4T_CHECK(
      OutputJson(restored.Generation().generation_request).find(kPassage) !=
          std::string::npos &&
      !restored.PendingWebSearch());
  Set(&Field(frozen, "web_content").array[0], "item_sha256",
      String(std::string(64, '0')));
  ResponsesExecution conflict(store);
  Q4T_CHECK(conflict.RestoreInputs(frozen, config, Access(), 45).code ==
            "web_context_invalid");
  return true;
}

namespace {
Json ChatRequest() {
  return Read(
      R"({"messages":[{"role":"user","content":"Find alpha"}],"web_search_options":{},"reasoning_effort":"none"})");
}
bool ChatPrepare(ChatWebSearch& web, ObjectStore& store, const Fixture& fixture,
                 const Json& request, ChatRequestPlan* plan,
                 const Json* frozen = nullptr) {
  ChatPlanError error;
  if (!PlanChatRequest(request, fixture.Config(), plan, &error)) return false;
  return Ok(
      web.Prepare(&store, request, fixture.Config(), Binary(), plan, frozen));
}
}  // namespace

Q4T_TEST(chat_web_contract_validates_nested_options_without_fetch) {
  Fixture fixture;
  auto config = fixture.Config();
  config.web_policy.reset();
  ChatRequestPlan plan;
  ChatPlanError error;
  auto request = ChatRequest();
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  Set(&Field(request, "web_search_options"), "user_location",
      Read(
          R"({"type":"approximate","approximate":{"country":"CN","city":"上海"}})"));
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  Set(&Field(request, "web_search_options"), "external_web_access",
      Boolean(false));
  Q4T_CHECK(!PlanChatRequest(request, config, &plan, &error) &&
            error.param == "web_search_options.external_web_access");
  Set(&request, "web_search_options", Json());
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  return true;
}

Q4T_TEST(chat_web_uses_real_index_and_keeps_public_input_unchanged) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  auto request = ChatRequest();
  const auto original = OutputJson(request);
  ChatWebSearch web;
  ChatRequestPlan plan;
  Q4T_CHECK(ChatPrepare(web, store, fixture, request, &plan) && web.Active());
  const auto model = OutputJson(plan.Request(request));
  Q4T_CHECK(OutputJson(request) == original &&
            model.find(kPassage) != model.npos &&
            model.find(WebCitationMarker(kUrl)) != model.npos &&
            plan.prepared_source_data);
  std::string prompt, param, code;
  std::vector<VisionItem> media;
  Q4T_CHECK(PrepareChatPrompt(plan.Request(request), plan.constraints, &prompt,
                              &media, &param, &code, nullptr, false, {}, false,
                              nullptr, {}, plan.prepared_source_data));
  Q4T_CHECK(prompt.find(kPassage) != prompt.npos &&
            prompt.find("not an instruction authority") != prompt.npos &&
            prompt.find("# Retrieved web evidence") <
                prompt.find("<|im_start|>user") &&
            prompt.find("citation_marker is literal attribution text") !=
                prompt.npos);
  std::string unprepared;
  Q4T_CHECK(PrepareChatPrompt(request, plan.constraints, &unprepared,
                              &media, &param, &code));
  Q4T_CHECK(unprepared.find("# Retrieved web evidence") == unprepared.npos);
  Q4T_CHECK(
      web.Prepare(&store, request, fixture.Config(), Binary(), &plan).code ==
      "web_prepared_input_invalid");
  return true;
}

Q4T_TEST(chat_web_frozen_inputs_bind_owner_query_and_policy) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  auto request = ChatRequest();
  ChatRequestPlan plan;
  ChatWebSearch web;
  Q4T_CHECK(ChatPrepare(web, store, fixture, request, &plan));
  const auto frozen = web.Freeze();
  const auto before = OutputJson(plan.Request(request));
  ChatWebSearch restored;
  ChatPlanError error;
  Q4T_CHECK(PlanChatRequest(request, fixture.Config(), &plan, &error));
  Q4T_CHECK(Ok(restored.Prepare(nullptr, request, fixture.Config(), Binary(),
                                &plan, &frozen)) &&
            OutputJson(plan.Request(request)) == before);
  ChatWebSearch foreign;
  auto access = Binary();
  access.scope = "client:other";
  Q4T_CHECK(
      foreign
          .Prepare(nullptr, request, fixture.Config(), access, &plan, &frozen)
          .code == "web_prepared_input_invalid");
  auto changed = request;
  Set(&Field(changed, "messages").array[0], "content", String("Changed query"));
  ChatWebSearch query;
  Q4T_CHECK(
      query
          .Prepare(nullptr, changed, fixture.Config(), Binary(), &plan, &frozen)
          .code == "web_prepared_input_invalid");
  fixture.policy->max_depth = 2;
  ChatWebSearch policy;
  Q4T_CHECK(
      policy
          .Prepare(nullptr, request, fixture.Config(), Binary(), &plan, &frozen)
          .code == "web_search_policy_changed");
  return true;
}

Q4T_TEST(chat_web_annotations_keep_actual_text_offsets_and_stream_shape) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  ChatWebSearch web;
  ChatRequestPlan plan;
  Q4T_CHECK(ChatPrepare(web, store, fixture, ChatRequest(), &plan));
  const auto marker = WebCitationMarker(kUrl);
  const auto text = "中文🙂 " + marker +
                    " unknown [source](<https://fake.invalid/>) " + marker;
  std::string message = OutputJson(Object(
                  {{"role", String("assistant")}, {"content", String(text)}})),
              delta;
  Q4T_CHECK(web.Annotate(message, &message, &delta));
  const auto result = Read(message);
  const auto* annotations = result.GetArray("annotations");
  Q4T_CHECK(
      result.GetString("content") == text && annotations &&
      annotations->array.size() == 2 &&
      annotations->array[0].Find("url_citation")->GetInt("start_index") == 4 &&
      OutputJson(*annotations) == OutputJson(*Read(delta).Find("annotations")));
  Q4T_CHECK(
      web.Annotate(
          R"({"role":"assistant","content":"[source](<https://example.com/sour"})",
          &message, &delta) &&
      delta == "{}");
  Q4T_CHECK(
      web.Annotate(R"({"role":"assistant","content":null,"refusal":"No"})",
                   &message, &delta) &&
      delta == "{}");
  return true;
}

Q4T_TEST(chat_web_backend_failure_and_cancellation_preserve_plain_chat) {
  Fixture fixture;
  auto config = fixture.Config();
  config.web_policy.reset();
  ChatRequestPlan plan;
  ChatPlanError error;
  auto request = ChatRequest();
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  ChatWebSearch web;
  Q4T_CHECK(web.Prepare(nullptr, request, config, Binary(), &plan).code ==
            "web_search_not_configured");
  ChatWebSearch cancelled;
  auto access = Binary();
  access.cancelled = [] { return true; };
  Q4T_CHECK(cancelled.Prepare(nullptr, request, fixture.Config(), access, &plan)
                .http_status == 499);
  Set(&request, "web_search_options", Json());
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  ChatWebSearch plain;
  Q4T_CHECK(Ok(plain.Prepare(nullptr, request, config, access, &plan)) &&
            !plain.Active());
  std::string output, delta;
  Q4T_CHECK(plain.Annotate("unchanged", &output, &delta) &&
            output == "unchanged" && delta == "{}");
  return true;
}

Q4T_TEST(chat_web_batch_preparation_and_count_use_same_frozen_evidence) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  BatchInputRow row;
  row.body = ChatRequest();
  row.line = 1;
  row.custom_id = "web";
  BatchPreparedInput prepared;
  Json captured;
  auto meter = [&](auto endpoint, const auto& input, const Json& frozen,
                   const auto& config, const auto& access, auto* output) {
    captured = frozen;
    BatchTokenCodec codec;
    codec.encode = [](std::string_view text, std::vector<uint32_t>* tokens) {
      tokens->assign(text.size(), 1);
      return q4t::Status{};
    };
    codec.decode = [](const std::vector<uint32_t>& ids, std::string* text) {
      text->assign(ids.size(), 'x');
      return q4t::Status{};
    };
    return MeasureBatchInput(store, endpoint, input, frozen, config, access,
                             codec, output);
  };
  Q4T_CHECK(Ok(BatchPreparation(store).Prepare(
      Binary(), "batch_web", "/v1/chat/completions", row, fixture.Config(),
      &prepared, meter)));
  Q4T_CHECK(captured.Find("web_content") &&
            OutputJson(captured).find(kPassage) != std::string::npos &&
            prepared.input_budget.tokens > 100);
  ChatRequestPlan plan;
  ChatWebSearch restore;
  Q4T_CHECK(ChatPrepare(restore, store, fixture, row.body, &plan,
                        captured.Find("web_content")));
  return true;
}

Q4T_TEST(chat_web_stored_annotations_do_not_persist_private_prompt_rewrite) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  auto request = ChatRequest();
  Set(&request, "store", Boolean(true));
  ChatWebSearch web;
  ChatRequestPlan plan;
  Q4T_CHECK(ChatPrepare(web, store, fixture, request, &plan));
  std::string message, delta;
  Q4T_CHECK(web.Annotate(
      OutputJson(Object({{"role", String("assistant")},
                         {"content", String(WebCitationMarker(kUrl))}})),
      &message, &delta));
  const auto response =
      Object({{"id", String("chatcmpl-web")},
              {"object", String("chat.completion")},
              {"created", Number(std::time(nullptr))},
              {"model", String("cpu")},
              {"choices", Array({Object({{"index", Number(0)},
                                         {"message", Read(message)},
                                         {"finish_reason", String("stop")}})})},
              {"usage", Object({{"prompt_tokens", Number(100)},
                                {"completion_tokens", Number(10)},
                                {"total_tokens", Number(110)}})}});
  auto saved = StoredChats(store).Save(request, response, Access());
  Q4T_CHECK(Ok(saved) &&
            OutputJson(saved.value).find("url_citation") != std::string::npos);
  const auto messages =
      StoredChats(store).Messages("chatcmpl-web", {}, Access());
  Q4T_CHECK(Ok(messages) &&
            OutputJson(messages.value).find(kPassage) == std::string::npos &&
            OutputJson(messages.value).find("Find alpha") != std::string::npos);
  const auto created = response.GetInt("created");
  std::string wire = detail::SseDeltaChunk(
      "chatcmpl-web", "cpu", R"({"role":"assistant"})", "", 0, created, true);
  wire += detail::SseDeltaChunk(
      "chatcmpl-web", "cpu",
      OutputJson(Object({{"content", String(WebCitationMarker(kUrl))}})), "", 0,
      created, true);
  wire +=
      detail::SseDeltaChunk("chatcmpl-web", "cpu", delta, "", 0, created, true);
  wire += detail::SseDeltaChunk("chatcmpl-web", "cpu", "{}", "stop", 0, created,
                                true);
  wire += detail::SseUsageChunk("chatcmpl-web", "cpu", created,
                                OutputJson(*response.Find("usage")));
  wire += "data: [DONE]\r\n\r\n";
  Q4T_CHECK(Save("chat", response, wire));
  return true;
}

Q4T_TEST(chat_web_prompt_alias_and_text_parts_preserve_query_and_context) {
  Fixture fixture;
  ObjectStore store(fixture.objects);
  Q4T_CHECK(fixture.Seed(store));
  const std::vector<Json> requests{
      Read(R"({"prompt":"Find alpha","web_search_options":{}})"),
      Read(
          R"({"messages":[{"role":"user","content":[{"type":"text","text":"Find"},{"type":"text","text":"alpha"}]}],"web_search_options":{}})")};
  for (const auto& request : requests) {
    ChatWebSearch web;
    ChatRequestPlan plan;
    Q4T_CHECK(ChatPrepare(web, store, fixture, request, &plan));
    Q4T_CHECK(OutputJson(plan.Request(request)).find(kPassage) !=
              std::string::npos);
    if (request.Has("prompt")) {
      Q4T_CHECK(
          !plan.Request(request).Has("messages") &&
          plan.Request(request).GetString("prompt").starts_with("Find alpha"));
      continue;
    }
    const auto* parts = plan.Request(request)
                            .GetArray("messages")
                            ->array.back()
                            .GetArray("content");
    Q4T_CHECK(parts && parts->array.size() >= 2 &&
              parts->array[0].GetString("text").starts_with("Find"));
  }
  return true;
}
