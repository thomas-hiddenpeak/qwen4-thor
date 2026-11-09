#include "q4t/server/responses_tokens.h"

#include <unistd.h>

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "q4t/server/application_routes.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/media_admission.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/responses_execution.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using namespace std::chrono_literals;
Json Read(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("input token fixture JSON");
  return value;
}
void Set(Json* object, std::string key, Json value) {
  for (auto& [name, item] : object->object)
    if (name == key) {
      item = std::move(value);
      return;
    }
  object->object.emplace_back(std::move(key), std::move(value));
}
Json String(std::string text) {
  Json value;
  value.type = Json::Type::kString;
  value.str = std::move(text);
  return value;
}
ObjectStoreOptions Options() {
  const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto root = std::filesystem::absolute(
      output ? output : ".q4t-work/serve-input-tokens-20261110/cpu");
  std::filesystem::create_directories(root);
  auto path = (root / "tokens-XXXXXX").string();
  if (!mkdtemp(path.data())) throw std::runtime_error("input token fixture");
  ObjectStoreOptions options;
  options.directory = path + "/objects";
  options.restart_backoff_ms = 0;
  return options;
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s: %s\n", result.code.c_str(),
                 result.message.c_str());
  return result.ok();
}
std::string Prompt(const ResponsesGenerationPlan& plan, bool empty = false) {
  std::string text, param, code;
  std::vector<VisionItem> media;
  const auto status = PrepareChatPrompt(
      plan.chat.Request(plan.generation_request), plan.chat.constraints, &text,
      &media, &param, &code, nullptr, empty);
  if (!status) throw std::runtime_error(status.message());
  return text;
}
struct Fixture {
  ObjectStore objects{Options()};
  ChatPlanConfig config;
  StateAccess access{
      "client:tokens", std::chrono::steady_clock::now() + 5s, {}};
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan plan;
  Fixture() {
    config.model = "cpu";
    config.model_aliases = {"alias"};
    config.count_input_only = true;
    config.structured = true;
  }
  ObjectReply Prepare(const Json& request) {
    return PrepareResponsesTokenCount(request, config, objects, access,
                                      &envelope, &plan);
  }
};
Json Generate(Fixture* fixture, Json request) {
  auto config = fixture->config;
  config.count_input_only = false;
  ResponsesExecution run(fixture->objects);
  if (!Ok(run.Prepare(request, config, fixture->access, 42)))
    throw std::runtime_error("count history prepare");
  std::string wire;
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kReasoning;
  delta.text = "A small actual fixture calculation.";
  if (!Ok(run.Begin(&wire)) || !run.Append(delta, "null", &wire))
    throw std::runtime_error("count history reasoning");
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "42";
  if (!run.Append(delta, "null", &wire) ||
      !Ok(run.Finish(false, 0, {20, 0, 10, 8}, 43, &wire)))
    throw std::runtime_error("count history finish");
  return run.Output().Snapshot();
}
}  // namespace

Q4T_TEST(responses_tokens_empty_null_and_empty_array_use_real_template) {
  Fixture f;
  std::string default_prompt;
  for (const auto* body :
       {"null", "{}", R"({"input":null,"model":null})", R"({"input":[]})"}) {
    Q4T_CHECK(Ok(f.Prepare(Read(body))));
    const auto prompt = Prompt(f.plan, true);
    if (default_prompt.empty()) default_prompt = prompt;
    Q4T_CHECK(prompt == default_prompt && !f.envelope.store);
    Q4T_CHECK(f.plan.input.items.array.empty());
  }
  Q4T_CHECK(Ok(f.Prepare(Read(R"({"reasoning":{"effort":"none"}})"))));
  Q4T_CHECK(Prompt(f.plan, true) ==
            "<|im_start|>assistant\n<think>\n\n</think>\n\n");
  // No hidden dummy user message was introduced to satisfy Chat validation.
  Q4T_CHECK(f.plan.generation_request.GetArray("messages")->array.empty());
  return true;
}

