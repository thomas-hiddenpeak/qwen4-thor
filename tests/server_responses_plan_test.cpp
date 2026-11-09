#include "q4t/server/responses_plan.h"

#include <cmath>

#include "q4t/server/chat_template.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"
#include "q4t/server/token_logprobs.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using namespace q4t::server;
Json Read(const std::string& text) {
  Json result;
  const auto status =
      q4t::io::ParseJson(text, &result, true, kSchemaJsonLimits);
  if (!status) throw std::runtime_error(status.message());
  return result;
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "base";
  config.model_aliases = {"org/alias"};
  config.max_len = 8192;
  config.token_vocab = 512;
  config.structured = true;
  return config;
}
bool Envelope(const std::string& text, ResponsesEnvelope* result,
              ChatPlanError* error) {
  return ParseResponsesEnvelope(Read(text), Config(), result, error).ok();
}
}  // namespace

Q4T_TEST(responses_summary_plan_uses_actual_reasoning_and_shared_budget) {
  const auto snapshot = Read(R"({"output":[
    {"type":"reasoning","content":[{"type":"reasoning_text", "text":"17 + 25 = 42; checked addition."}],"summary":[]},
    {"type":"message","role":"assistant","content":[{"type":"output_text","text":"42"}]}]})");
  std::vector<std::string> prompts;
  for (const auto& mode : {"concise", "detailed", "auto"}) {
    ResponsesEnvelope envelope;
    ChatPlanError error;
    Q4T_CHECK(Envelope(
        std::string(R"({"input":"original task","reasoning":{"summary":")") +
            mode + R"("},"stream":true})",
        &envelope, &error));
    const auto original = OutputJson(envelope.request);
    ResponsesSummaryPlan summary;
    Q4T_CHECK(PlanResponsesSummary(envelope, snapshot, 256, 200, Config(),
                                   &summary, &error));
    Q4T_CHECK(summary.needed && !summary.budget_exhausted);
    Q4T_CHECK(summary.request.GetInt("max_completion_tokens") == 56);
    Q4T_CHECK(!summary.request.GetBool("enable_thinking", true));
    Q4T_CHECK(summary.chat.sampling.PlainGreedy() &&
              !summary.chat.constraints.active &&
              !summary.request.Has("tools"));
    const auto* messages = summary.request.GetArray("messages");
    Q4T_CHECK(messages && messages->array.size() == 2 &&
              messages->array[1].GetString("content") ==
                  "17 + 25 = 42; checked addition.");
    prompts.push_back(messages->array[0].GetString("content"));
    Q4T_CHECK(OutputJson(envelope.request) == original);
  }
  Q4T_CHECK(prompts[0] != prompts[1] && prompts[1] != prompts[2] &&
            prompts[0] != prompts[2]);
  return true;
}

Q4T_TEST(responses_summary_plan_no_fabricated_analysis_or_replenished_budget) {
  ResponsesEnvelope envelope;
  ChatPlanError error;
  Q4T_CHECK(Envelope(R"({"input":"question","reasoning":{"summary":"auto"}})",
                     &envelope, &error));
  ResponsesSummaryPlan summary;
  const auto none = Read(
      R"({"output":[{"type":"message","content":[{"type":"output_text","text":"42"}]}]})");
  Q4T_CHECK(
      PlanResponsesSummary(envelope, none, 256, 3, Config(), &summary, &error));
  Q4T_CHECK(!summary.needed && !summary.budget_exhausted);
  const auto actual = Read(
      R"({"output":[{"type":"reasoning","content":[{"type":"reasoning_text","text":"actual analysis"}],"summary":[]}]})");
  Q4T_CHECK(PlanResponsesSummary(envelope, actual, 256, 256, Config(), &summary,
                                 &error));
  Q4T_CHECK(!summary.needed && summary.budget_exhausted);
  Q4T_CHECK(!PlanResponsesSummary(envelope, actual, 256, 257, Config(),
                                  &summary, &error));
  Q4T_CHECK(error.http_status == 500 && !summary.needed);
  return true;
}

