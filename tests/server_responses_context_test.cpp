#include "q4t/server/responses_plan.h"

#include "q4t/server/chat_prompt.h"
#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
Json Read(const std::string& text) {
  Json result;
  if (!q4t::io::ParseJson(text, &result, true))
    throw std::runtime_error("context fixture JSON");
  return result;
}
struct Fixture {
  ChatPlanConfig config;
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan plan;
  ChatPlanError error;
  Fixture() {
    config.model = "context-fixture";
    config.max_len = 1024;
    config.structured = true;
  }
  bool Prepare(const std::string& input, const std::string& extra = "") {
    const auto request = Read(R"({"reasoning":{"effort":"none"},"store":false,)"
                              R"("truncation":"auto","input":)" +
                              input + extra + '}');
    return ParseResponsesEnvelope(request, config, &envelope, &error) &&
           PrepareResponsesGeneration(envelope, *request.Find("input"), config,
                                      &plan, &error);
  }
  q4t::Status Measure(const ResponsesGenerationPlan& candidate,
                      ChatPlanError* failure) {
    const auto& request = candidate.chat.Request(candidate.generation_request);
    std::string prompt;
    std::vector<VisionItem> media;
    auto status =
        PrepareChatPrompt(request, candidate.chat.constraints, &prompt, &media,
                          &failure->param, &failure->code);
    if (!status) return status;
    std::vector<uint32_t> tokens;
    ChatPromptBudget budget;
    // A deterministic byte-token fixture measures the real rendered template;
    // actual Qwen tokenization is covered by the later HTTP short validation.
    return EncodeChatPrompt(
        request, config, prompt,
        [](std::string_view text, std::vector<uint32_t>* out) {
          out->assign(text.begin(), text.end());
          return q4t::Status();
        },
        &tokens, &budget, failure);
  }
  bool Fit() {
    return FitResponsesContext(
               envelope, config,
               [this](const auto& candidate, auto* failure) {
                 return Measure(candidate, failure);
               },
               &plan, &error)
        .ok();
  }
};
std::string Message(const std::string& role, const std::string& text) {
  return "{\"role\":\"" + role + "\",\"content\":\"" + text + "\"}";
}
}  // namespace

Q4T_TEST(responses_context_preserves_fitting_input_and_disabled_contract) {
  Fixture f;
  Q4T_CHECK(f.Prepare(R"("short input")"));
  const auto original = OutputJson(f.plan.generation_request);
  Q4T_CHECK(f.Fit() && f.plan.truncated_items == 0);
  Q4T_CHECK(OutputJson(f.plan.generation_request) == original);
  f.envelope.truncate = false;
  bool measured = false;
  Q4T_CHECK(FitResponsesContext(
      f.envelope, f.config,
      [&](const auto&, auto*) {
        measured = true;
        return q4t::Status::Fail("disabled must not trim");
      },
      &f.plan, &f.error));
  Q4T_CHECK(!measured);
  return true;
}

Q4T_TEST(
    responses_context_trims_old_prefix_keeps_instructions_and_active_turn) {
  Fixture f;
  const auto input = '[' + Message("user", std::string(2000, 'a')) + ',' +
                     Message("assistant", std::string(2000, 'b')) + ',' +
                     Message("user", "current question") + ']';
  Q4T_CHECK(f.Prepare(input, R"(,"instructions":"Keep this instruction.")"));
  const auto original = OutputJson(f.envelope.request);
  Q4T_CHECK(f.Fit() && f.plan.truncated_items == 2);
  Q4T_CHECK(f.plan.input.items.array.size() == 1);
  Q4T_CHECK(f.plan.input.items.array[0].GetString("content") ==
            "current question");
  Q4T_CHECK(f.plan.input.messages.array[0].GetString("content") ==
            "Keep this instruction.");
  Q4T_CHECK(f.plan.input.message_paths.back() == "input[2]");
  Q4T_CHECK(OutputJson(f.envelope.request) == original);
  return true;
}

