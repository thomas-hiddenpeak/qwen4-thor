#include "q4t/server/chat_choices.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/token_logprobs.h"

#include <limits>
#include <stdexcept>
#include "q4t/server/text_stop.h"

#include "q4t/server/chat_prompt.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;

Json Read(const std::string& wire) {
  Json value;
  if (!q4t::io::ParseJson(wire, &value, true))
    throw std::runtime_error("bad response test JSON");
  return value;
}

struct LogprobPipeline {
  explicit LogprobPipeline(const std::string& fields) : records(true) {
    request =
        Read(R"({"messages":[{"role":"user","content":"q"}],)" + fields + '}');
    const auto thinking = request.GetBool("enable_thinking", true);
    const std::string prefix =
        thinking ? "<think>\n" : "<think>\n\n</think>\n\n";
    if (!parser.Init(request, prefix, "test", true))
      throw std::runtime_error("bad response parser fixture");
  }
  bool Push(std::string piece) {
    vocabulary.push_back(std::move(piece));
    const int32_t token = static_cast<int32_t>(vocabulary.size() - 1);
    SampledToken sample{token, -0.25, {{token, -0.25}}};
    if (!records.Push(sample, vocabulary.back(),
                      [&](int32_t id, std::string* bytes) {
                        *bytes = vocabulary.at(id);
                        return q4t::Status();
                      }))
      return false;
    std::vector<detail::Utf8Span> spans;
    const auto output = utf8.Append(vocabulary.back(), &spans);
    records.Map(spans);
    std::vector<ResponseDelta> deltas;
    return parser.Append(stop.Append(output), &deltas) && Emit(deltas);
  }
  bool Finish() {
    std::vector<detail::Utf8Span> spans;
    const auto tail = utf8.Finish(&spans);
    records.Map(spans);
    std::vector<ResponseDelta> deltas;
    return parser.Append(stop.Append(tail), &deltas) && Emit(deltas) &&
           parser.Append(stop.Finish(), &deltas) && Emit(deltas) &&
           parser.Finish(false, &deltas, stop.Matched()) && Emit(deltas);
  }
  bool Emit(std::vector<ResponseDelta>& deltas) {
    for (const auto& delta : deltas) {
      std::string json;
      if (!records.Emit(delta, &json)) return false;
      if (delta.kind == ResponseDelta::Kind::kContent) visible += delta.text;
      frames.push_back(std::move(json));
    }
    deltas.clear();
    records.DiscardBefore(parser.ConsumedBytes());
    return true;
  }
  Json request;
  ChatResponseParser parser;
  TokenLogprobs records;
  detail::Utf8Stream utf8;
  TextStopStream stop{{}};
  std::vector<std::string> vocabulary;
  std::vector<std::string> frames;
  std::string visible;
};
}  // namespace

Q4T_TEST(sampling_response_utf8_token_bytes_survive_delayed_scalars) {
  LogprobPipeline pipe(R"("enable_thinking":false)");
  Q4T_CHECK(pipe.Push("\xe4") && pipe.Push("\xb8\xad") && pipe.Push("!"));
  Q4T_CHECK(pipe.Finish() && pipe.visible == "中!");
  const auto result = Read(pipe.records.Json());
  const auto& entries = result.Find("content")->array;
  Q4T_CHECK(entries.size() == 3);
  Q4T_CHECK(entries[0].GetString("token") == "\xef\xbf\xbd");
  Q4T_CHECK(entries[0].Find("bytes")->array[0].number == 228);
  Q4T_CHECK(entries[1].Find("bytes")->array.size() == 2);
  Q4T_CHECK(entries[2].GetString("token") == "!");
  Q4T_CHECK(entries[0].Find("top_logprobs")->array.size() == 1);
  return true;
}

Q4T_TEST(sampling_response_repaired_bytes_keep_original_probabilities) {
  LogprobPipeline pipe(R"("enable_thinking":false)");
  Q4T_CHECK(pipe.Push("\xff") && pipe.Push("A\xe4") && pipe.Finish());
  Q4T_CHECK(pipe.visible ==
            "\xef\xbf\xbd"
            "A\xef\xbf\xbd");
  const auto result = Read(pipe.records.Json());
  const auto& entries = result.Find("content")->array;
  Q4T_CHECK(entries.size() == 2);
  Q4T_CHECK(entries[1].GetString("token") == "A\xef\xbf\xbd");
  Q4T_CHECK(entries[1].Find("bytes")->array[1].number == 228);
  Q4T_CHECK(entries[1].Find("logprob")->number == -0.25);
  return true;
}

Q4T_TEST(sampling_response_hidden_reasoning_and_stop_are_not_content) {
  LogprobPipeline pipe(R"("include_reasoning":false)");
  pipe.stop = TextStopStream({"END"});
  Q4T_CHECK(pipe.Push("secret") && pipe.Push("</thi") && pipe.Push("nk>"));
  Q4T_CHECK(pipe.Push("answer") && pipe.Push("E") && pipe.Push("NDhidden"));
  Q4T_CHECK(pipe.Finish() && pipe.visible == "answer");
  const auto result = Read(pipe.records.Json());
  const auto& entries = result.Find("content")->array;
  Q4T_CHECK(entries.size() == 1 && entries[0].GetString("token") == "answer");
  return true;
}