Q4T_TEST(responses_plan_defaults_keep_real_storage_requirement) {
  ResponsesEnvelope envelope;
  ChatPlanError error;
  Q4T_CHECK(Envelope(R"({"input":"你好"})", &envelope, &error));
  Q4T_CHECK(envelope.store && !envelope.background && !envelope.stream);
  Q4T_CHECK(envelope.options.GetString("model") == "base");
  Q4T_CHECK(envelope.options.GetInt("max_output_tokens") == 256);
  Q4T_CHECK(envelope.options.GetInt("temperature") == 1);
  Q4T_CHECK(envelope.reasoning_effort == "xhigh" &&
            envelope.reasoning_context == "all_turns");
  Q4T_CHECK(envelope.options.GetString("service_tier") == "default");
  Q4T_CHECK(!envelope.logprobs && envelope.obfuscation);
  ResponsesGenerationPlan plan;
  Q4T_CHECK(PrepareResponsesGeneration(
      envelope, *envelope.request.Find("input"), Config(), &plan, &error));
  Q4T_CHECK(plan.chat.sampling.choices == 1);
  Q4T_CHECK(plan.generation_request.GetArray("messages")
                ->array[0]
                .GetString("content") == "你好");
  Q4T_CHECK(!plan.generation_request.Has("store"));
  Q4T_CHECK(envelope.store);  // Still required in the owning service stage.
  return true;
}

Q4T_TEST(responses_plan_null_defaults_and_explicit_sampling) {
  ResponsesEnvelope envelope;
  ChatPlanError error;
  Q4T_CHECK(Envelope(R"({"input":"x","store":null,"background":null,
      "stream":null,"temperature":null,"top_p":null,"parallel_tool_calls":null,
      "max_output_tokens":null,"metadata":null,"reasoning":null})",
                     &envelope, &error));
  Q4T_CHECK(envelope.store && envelope.options.GetBool("parallel_tool_calls"));
  Q4T_CHECK(Envelope(R"({"input":"x","model":"org/alias","temperature":0,
    "top_p":0,"max_output_tokens":32,"store":false,"stream":true,
    "stream_options":{"include_obfuscation":false},"top_logprobs":3,
    "seed":9223372036854775807,"reasoning":{"effort":"none"}})",
                     &envelope, &error));
  ResponsesGenerationPlan plan;
  Q4T_CHECK(PrepareResponsesGeneration(
      envelope, *envelope.request.Find("input"), Config(), &plan, &error));
  Q4T_CHECK(!envelope.store && envelope.stream && !envelope.obfuscation);
  Q4T_CHECK(plan.chat.sampling.temperature == 0 &&
            plan.chat.sampling.top_p == 0);
  Q4T_CHECK(plan.chat.sampling.logprobs &&
            plan.chat.sampling.top_logprobs == 3);
  Q4T_CHECK(plan.chat.sampling.seed == INT64_MAX);
  Q4T_CHECK(!plan.generation_request.GetBool("enable_thinking", true));
  return true;
}

Q4T_TEST(responses_plan_rejects_invalid_public_types_and_exact_numbers) {
  for (
      const auto& [fields, param] :
      std::vector<std::pair<std::string, std::string>>{
          {R"("stream":"yes")", "stream"},
          {R"("store":1)", "store"},
          {R"("max_output_tokens":0)", "max_output_tokens"},
          {R"("max_output_tokens":1.5)", "max_output_tokens"},
          {R"("temperature":2.00000000000000001)", "temperature"},
          {R"("top_p":1.00000000000000001)", "top_p"},
          {R"("top_logprobs":21)", "top_logprobs"},
          {R"("seed":9223372036854775808)", "seed"},
          {R"("seed":1.00000000000000001)", "seed"},
          {R"("stream_options":{})", "stream_options"},
          {R"("stream":true,"stream_options":{"include_usage":true})",
           "stream_options.include_usage"},
          {R"("include":["unknown"])", "include[0]"},
          {R"("reasoning":{"summary":"detailed","generate_summary":"concise"})",
           "reasoning.summary"},
          {R"("text":{"verbosity":"huge"})", "text.verbosity"},
          {R"("response_format":{"type":"text"})", "response_format"}}) {
    ResponsesEnvelope envelope;
    ChatPlanError error;
    Q4T_CHECK(!Envelope("{\"input\":\"x\"," + fields + '}', &envelope, &error));
    Q4T_CHECK(error.http_status == 400 && error.param == param);
  }
  return true;
}

