#include "q4t/server/responses_execution.h"

#include <cstdlib>
#include <algorithm>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/batch_budget.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/chat_tool_history.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/responses_reasoning.h"
#include "q4t/server/tool_result_contracts.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Read(const std::string& text) {
  Json value;
  const auto status = q4t::io::ParseJson(text, &value, true);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s %s: %s\n", result.code.c_str(),
                 result.value.GetString("param").c_str(),
                 result.message.c_str());
  return result.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
    if (!root) throw std::runtime_error("Missing async fixture output");
    auto directory = (std::filesystem::path(root) / "async-XXXXXX").string();
    if (!mkdtemp(directory.data())) throw std::runtime_error("Async directory");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
  }
};
StateAccess Access() { return {"client:async", Clock::now() + 5s, {}}; }
ChatPlanConfig Config() {
  ChatPlanConfig result;
  result.model = "cpu";
  result.structured = true;
  return result;
}
Json Request(bool custom = false) {
  auto request = Read(R"({"input":"Read project records and explain LLM",
    "reasoning":{"effort":"none"},"max_output_tokens":64,
    "tools":[{"type":"namespace","name":"project","description":"Records",
      "tools":[{"type":"function","name":"lookup","async":true}]}]})");
  if (custom)
    Set(&Field(Field(request, "tools").array[0], "tools").array[0], "type",
        String("custom"));
  return request;
}
Json Call(bool custom = false) {
  auto call = Read(R"({"type":"function_call","id":"fc_old","call_id":"late",
    "name":"lookup","namespace":"project","arguments":"{}","async":true,
    "status":"completed","caller":{"type":"direct"}})");
  if (custom) {
    Set(&call, "type", String("custom_tool_call"));
    std::erase_if(call.object,
                  [](const auto& field) { return field.first == "arguments"; });
    Set(&call, "input", String("read docs/MODEL.md"));
  }
  return call;
}
Json Result(bool custom = false) {
  return Object({{"type", String(custom ? "custom_tool_call_output"
                                        : "function_call_output")},
                 {"call_id", String("late")},
                 {"output", String("verified project record")}});
}
ResponseDelta Delta(bool custom = false, size_t index = 0) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.tool_start = true;
  delta.call = {index, "late",
                ResponsesToolModelName("project", "lookup",
                                       custom ? "custom" : "function"),
                custom ? "read docs/MODEL.md" : "{}", custom};
  return delta;
}
ResponseDelta Text() {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "LLM means large language model.";
  return delta;
}
q4t::Status Measure(const ResponsesGenerationPlan& plan, ChatPlanError* error) {
  std::string prompt;
  std::vector<VisionItem> media;
  return PrepareChatPrompt(
      plan.chat.Request(plan.generation_request), plan.chat.constraints,
      &prompt, &media, &error->param, &error->code, nullptr, false,
      plan.chat.async_tool_call_ids, plan.chat.responses_tool_media);
}
size_t Occurrences(const std::string& text, const std::string& word) {
  size_t count = 0, position = 0;
  while ((position = text.find(word, position)) != text.npos) {
    ++count;
    position += word.size();
  }
  return count;
}
}  // namespace

Q4T_TEST(responses_async_definitions_bind_function_custom_and_direct_caller) {
  for (bool custom : {false, true}) {
    auto request = Request(custom);
    Json tools, choice;
    ChatPlanError error;
    std::vector<ResponsesToolIdentity> identities;
    Q4T_CHECK(NormalizeResponsesTools(request, &tools, &choice, &error));
    Q4T_CHECK(PlanResponsesToolIdentities(request, &identities, &error));
    Q4T_CHECK(identities.size() == 1 && identities[0].async);
    auto& definition =
        Field(Field(request, "tools").array[0], "tools").array[0];
    Set(&definition, "async", String("true"));
    Q4T_CHECK(!NormalizeResponsesTools(request, &tools, &choice, &error));
    Q4T_CHECK(error.http_status == 400 &&
              error.param == "tools[0].tools[0].async");
    Set(&definition, "async", Boolean(false));
    Set(&definition, "allowed_callers", Array({String("direct")}));
    Q4T_CHECK(NormalizeResponsesTools(request, &tools, &choice, &error));
    Q4T_CHECK(PlanResponsesToolIdentities(request, &identities, &error));
    Q4T_CHECK(!identities[0].async);
  }
  return true;
}