Q4T_TEST(responses_context_never_drops_oversized_active_user_request) {
  Fixture f;
  Q4T_CHECK(f.Prepare('[' + Message("user", std::string(2000, 'x')) + ']'));
  const auto before = OutputJson(f.plan.input.items);
  Q4T_CHECK(!f.Fit() && f.error.code == "context_length_exceeded");
  Q4T_CHECK(OutputJson(f.plan.input.items) == before &&
            !f.plan.truncated_items);
  return true;
}

Q4T_TEST(responses_context_parallel_calls_and_outputs_are_not_split) {
  Fixture f;
  const auto input =
      '[' + Message("assistant", std::string(2000, 'x')) +
      R"(,{"type":"function_call","call_id":"a","name":"read","arguments":"{}"},)"
      R"({"type":"custom_tool_call","call_id":"b","name":"query","input":"opaque"},)"
      R"({"type":"custom_tool_call_output","call_id":"b","output":"second"},)"
      R"({"type":"function_call_output","call_id":"a","output":"first"}])";
  Q4T_CHECK(f.Prepare(input));
  std::vector<size_t> measured;
  const auto status = FitResponsesContext(
      f.envelope, f.config,
      [&](const auto& candidate, auto* error) {
        measured.push_back(candidate.input.items.array.size());
        return f.Measure(candidate, error);
      },
      &f.plan, &f.error);
  Q4T_CHECK(status && f.plan.truncated_items == 1);
  Q4T_CHECK(measured == std::vector<size_t>({5, 4}));
  f.config.max_len = 32;
  Q4T_CHECK(!f.Fit() && f.plan.input.items.array.size() == 4);
  return true;
}

Q4T_TEST(responses_context_reasoning_remains_attached_to_following_item) {
  Fixture f;
  const auto input =
      '[' + Message("assistant", std::string(2000, 'x')) +
      R"(,{"type":"reasoning","id":"rs_context","summary":[],)"
      R"("content":[{"type":"reasoning_text","text":"prior analysis"}]},)" +
      Message("assistant", "answer") + ']';
  Q4T_CHECK(f.Prepare(input));
  Q4T_CHECK(f.Fit() && f.plan.truncated_items == 1);
  Q4T_CHECK(f.plan.input.items.array.size() == 2);
  Q4T_CHECK(f.plan.input.items.array[0].GetString("type") == "reasoning");
  return true;
}

Q4T_TEST(responses_context_does_not_hide_invalid_tool_history_or_quota) {
  Fixture f;
  Q4T_CHECK(!f.Prepare(
      '[' + Message("assistant", std::string(2000, 'x')) +
      R"(,{"type":"function_call_output","call_id":"missing","output":"x"},)" +
      Message("user", "question") + ']'));
  Q4T_CHECK(f.Prepare('[' + Message("user", "question") + ']'));
  ClientLimits limits;
  limits.max_prompt_tokens = 1;
  f.config.client_limits = &limits;
  Q4T_CHECK(!f.Fit() && f.error.code == "client_prompt_limit_exceeded");
  Q4T_CHECK(f.plan.truncated_items == 0);
  return true;
}

Q4T_TEST(responses_context_callback_error_does_not_partially_replace_plan) {
  Fixture f;
  Q4T_CHECK(f.Prepare('[' + Message("user", std::string(2000, 'x')) + ',' +
                      Message("user", "current") + ']'));
  const auto before = OutputJson(f.plan.input.items);
  int calls = 0;
  Q4T_CHECK(!FitResponsesContext(
      f.envelope, f.config,
      [&](const auto& candidate, auto* error) {
        if (++calls == 1) return f.Measure(candidate, error);
        *error = {"request_cancelled", "input", 499};
        return q4t::Status::Fail("cancelled");
      },
      &f.plan, &f.error));
  Q4T_CHECK(calls == 2 && f.error.http_status == 499);
  Q4T_CHECK(OutputJson(f.plan.input.items) == before);
  return true;
}