Q4T_TEST(responses_plan_model_alias_lookup_and_client_budget_before_state) {
  ResponsesEnvelope envelope;
  ChatPlanError error;
  Q4T_CHECK(!Envelope(R"({"model":"missing","input":"x"})", &envelope, &error));
  Q4T_CHECK(error.http_status == 404 && error.code == "model_not_found");
  ClientLimits limits;
  limits.max_output_tokens = 16;
  auto config = Config();
  config.client_limits = &limits;
  Q4T_CHECK(ParseResponsesEnvelope(
      Read(R"({"input":"x","previous_response_id":"resp_a"})"), config,
      &envelope, &error));
  Q4T_CHECK(envelope.options.GetInt("max_output_tokens") == 16);
  Q4T_CHECK(
      !ParseResponsesEnvelope(Read(R"({"input":"x","max_output_tokens":17})"),
                              config, &envelope, &error));
  Q4T_CHECK(error.code == "client_output_limit_exceeded");
  return true;
}

Q4T_TEST(responses_plan_metadata_unicode_limits_and_caller_identity) {
  ResponsesEnvelope envelope;
  ChatPlanError error;
  std::string key, value;
  for (int i = 0; i < 64; ++i) key += "键";
  for (int i = 0; i < 512; ++i) value += "值";
  const auto metadata = "\"metadata\":{\"" + key + "\":\"" + value + "\"}";
  Q4T_CHECK(Envelope(
      "{" + metadata + ",\"user\":\"caller\",\"safety_identifier\":\"group\"}",
      &envelope, &error));
  Q4T_CHECK(envelope.options.Find("metadata")->object[0].first == key);
  Q4T_CHECK(envelope.options.GetString("user") == "caller");
  Q4T_CHECK(
      !Envelope("{\"metadata\":{\"" + key + "多\":\"x\"}}", &envelope, &error));
  Q4T_CHECK(!Envelope("{\"metadata\":{\"k\":\"" + value + "多\"}}", &envelope,
                      &error));
  Q4T_CHECK(!Envelope(R"({"metadata":{"x":42}})", &envelope, &error));
  return true;
}

Q4T_TEST(responses_plan_lifecycle_fields_survive_and_conflicts_fail) {
  ResponsesEnvelope envelope;
  ChatPlanError error;
  Q4T_CHECK(Envelope(R"({"background":true,"store":true,
      "conversation":{"id":"conv_a"},"prompt":{"id":"prompt_a","version":"2"},
      "prompt_cache_key":"bucket","prompt_cache_retention":"24h",
      "prompt_cache_options":{"mode":"explicit","prewarm":true},
      "context_management":[{"type":"compaction","compact_threshold":4096}],
      "moderation":{"model":"actual-backend"},"service_tier":"priority"})",
                     &envelope, &error));
  Q4T_CHECK(envelope.background && envelope.conversation_id == "conv_a");
  Q4T_CHECK(envelope.options.Find("prompt")->GetString("version") == "2");
  Q4T_CHECK(envelope.request.Has("context_management"));
  Q4T_CHECK(envelope.cache.prewarm &&
            !envelope.options.Find("prompt_cache_options")->Has("prewarm"));
  Q4T_CHECK(envelope.options.GetString("service_tier") == "priority");
  // Parsing retains execution requirements; this is not a moderation/cache/
  // background acceptance. Their owners must execute before any success.
  Q4T_CHECK(
      Envelope(R"({"background":true,"store":false})", &envelope, &error));
  Q4T_CHECK(envelope.background && !envelope.store);
  Q4T_CHECK(
      !Envelope(R"({"conversation":"conv_a","previous_response_id":"resp_a"})",
                &envelope, &error));
  Q4T_CHECK(error.param == "previous_response_id");
  Q4T_CHECK(!Envelope(R"({"conversation":{"id":42}})", &envelope, &error));
  Q4T_CHECK(error.param == "conversation");
  return true;
}