Q4T_TEST(responses_async_late_results_preserve_chronology_and_chat_boundary) {
  for (bool custom : {false, true}) {
    auto request = Object(
        {{"input",
          Array({Call(custom),
                 Read(R"({"role":"user","content":"unrelated question"})"),
                 Read(R"({"role":"assistant","content":"independent answer"})"),
                 Result(custom)})}});
    ResponsesInputPlan plan;
    ChatPlanError error;
    Q4T_CHECK(NormalizeResponsesInput(request, &plan, &error));
    Q4T_CHECK(plan.async_tool_call_ids == std::vector<std::string>{"late"});
    Q4T_CHECK(plan.messages.array.size() == 4);
    Q4T_CHECK(plan.messages.array[3].GetString("role") == "tool");
    auto chat = Object(
        {{"messages", plan.messages}, {"enable_thinking", Boolean(false)}});
    Q4T_CHECK(!ValidateChatContract(chat, "cpu", false));
    std::vector<size_t> order;
    Q4T_CHECK(
        PlanToolResultOrder(plan.messages, &order, plan.async_tool_call_ids));
    Q4T_CHECK(order == (std::vector<size_t>{0, 1, 2, 3}));
    auto config = Config();
    config.async_tool_call_ids = plan.async_tool_call_ids;
    ChatRequestPlan ready;
    Q4T_CHECK(PlanChatRequest(chat, config, &ready, &error));
    std::string prompt;
    std::vector<VisionItem> media;
    Q4T_CHECK(PrepareChatPrompt(chat, ready.constraints, &prompt, &media,
                                &error.param, &error.code, nullptr, false,
                                ready.async_tool_call_ids));
    Q4T_CHECK(prompt.find("[Tool call_id: \"late\"; asynchronous]") <
              prompt.find("unrelated question"));
    Q4T_CHECK(prompt.find("independent answer") <
              prompt.find("[Tool result call_id: \"late\"]"));
    Field(request, "input").array.pop_back();
    Q4T_CHECK(NormalizeResponsesInput(request, &plan, &error));
    Q4T_CHECK(plan.messages.array.size() == 3);
  }
  return true;
}

Q4T_TEST(responses_async_mixed_batch_keeps_synchronous_result_requirement) {
  auto sync = Call();
  Set(&sync, "id", String("fc_sync"));
  Set(&sync, "call_id", String("sync"));
  Set(&sync, "async", Boolean(false));
  auto result = Result();
  Set(&result, "call_id", String("sync"));
  auto request = Object(
      {{"input", Array({Call(), sync,
                        Read(R"({"role":"user","content":"continue"})")})}});
  ResponsesInputPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!NormalizeResponsesInput(request, &plan, &error));
  Field(request, "input")
      .array.insert(Field(request, "input").array.begin() + 2, result);
  Q4T_CHECK(NormalizeResponsesInput(request, &plan, &error));
  Q4T_CHECK(
      PlanToolResultOrder(plan.messages, nullptr, plan.async_tool_call_ids));
  auto& messages = plan.messages.array;
  messages.erase(messages.begin() + 1);
  Q4T_CHECK(
      !PlanToolResultOrder(plan.messages, nullptr, plan.async_tool_call_ids));
  return true;
}