Q4T_TEST(responses_tokens_closed_contract_and_model_selection) {
  Fixture f;
  for (const auto* body :
       {"[]", "42", "false", R"({"stream":true})", R"({"store":false})",
        R"({"access_programs":{}})", R"({"model":3})", R"({"truncation":null})",
        R"({"personality":null})", R"({"personality":"future"})",
        R"({"parallel_tool_calls":1})", R"({"input":[{"type":"unknown"}]})"}) {
    const auto result = f.Prepare(Read(body));
    Q4T_CHECK(!result.ok() && result.http_status == 400);
  }
  Q4T_CHECK(f.Prepare(Read(R"({"model":"missing"})")).code ==
            "model_not_found");
  Q4T_CHECK(Ok(f.Prepare(Read(R"({"model":"alias","input":"hello"})"))));
  Q4T_CHECK(f.envelope.options.GetString("model") == "alias");
  f.config.count_input_only = false;
  Q4T_CHECK(f.Prepare(Read("{}")).code == "invalid_count_configuration");
  return true;
}

Q4T_TEST(responses_tokens_sdk_nullable_fields_default_only_on_count_endpoint) {
  Fixture f;
  Q4T_CHECK(Ok(f.Prepare(Read("{}"))));
  const auto empty = Prompt(f.plan, true);
  auto all = Read("{}");
  for (const auto* name :
       {"conversation", "input", "instructions", "model", "parallel_tool_calls",
        "previous_response_id", "reasoning", "text", "tool_choice", "tools"}) {
    auto request = Read("{}");
    Set(&request, name, Json());
    Set(&all, name, Json());
    Q4T_CHECK(Ok(f.Prepare(request)) && Prompt(f.plan, true) == empty);
  }
  Q4T_CHECK(Ok(f.Prepare(all)) && Prompt(f.plan, true) == empty);
  auto config = f.config;
  config.count_input_only = false;
  for (const auto* name : {"model", "text", "tool_choice", "tools"}) {
    auto request = Read(R"({"input":"hello"})");
    Set(&request, name, Json());
    ResponsesExecution run(f.objects);
    const auto result = run.Prepare(request, config, f.access, 42);
    Q4T_CHECK(!result.ok() && result.http_status == 400);
  }
  return true;
}

Q4T_TEST(responses_tokens_generation_template_parity_and_real_personality) {
  Fixture f;
  auto config = f.config;
  config.count_input_only = false;
  for (const auto* fields :
       {"", R"(,"personality":"friendly")",
        R"(,"personality":"pragmatic","text":{"verbosity":"low"})",
        R"(,"text":{"format":{"type":"json_schema","name":"answer",
               "strict":true,"schema":{"type":"object","properties":{
               "answer":{"type":"integer"}},"required":["answer"],
               "additionalProperties":false}}})",
        R"(,"tools":[{"type":"function","name":"lookup","parameters":{
               "type":"object","properties":{},"additionalProperties":false},
               "strict":true}],"tool_choice":{"type":"function","name":"lookup"})"}) {
    const auto request =
        Read(std::string(R"({"input":"17+25","instructions":"Use evidence.",)"
                         R"("reasoning":{"effort":"none"})") +
             fields + "}");
    Q4T_CHECK(Ok(f.Prepare(request)));
    ResponsesExecution run(f.objects);
    Q4T_CHECK(Ok(run.Prepare(request, config, f.access, 42)));
    Q4T_CHECK(run.Output().Snapshot().GetString("personality") ==
              request.GetString("personality"));
    const auto prompt = Prompt(f.plan, true);
    Q4T_CHECK(prompt == Prompt(run.Generation()));
    if (request.GetString("personality") == "friendly")
      Q4T_CHECK(prompt.find("warm, approachable") != prompt.npos);
    if (request.GetString("personality") == "pragmatic")
      Q4T_CHECK(prompt.find("practical, direct") != prompt.npos);
  }
  return true;
}

Q4T_TEST(responses_tokens_empty_permission_cannot_leak_into_chat) {
  ChatPlanConfig config;
  config.model = "cpu";
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(
      !PlanChatRequest(Read(R"({"messages":[]})"), config, &plan, &error));
  Q4T_CHECK(!PlanChatRequest(Read(R"({"messages":[],"count_input_only":true})"),
                             config, &plan, &error));
  Q4T_CHECK(error.param == "count_input_only");
  std::string prompt, param;
  std::vector<VisionItem> media;
  Q4T_CHECK(!PrepareChatPrompt(Read(R"({"messages":[]})"), {}, &prompt, &media,
                               &param));
  ResponsesInputPlan input;
  Q4T_CHECK(!NormalizeResponsesInput(Read("{}"), &input, &error));
  return true;
}