Q4T_TEST(sampling_response_boundary_crossing_token_is_never_retokenized) {
  LogprobPipeline pipe(R"("include_reasoning":false)");
  Q4T_CHECK(pipe.Push("thought</think>A") && pipe.Finish());
  Q4T_CHECK(pipe.visible == "A");
  const auto result = Read(pipe.records.Json());
  const auto& entries = result.Find("content")->array;
  Q4T_CHECK(entries.size() == 1);
  Q4T_CHECK(entries[0].GetString("token") == "thought</think>A");
  Q4T_CHECK(entries[0].Find("logprob")->number == -0.25);
  return true;
}

Q4T_TEST(sampling_response_constrained_deferred_content_keeps_provenance) {
  LogprobPipeline pipe(R"("response_format":{"type":"json_object"})");
  Q4T_CHECK(pipe.Push("hidden") && pipe.Push("</think>"));
  Q4T_CHECK(pipe.visible.empty());
  // JSON content became incremental in A05. Probability provenance follows
  // actual emitted bytes; reasoning remains hidden and quoted tags are data.
  Q4T_CHECK(pipe.Push("{\"text\":") && pipe.visible == "{\"text\":");
  Q4T_CHECK(pipe.Push("\"<think>\"}"));
  Q4T_CHECK(pipe.Finish() && pipe.visible == "{\"text\":\"<think>\"}");
  const auto result = Read(pipe.records.Json());
  Q4T_CHECK(result.Find("content")->array.size() == 2);
  return true;
}

Q4T_TEST(sampling_choices_share_prompt_and_aggregate_real_usage) {
  ChatChoices choices(2, false);
  const auto message = R"({"role":"assistant","content":"x"})";
  Q4T_CHECK(choices.Add(0, message, "stop", "null", 10, 7, 3, true, 4, 3, 1));
  std::string output;
  Q4T_CHECK(!choices.Complete() && !choices.Json("id", "m", 1, &output));
  Q4T_CHECK(!choices.Add(0, message, "stop", "null", 10, 7, 3, true, 4, 3, 1));
  Q4T_CHECK(!choices.Add(1, message, "stop", "null", 11, 7, 3, true, 4, 3, 1));
  Q4T_CHECK(
      choices.Add(1, message, "length", "null", 10, 9, 10, true, 4, 4, 2));
  Q4T_CHECK(choices.Complete() && choices.Json("id", "m", 1, &output));
  const auto result = Read(output);
  const auto& rows = result.Find("choices")->array;
  const auto& usage = *result.Find("usage");
  Q4T_CHECK(rows.size() == 2 && rows[1].GetInt("index") == 1);
  Q4T_CHECK(usage.GetInt("prompt_tokens") == 10 &&
            usage.GetInt("completion_tokens") == 16 &&
            usage.GetInt("total_tokens") == 26);
  Q4T_CHECK(usage.Find("prompt_tokens_details")->GetInt("cached_tokens") == 3);
  Q4T_CHECK(
      usage.Find("completion_tokens_details")->GetInt("reasoning_tokens") ==
      10);
  return true;
}

Q4T_TEST(sampling_choices_accounting_and_storage_fail_closed) {
  ChatChoices choices(2, true);
  Q4T_CHECK(choices.Add(0, "", "stop", "null", 1, 2, 0, false, -1, 0, 0));
  Q4T_CHECK(!choices.Add(1, "", "stop", "null", 1,
                         std::numeric_limits<uint64_t>::max(), 0, false, -1, 0,
                         0));
  Q4T_CHECK(!choices.Add(1, "", "stop", "null", 1, 2, 0, false, 3, 3, 0));
  Q4T_CHECK(choices.CompletionTokens() == 2 && !choices.Complete());
  ChatChoices json(1, false);
  Q4T_CHECK(!json.Add(0, std::string(ChatChoices::kResponseLimit, 'x'), "stop",
                      "null", 1, 1, 0, false, -1, 0, 0));
  return true;
}

Q4T_TEST(sampling_plan_token_bias_and_choice_quota_are_not_shape_only) {
  ChatPlanConfig config;
  config.model = "m";
  config.token_vocab = 100;
  ClientLimits limits;
  limits.max_output_tokens = 20;
  limits.max_prompt_tokens = 8192;
  config.client_limits = &limits;
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanChatRequest(Read(R"({"prompt":"q","max_tokens":2,
      "logit_bias":{"100":1}})"),
                             config, &plan, &error));
  Q4T_CHECK(error.param == "logit_bias[\"100\"]");
  Q4T_CHECK(!PlanChatRequest(Read(R"({"prompt":"q","max_tokens":11,"n":2})"),
                             config, &plan, &error));
  Q4T_CHECK(error.code == "client_output_limit_exceeded");
  const auto request = Read(R"({"prompt":"q","max_tokens":10,"n":2})");
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  ChatPromptBudget budget;
  Q4T_CHECK(PlanChatBudget(request, config, 8190, &budget, &error));
  Q4T_CHECK(budget.available_output == 2 && budget.total_output == 4);
  return true;
}