Q4T_TEST(responses_async_rejects_late_orphan_duplicate_type_and_caller) {
  const auto base = Object(
      {{"input", Array({Call(), Read(R"({"role":"user","content":"continue"})"),
                        Result()})}});
  for (const auto* change :
       {R"({"call_id":"orphan"})", R"({"type":"custom_tool_call_output"})",
        R"({"name":"wrong"})", R"({"namespace":"wrong"})",
        R"({"caller":{"type":"direct","caller_id":"p"}})"}) {
    auto request = base;
    for (const auto& [key, value] : Read(change).object)
      Set(&Field(request, "input").array.back(), key, value);
    ResponsesInputPlan plan;
    ChatPlanError error;
    Q4T_CHECK(!NormalizeResponsesInput(request, &plan, &error));
    Q4T_CHECK(error.http_status == 400);
  }
  auto duplicate = base;
  Field(duplicate, "input").array.push_back(Result());
  ResponsesInputPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!NormalizeResponsesInput(duplicate, &plan, &error));
  Q4T_CHECK(error.param == "input[3].call_id");
  return true;
}

Q4T_TEST(responses_async_done_precedes_independent_text_and_only_once) {
  for (bool custom : {false, true}) {
    ResponsesOutput output;
    auto request = Request(custom);
    ResponsesEnvelope envelope;
    ChatPlanError error;
    Q4T_CHECK(ParseResponsesEnvelope(request, Config(), &envelope, &error));
    ResponsesGenerationPlan generation;
    Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"),
                                         Config(), &generation, &error));
    Set(&envelope.options, "tools", generation.effective_tools);
    Set(&envelope.options, "tool_choice", String("auto"));
    Set(&envelope.options, "service_tier", String("default"));
    Q4T_CHECK(output.Init(envelope.options, "resp_async", 42, false));
    std::string wire, all;
    Q4T_CHECK(output.Start(&wire));
    all += wire;
    Q4T_CHECK(output.Append(Delta(custom), "null", &wire));
    all += wire;
    bool answer = false;
    Q4T_CHECK(output.CompleteAsyncCalls(1, &answer, &wire) && answer);
    all += wire;
    Q4T_CHECK(wire.find("event: response.output_item.done") != wire.npos);
    auto fragment = Delta(custom);
    fragment.tool_start = false;
    Q4T_CHECK(!output.Append(fragment, "null", &wire));
    Q4T_CHECK(output.Append(Text(), "null", &wire));
    all += wire;
    Q4T_CHECK(output.Finish(false, 0, {20, 0, 10, 0}, 43, &wire));
    all += wire;
    Q4T_CHECK(Occurrences(all, "event: response.output_item.done") == 2);
    Q4T_CHECK(all.find("event: response.output_item.done") <
              all.find("event: response.output_text.delta"));
    const auto& call = output.Snapshot().GetArray("output")->array[0];
    Q4T_CHECK(call.GetBool("async") &&
              call.Find("caller")->GetString("type") == "direct");
    Q4T_CHECK(ValidateResponsesItemShape(ResponsesPublicValue(call),
                                         ResponsesItemShape::kOutput, &error));
    Q4T_CHECK(output.Snapshot().Find("usage")->GetInt("total_tokens") == 30);
    const auto directory =
        std::filesystem::path(std::getenv("Q4T_TEST_OUTPUT_DIR")) / "responses";
    std::filesystem::create_directories(directory);
    const auto name = custom ? "async-custom" : "async-function";
    std::ofstream(directory / (std::string(name) + ".json")) << output.Json();
    std::ofstream(directory / (std::string(name) + ".wire")) << all;
  }
  return true;
}

Q4T_TEST(responses_async_partial_and_mixed_calls_do_not_continue) {
  for (bool mixed : {false, true}) {
    auto request = Request();
    if (mixed)
      Field(request, "tools")
          .array.push_back(Read(R"({"type":"function","name":"sync"})"));
    ResponsesOutput output;
    Q4T_CHECK(output.Init(request, "resp_boundary", 42, false));
    std::string wire;
    Q4T_CHECK(output.Start(&wire));
    Q4T_CHECK(output.Append(Delta(), "null", &wire));
    if (mixed) {
      auto sync = Delta(false, 1);
      sync.call.name = "sync";
      sync.call.id = "sync";
      Q4T_CHECK(output.Append(sync, "null", &wire));
    }
    bool answer = true;
    const auto status =
        output.CompleteAsyncCalls(mixed ? 2 : 0, &answer, &wire);
    Q4T_CHECK(status.ok() == mixed && !answer && wire.empty());
    Q4T_CHECK(output.Finish(!mixed, mixed ? 2 : 0, {10, 0, 5, 0}, 43, &wire));
    if (!mixed)
      Q4T_CHECK(wire.find("event: response.function_call_arguments.done") ==
                wire.npos);
  }
  return true;
}