Q4T_TEST(responses_tokens_zero_output_no_slot_reservation_keeps_client_limits) {
  Fixture f;
  Q4T_CHECK(Ok(f.Prepare(Read(R"({"input":"hello"})"))));
  ClientLimits client;
  client.max_prompt_tokens = 128;
  client.max_output_tokens = 1;
  f.config.client_limits = &client;
  f.config.capacities = {2};
  f.config.max_len = 128;
  ChatPromptBudget budget;
  ChatPlanError error;
  const auto& req = f.plan.generation_request;
  Q4T_CHECK(PlanChatBudget(req, f.config, 128, &budget, &error));
  Q4T_CHECK(budget.required_capacity == 128 && budget.eligible_slots.empty() &&
            budget.requested_output == 0 && budget.total_output == 0);
  Q4T_CHECK(!PlanChatBudget(req, f.config, 129, &budget, &error));
  Q4T_CHECK(error.code == "client_prompt_limit_exceeded");
  client.max_prompt_tokens = 256;
  Q4T_CHECK(!PlanChatBudget(req, f.config, 129, &budget, &error));
  Q4T_CHECK(error.code == "context_length_exceeded");
  return true;
}

Q4T_TEST(responses_tokens_conversation_scoped_read_without_revision_mutation) {
  Fixture f;
  ConversationState state(f.objects);
  const auto items = ResponsesState(f.objects).ResolveItems(
      Read(R"([{"role":"user","content":"prior task"}])"), f.access);
  Q4T_CHECK(Ok(items));
  auto created = state.Create(Json(), items.value, f.access);
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  const auto before = state.Retrieve(id, f.access);
  auto request = Read(R"({"input":"current task"})");
  Set(&request, "conversation", String(id));
  Q4T_CHECK(Ok(f.Prepare(request)));
  Q4T_CHECK(f.plan.input.items.array.size() == 2 &&
            Prompt(f.plan).find("prior task") != std::string::npos);
  Q4T_CHECK(OutputJson(state.Retrieve(id, f.access).value) ==
            OutputJson(before.value));
  f.access.scope = "client:other";
  Q4T_CHECK(f.Prepare(request).http_status == 404);
  return true;
}

Q4T_TEST(
    responses_tokens_previous_history_replaces_instructions_without_write) {
  Fixture f;
  const auto generated = Generate(
      &f,
      Read(R"({"input":"history question","instructions":"OLD_INSTRUCTION"})"));
  const auto id = generated.GetString("id");
  ResponsesState state(f.objects);
  const auto before = state.Retrieve(id, f.access);
  auto request =
      Read(R"({"input":"next question","instructions":"NEW_INSTRUCTION"})");
  Set(&request, "previous_response_id", String(id));
  Q4T_CHECK(Ok(f.Prepare(request)));
  const auto text = Prompt(f.plan);
  Q4T_CHECK(text.find("history question") != text.npos &&
            text.find("42") != text.npos &&
            text.find("NEW_INSTRUCTION") != text.npos &&
            text.find("OLD_INSTRUCTION") == text.npos);
  Q4T_CHECK(OutputJson(state.Retrieve(id, f.access).value) ==
            OutputJson(before.value));
  f.access.scope = "client:other";
  Q4T_CHECK(f.Prepare(request).http_status == 404);
  return true;
}

Q4T_TEST(responses_tokens_encrypted_state_restored_only_for_original_client) {
  Fixture f;
  const auto generated =
      Generate(&f, Read(
                       R"({"input":"original question","store":false,
           "include":["reasoning.encrypted_content"]})"));
  auto items = *generated.GetArray("output");
  for (auto& item : items.array)
    if (item.GetString("type") == "reasoning") {
      Q4T_CHECK(!item.GetString("encrypted_content").empty());
      std::erase_if(item.object,
                    [](const auto& field) { return field.first == "content"; });
    }
  auto request = Read("{}");
  Set(&request, "input", items);
  Q4T_CHECK(Ok(f.Prepare(request)));
  const auto text = Prompt(f.plan);
  Q4T_CHECK(text.find("original question") != text.npos &&
            text.find("small actual fixture calculation") != text.npos);
  f.access.scope = "client:other";
  Q4T_CHECK(!f.Prepare(request).ok());
  return true;
}