Q4T_TEST(responses_plan_structured_format_and_error_paths) {
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan plan;
  ChatPlanError error;
  Q4T_CHECK(Envelope(R"({"input":"answer","reasoning":{"effort":"none"},
    "text":{"format":{"type":"json_schema","name":"answer","strict":true,
    "schema":{"type":"object","properties":{"answer":{"type":"integer"}},
    "required":["answer"],"additionalProperties":false}}}})",
                     &envelope, &error));
  Q4T_CHECK(PrepareResponsesGeneration(
      envelope, *envelope.request.Find("input"), Config(), &plan, &error));
  Q4T_CHECK(plan.chat.constraints.json_output && plan.chat.constraints.active);
  auto bad = Read(R"({"input":"x","text":{"format":{"type":"json_schema",
      "name":"answer","strict":true,"schema":{"type":"object",
      "properties":{"x":{"type":"string"}}}}}})");
  Q4T_CHECK(ParseResponsesEnvelope(bad, Config(), &envelope, &error));
  Q4T_CHECK(!PrepareResponsesGeneration(envelope, *bad.Find("input"), Config(),
                                        &plan, &error));
  Q4T_CHECK(error.param.starts_with("text.format.schema"));
  return true;
}

Q4T_TEST(responses_plan_reasoning_effort_and_verbosity_model_adaptation) {
  for (const auto* effort :
       {"none", "minimal", "low", "medium", "high", "xhigh", "max"}) {
    ResponsesEnvelope envelope;
    ResponsesGenerationPlan plan;
    ChatPlanError error;
    Q4T_CHECK(Envelope("{\"input\":\"answer\",\"reasoning\":{\"effort\":\"" +
                           std::string(effort) +
                           "\"},\"text\":{\"verbosity\":\"high\"}}",
                       &envelope, &error));
    Q4T_CHECK(PrepareResponsesGeneration(
        envelope, *envelope.request.Find("input"), Config(), &plan, &error));
    Q4T_CHECK(plan.generation_request.GetBool("enable_thinking") ==
              (std::string_view(effort) != "none"));
    std::vector<std::string> contents;
    for (const auto& message :
         plan.generation_request.GetArray("messages")->array)
      contents.push_back(message.GetString("content"));
    std::string prompt;
    Q4T_CHECK(RenderChatPrompt(plan.generation_request, contents, &prompt));
    Q4T_CHECK(prompt.find("detailed final answer") != prompt.npos);
    Q4T_CHECK(prompt.ends_with(std::string_view(effort) == "none"
                                   ? "<think>\n\n</think>\n\n"
                                   : "<think>\n"));
  }
  return true;
}

Q4T_TEST(responses_plan_current_turn_reasoning_and_new_instructions) {
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan plan;
  ChatPlanError error;
  Q4T_CHECK(Envelope(
      R"({"instructions":"new instructions","reasoning":{"context":"current_turn"}})",
      &envelope, &error));
  const auto input = Read(R"([
    {"role":"user","content":"old task"},
    {"type":"reasoning","id":"rs_old","summary":[],
     "content":[{"type":"reasoning_text","text":"old reasoning"}]},
    {"role":"assistant","content":"old answer"},
    {"role":"user","content":"new task"},
    {"type":"reasoning","id":"rs_current","summary":[],
     "content":[{"type":"reasoning_text","text":"current reasoning"}]}])");
  Q4T_CHECK(
      PrepareResponsesGeneration(envelope, input, Config(), &plan, &error));
  Q4T_CHECK(plan.input.items.array.size() == 5);
  const auto json = OutputJson(plan.generation_request);
  Q4T_CHECK(json.find("old reasoning") == json.npos);
  Q4T_CHECK(json.find("current reasoning") != json.npos);
  Q4T_CHECK(json.find("new instructions") != json.npos);
  return true;
}

Q4T_TEST(responses_plan_selected_probability_does_not_change_chat_sentinel) {
  SampledToken sample;
  sample.token = 0;
  sample.logprob = -9999;
  sample.raw_logprob = -23.5;
  for (bool responses : {false, true}) {
    TokenLogprobs records(true, false, 0, responses);
    Q4T_CHECK(records.Push(sample, "x", [](int32_t, std::string* text) {
      *text = "x";
      return q4t::Status();
    }));
    records.Map(std::vector<q4t::server::detail::Utf8Span>{{0, 1, 0, 1}});
    ResponseDelta delta;
    delta.text = "x";
    delta.source_end = 1;
    std::string probabilities;
    Q4T_CHECK(records.Emit(delta, &probabilities));
    const auto row = Read(probabilities).GetArray("content")->array[0];
    Q4T_CHECK(row.GetNumber("logprob") == (responses ? -23.5 : -9999));
  }
  return true;
}