Q4T_TEST(responses_async_history_definitions_survive_none_without_dispatch) {
  for (bool custom : {false, true}) {
    auto request = Request(custom);
    auto& tools = Field(Field(request, "tools").array[0], "tools").array;
    Set(&tools[0], "description", String("Read approved project documents"));
    auto unused = tools[0];
    Set(&unused, "name", String("unused_reader"));
    Set(&unused, "description", String("UNUSED_DEFINITION"));
    tools.push_back(std::move(unused));
    Set(&request, "tool_choice", String("none"));
    Set(&request, "input",
        Array(
            {Call(custom),
             Read(
                 R"({"role":"user","content":"Explain LLM while pending"})")}));
    ResponsesEnvelope envelope;
    ResponsesGenerationPlan plan;
    ChatPlanError error;
    Q4T_CHECK(ParseResponsesEnvelope(request, Config(), &envelope, &error));
    Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"),
                                         Config(), &plan, &error));
    const auto& chat = plan.chat.Request(plan.generation_request);
    Q4T_CHECK(ConstraintTemplateRequest(chat, plan.chat.constraints)
                  .GetArray("tools")
                  ->array.empty());
    Q4T_CHECK(plan.chat.constraints.choice ==
              OutputConstraints::ToolChoice::kNone);
    std::string prompt;
    std::vector<VisionItem> media;
    ChatPromptLayout layout;
    Q4T_CHECK(PrepareChatPrompt(chat, plan.chat.constraints, &prompt, &media,
                                &error.param, &error.code, &layout, false,
                                plan.chat.async_tool_call_ids));
    Q4T_CHECK(prompt.find("Read approved project documents") != prompt.npos);
    Q4T_CHECK(prompt.find("Historical tool definitions") != prompt.npos);
    Q4T_CHECK(prompt.find("UNUSED_DEFINITION") == prompt.npos);
    Q4T_CHECK(prompt.find("# Tools\n") == prompt.npos);
    Q4T_CHECK(prompt.find("Do not add text after a tool call") == prompt.npos);
    Q4T_CHECK(layout.messages.size() ==
              chat.GetArray("messages")->array.size());
    Q4T_CHECK(prompt.find("read docs/MODEL.md") != prompt.npos || !custom);
    Set(&request, "input",
        Array(
            {Call(custom), Result(custom),
             Read(
                 R"({"role":"user","content":"Now answer from the result"})")}));
    Q4T_CHECK(ParseResponsesEnvelope(request, Config(), &envelope, &error));
    Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"),
                                         Config(), &plan, &error));
    Q4T_CHECK(PrepareChatPrompt(plan.generation_request, plan.chat.constraints,
                                &prompt, &media, &error.param, &error.code,
                                nullptr, false, plan.chat.async_tool_call_ids));
    Q4T_CHECK(prompt.find("# Pending asynchronous calls") == prompt.npos);
  }
  return true;
}