Q4T_TEST(responses_tokens_deadline_cancellation_before_state_or_rendering) {
  Fixture f;
  f.access.deadline = std::chrono::steady_clock::now() - 1s;
  Q4T_CHECK(f.Prepare(Read("{}")).code == "request_deadline_exceeded");
  f.access.deadline = std::chrono::steady_clock::now() + 5s;
  f.access.cancelled = [] { return true; };
  Q4T_CHECK(f.Prepare(Read("{}")).code == "request_cancelled");
  Q4T_CHECK(f.plan.generation_request.IsNull());
  return true;
}

Q4T_TEST(
    responses_tokens_auto_truncation_uses_count_budget_and_keeps_last_turn) {
  Fixture f;
  f.config.max_len = 128;
  auto request = Read(R"({"reasoning":{"effort":"none"},"truncation":"auto",
    "input":[{"role":"user","content":"old"},
             {"role":"assistant","content":"old answer"},
             {"role":"user","content":"current"}]})");
  auto input = *request.Find("input");
  Set(&input.array[0], "content", String(std::string(256, 'x')));
  Set(&request, "input", std::move(input));
  Q4T_CHECK(Ok(f.Prepare(request)));
  ChatPlanError error;
  const auto original = OutputJson(f.envelope.request);
  Q4T_CHECK(FitResponsesContext(
      f.envelope, f.config,
      [&](const auto& candidate, auto* failure) {
        ChatPromptBudget budget;
        // Byte-token fixture only; actual tokenizer parity is HTTP-tested.
        return PlanChatBudget(candidate.generation_request, f.config,
                              Prompt(candidate, true).size(), &budget, failure);
      },
      &f.plan, &error));
  Q4T_CHECK(f.plan.truncated_items > 0 &&
            Prompt(f.plan).find("current") != std::string::npos);
  Q4T_CHECK(OutputJson(f.envelope.request) == original);
  return true;
}

Q4T_TEST(responses_tokens_media_counts_expanded_tokens_without_gpu_prefill) {
  Fixture f;
  f.config.allow_media = true;
  f.config.max_prefill = 1;
  std::string bytes;
  const char* code = nullptr;
  Q4T_CHECK(DecodeMediaUrl("iVBORw0KGgoAAAANSUhEUgAAARAAAAEQCAIAAAD3aLxC",
                           &bytes, &code));
  const std::vector<VisionItem> items{{VisionItem::kImage, {bytes}, 0, 0}};
  q4t::vision::ProcessorConfig image, video;
  q4t::vision::MediaDeviceBudget device{48128};
  std::vector<uint32_t> tokens{9, 1, 8};
  q4t::vision::MediaPlan media;
  ChatPromptBudget budget;
  ChatPlanError error;
  const auto request =
      Read(R"({"messages":[{"role":"user","content":"image"}]})");
  Q4T_CHECK(ExpandChatMedia(request, items, 1, 2, image, video, device,
                            f.config, &tokens, &media, &budget, &error));
  Q4T_CHECK(tokens.size() == media.expanded_prompt_tokens && tokens.size() > 3);
  Q4T_CHECK(budget.required_capacity == tokens.size() &&
            budget.total_output == 0 && budget.eligible_slots.empty());
  return true;
}

Q4T_TEST(responses_tokens_native_route_and_sdk_wire_shape) {
  constexpr auto path = "/v1/responses/input_tokens";
  Q4T_CHECK(ResponsesMethodAllowed("POST", path));
  Q4T_CHECK(!ResponsesMethodAllowed("GET", path));
  Q4T_CHECK(!ResponsesMethodAllowed("DELETE", path));
  Q4T_CHECK(!IsResponsesObservation("POST", path));
  ResponsesReadPlan plan;
  Q4T_CHECK(PlanResponsesRead("GET", path, "", &plan).http_status == 405);
  const auto value = ResponsesTokenCountJson(42);
  Q4T_CHECK(value.GetString("object") == "response.input_tokens" &&
            value.GetInt("input_tokens") == 42 && value.object.size() == 2);
  if (const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR"))
    std::ofstream(std::filesystem::path(output) / "input-tokens.json")
        << OutputJson(value) << '\n';
  return true;
}