Q4T_TEST(responses_async_execution_restart_queue_late_result_and_isolation) {
  for (bool custom : {false, true}) {
    Fixture fixture;
    std::string id, conversation;
    {
      ObjectStore store(fixture.options);
      ResponsesExecution run(store);
      auto request = Request(custom);
      if (custom) {
        const auto created =
            ConversationState(store).Create({}, Array(), Access());
        Q4T_CHECK(Ok(created));
        conversation = created.value.GetString("id");
        Set(&request, "conversation", String(conversation));
      }
      Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
      Q4T_CHECK(run.HasAsyncTools() && run.VariableInputSteps());
      std::string wire;
      Q4T_CHECK(Ok(run.Begin(&wire)));
      Q4T_CHECK(run.Append(Delta(custom), "null", &wire));
      ResponsesGenerationPlan next;
      bool answer = false;
      Q4T_CHECK(Ok(run.ContinueAsyncTools(1, 9, Config(), Measure, &next,
                                          &answer, &wire)) &&
                answer);
      Q4T_CHECK(next.generation_request.GetInt("max_completion_tokens") == 55);
      Q4T_CHECK(next.generation_request.GetString("tool_choice") == "none");
      Q4T_CHECK(next.chat.async_tool_call_ids ==
                std::vector<std::string>{"late"});
      Q4T_CHECK(next.input.items.array.size() == 2);
      Q4T_CHECK(Ok(run.ContinueAsyncTools(0, 10, Config(), Measure, &next,
                                          &answer, &wire)) &&
                !answer);
      Q4T_CHECK(run.Append(Text(), "null", &wire));
      Q4T_CHECK(Ok(run.Finish(false, 0, {20, 0, 15, 0}, 43, &wire)));
      id = run.Id();
    }
    ObjectStore store(fixture.options);
    auto follow = Request(custom);
    Set(&follow, custom ? "conversation" : "previous_response_id",
        String(custom ? conversation : id));
    Set(&follow, "background", Boolean(true));
    Set(&follow, "tools", Array());
    Set(&follow, "input", String("another independent question"));
    ResponsesExecution queue(store);
    Q4T_CHECK(Ok(queue.Prepare(follow, Config(), Access(), 44, true)));
    std::string wire;
    Q4T_CHECK(Ok(queue.Begin(&wire)));
    ResponsesExecution restored(store);
    Q4T_CHECK(Ok(restored.RestoreQueued(queue.Id(), Config(), Access())));
    Q4T_CHECK(restored.Generation().chat.async_tool_call_ids ==
              std::vector<std::string>{"late"});
    Q4T_CHECK(Ok(restored.StartRunning(&wire)));
    Q4T_CHECK(restored.Append(Text(), "null", &wire));
    Q4T_CHECK(Ok(restored.Finish(false, 0, {10, 0, 5, 0}, 45, &wire)));
    if (!custom) Set(&follow, "previous_response_id", String(restored.Id()));
    Set(&follow, "background", Boolean(false));
    Set(&follow, "input", Array({Result(custom)}));
    ResponsesExecution result(store);
    Q4T_CHECK(Ok(result.Prepare(follow, Config(), Access(), 46)));
    Q4T_CHECK(Ok(result.Begin(&wire)));
    Q4T_CHECK(result.Append(Text(), "null", &wire));
    Q4T_CHECK(Ok(result.Finish(false, 0, {10, 0, 5, 0}, 47, &wire)));
    if (!custom) Set(&follow, "previous_response_id", String(result.Id()));
    ResponsesExecution duplicate(store);
    Q4T_CHECK(duplicate.Prepare(follow, Config(), Access(), 48).http_status ==
              400);
    auto other = Access();
    other.scope = "client:other";
    ResponsesExecution isolated(store);
    Q4T_CHECK(isolated.Prepare(follow, Config(), other, 48).http_status == 404);
  }
  return true;
}

Q4T_TEST(responses_async_schema_binding_rejects_execution_policy_rewrite) {
  auto request = Request();
  auto& definition = Field(Field(request, "tools").array[0], "tools").array[0];
  Set(&definition, "output_schema", Read(R"({"type":"integer"})"));
  auto items = Array({Call()});
  Json contracts;
  Q4T_CHECK(Ok(BindToolResultContracts(request, items, &contracts)));
  Q4T_CHECK(contracts.array[0].GetBool("async"));
  Set(&definition, "async", Boolean(false));
  Set(&definition, "output_schema", Read(R"({"type":"string"})"));
  Q4T_CHECK(Ok(BindToolResultContracts(request, items, &contracts)));
  Q4T_CHECK(contracts.array[0].Find("schema")->GetString("type") == "integer");
  Set(&items.array[0], "async", Boolean(false));
  Json merged;
  Q4T_CHECK(!MergeToolResultContracts(contracts, items, &merged).ok());
  return true;
}

Q4T_TEST(responses_async_truncation_and_compaction_keep_unreturned_call) {
  auto request = Request();
  Set(&request, "truncation", String("auto"));
  Set(&request, "input",
      Array({Read(R"({"role":"user","content":"old turn"})"), Call(),
             Read(R"({"role":"user","content":"current turn"})")}));
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan plan;
  ChatPlanError error;
  Q4T_CHECK(ParseResponsesEnvelope(request, Config(), &envelope, &error));
  Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"),
                                       Config(), &plan, &error));
  size_t measured = 0;
  Q4T_CHECK(!FitResponsesContext(
      envelope, Config(),
      [&](const auto& candidate, auto* failure) {
        ++measured;
        if (candidate.input.items.array.size() < 2) return q4t::Status();
        *failure = {"context_length_exceeded", "input", 400};
        return q4t::Status::Fail("fixture context exhausted");
      },
      &plan, &error));
  Q4T_CHECK(measured == 2 && plan.truncated_items == 0);
  Fixture fixture;
  ObjectStore store(fixture.options);
  const auto history =
      ResponsesState(store).ResolveItems(*request.Find("input"), Access());
  Q4T_CHECK(Ok(history));
  ResponsesCompactionPlan compact;
  Q4T_CHECK(PlanResponsesCompaction(history.value,
                                    ResponsesCompactionMode::kAutomatic, 32,
                                    Config(), &compact, &error));
  bool retained = false;
  for (const auto& item : compact.retained.array)
    if (item.GetString("call_id") == "late") retained = item.GetBool("async");
  Q4T_CHECK(retained);
  return true;
}

Q4T_TEST(responses_async_rate_reserves_one_extra_step_and_cancel_keeps_call) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  auto request = Request();
  Set(&request, "background", Boolean(true));
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42, true)));
  BatchTokenCodec codec;
  codec.encode = [](std::string_view text, std::vector<uint32_t>* tokens) {
    tokens->assign(text.begin(), text.end());
    return q4t::Status();
  };
  codec.decode = [](const std::vector<uint32_t>& tokens, std::string* text) {
    text->assign(tokens.begin(), tokens.end());
    return q4t::Status();
  };
  BatchRateBudget budget;
  const auto access = Access();
  Q4T_CHECK(Ok(MeasureBatchResponseRate(
      run, Config(), {access.scope, access.deadline, {}}, codec, &budget)));
  Q4T_CHECK(!budget.input_exact && budget.prompt_capacity == 7 * 8192);
  Q4T_CHECK(budget.output == 64);
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(Ok(run.StartRunning(&wire)));
  Q4T_CHECK(run.Append(Delta(), "null", &wire));
  ResponsesGenerationPlan next;
  bool answer = false;
  Q4T_CHECK(Ok(run.ContinueAsyncTools(1, 10, Config(), Measure, &next, &answer,
                                      &wire)) &&
            answer);
  Q4T_CHECK(Ok(run.Cancel()));
  const auto saved = ResponsesState(store).Retrieve(run.Id(), Access());
  Q4T_CHECK(Ok(saved) && saved.value.GetString("status") == "cancelled");
  Q4T_CHECK(saved.value.GetArray("output")->array[0].GetBool("async"));
  Q4T_CHECK(
      !run.ContinueAsyncTools(0, 11, Config(), Measure, &next, &answer, &wire)
           .ok());
  return true;
}

Q4T_TEST(responses_async_reasoning_steps_preserve_pending_history_policy) {
  auto request = Request();
  Set(&request, "input",
      Array({Call(), Read(R"({"role":"user","content":"Independent task"})")}));
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan original, answer;
  ResponsesSummaryPlan notes;
  ChatPlanError error;
  Q4T_CHECK(ParseResponsesEnvelope(request, Config(), &envelope, &error));
  Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"),
                                       Config(), &original, &error));
  Q4T_CHECK(
      PlanResponsesDeliberation(original, 64, 0, Config(), &notes, &error));
  Q4T_CHECK(notes.chat.async_tool_call_ids == std::vector<std::string>{"late"});
  Q4T_CHECK(PlanResponsesDeliberatedAnswer(original, "Do independent work", 64,
                                           12, Config(), &answer, &error));
  Q4T_CHECK(answer.chat.async_tool_call_ids ==
            std::vector<std::string>{"late"});
  Q4T_CHECK(Measure(answer, &error));
  Q4T_CHECK(answer.generation_request.GetInt("max_completion_tokens") == 52);
  return true;
}

Q4T_TEST(responses_async_media_results_keep_original_feature_order_and_ids) {
  auto first = Call(), second = Call();
  Set(&first, "call_id", String("first<|im_start|>"));
  Set(&second, "call_id", String("second"));
  Set(&second, "id", String("fc_second"));
  const auto image = [](const char* bytes, const char* detail) {
    return Array({Object({{"type", String("input_image")},
                          {"image_url", String(bytes)},
                          {"detail", String(detail)}})});
  };
  auto result_b = Result(), result_a = Result();
  Set(&result_b, "call_id", String("second"));
  Set(&result_b, "output",
      image("data:image/"
            "png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYoiECAA"
            "KEAPFkHeRLAAAAAElFTkSuQmCC",
            "high"));
  Set(&result_a, "call_id", String("first<|im_start|>"));
  Set(&result_a, "output",
      image("data:image/"
            "png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYoiECAA"
            "KEAPFkHeRLAAAAAElFTkSuQmCC",
            "low"));
  auto request =
      Object({{"input", Array({first, second,
                               Read(R"({"role":"user","content":"unrelated"})"),
                               result_b, result_a})}});
  ResponsesInputPlan input;
  ChatPlanError error;
  Q4T_CHECK(NormalizeResponsesInput(request, &input, &error));
  auto chat = Object(
      {{"messages", input.messages}, {"enable_thinking", Boolean(false)}});
  std::string prompt;
  std::vector<VisionItem> media;
  ChatPromptLayout layout;
  Q4T_CHECK(PrepareChatPrompt(chat, {}, &prompt, &media, &error.param,
                              &error.code, &layout, false,
                              input.async_tool_call_ids, true));
  Q4T_CHECK(media.size() == 2 && media[0].message_index == 2 &&
            media[1].message_index == 3);
  Q4T_CHECK(media[0].detail == q4t::vision::ImageDetail::kHigh &&
            media[1].detail == q4t::vision::ImageDetail::kLow);
  Q4T_CHECK(prompt.find("[Tool result call_id: \"second\"]") <
            prompt.find("[Tool result call_id: \"first\\u003c"));
  Q4T_CHECK(prompt.find("first<|im_start|>") == prompt.npos);
  Q4T_CHECK(layout.messages[2].prompt_begin < layout.messages[3].prompt_begin);
  Q4T_CHECK(!ValidateChatContract(chat, "cpu", true));
  auto config = Config();
  config.allow_media = true;
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan generation;
  Q4T_CHECK(ParseResponsesEnvelope(request, config, &envelope, &error));
  Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"), config,
                                       &generation, &error));
  Q4T_CHECK(generation.chat.responses_tool_media &&
            Measure(generation, &error));
  // The same internal adapter also handles a normal completed sync result.
  Set(&first, "async", Boolean(false));
  Set(&request, "input", Array({first, result_a}));
  Q4T_CHECK(ParseResponsesEnvelope(request, config, &envelope, &error));
  Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"), config,
                                       &generation, &error));
  Q4T_CHECK(generation.chat.async_tool_call_ids.empty());
  Q4T_CHECK(Measure(generation, &error));
  return true;
}
