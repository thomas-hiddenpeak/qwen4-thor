// Opt-in real XGrammar CPU contracts. Defined/compiled, not executed during
// the development-only stage. No synthetic test substitutes for Thor E2E.
#include "q4t/server/structured_decoder.h"
#include "q4t/server/sampling.h"
#include "q4t/server/thinking_budget.h"

#include <cstring>
#include <limits>
#include <stdexcept>

#include "q4t/test.h"

namespace {
using q4t::server::OutputConstraints;
using q4t::server::StructuredDecoder;

uint16_t Bf16(float value) {
  uint32_t bits;
  std::memcpy(&bits, &value, sizeof(bits));
  return static_cast<uint16_t>(bits >> 16);
}

OutputConstraints Plan(const std::string& fields) {
  q4t::io::Json request;
  if (!q4t::io::ParseJson(
          std::string(R"({"messages":[{"role":"user","content":"q"}],)") +
              fields + '}',
          &request, true))
    throw std::runtime_error("bad grammar test fixture");
  OutputConstraints plan;
  const auto status = q4t::server::PlanOutputConstraints(request, &plan);
  if (!status) throw std::runtime_error(status.message());
  return plan;
}

std::vector<std::string> Vocabulary() {
  std::vector<std::string> result(258);
  for (int i = 0; i < 256; ++i)
    result[i + 1] = std::string(1, static_cast<char>(i));
  result[257] = "{}trailing";  // One token crossing a JSON root boundary.
  return result;  // 0 is EOS, 258/259 are padded, non-decodable logits.
}

bool Exact(const OutputConstraints& plan, const std::string& text) {
  std::unique_ptr<StructuredDecoder> decoder;
  if (!StructuredDecoder::Create(plan, Vocabulary(), 260, {0}, &decoder))
    return false;
  for (const unsigned char c : text) {
    std::vector<uint16_t> logits(260, Bf16(-20));
    logits[259] = Bf16(40);  // Padding can never win, even in free text.
    logits[c + 1] = Bf16(20);
    int32_t token;
    if (!decoder->Select(logits, &token) || token != c + 1) return false;
  }
  std::vector<uint16_t> logits(260, Bf16(-20));
  logits[0] = Bf16(20);
  logits[259] = Bf16(40);
  int32_t token;
  return !decoder->Terminated() && decoder->Select(logits, &token).ok() &&
         token == 0 && decoder->Terminated();
}

std::string Tools(std::string_view choice, bool parallel) {
  return std::string(
             R"("enable_thinking":false,"tools":[)"
             R"({"type":"function","function":{"name":"read","strict":true,)"
             R"("parameters":{"type":"object","properties":{)"
             R"("path":{"type":"string","enum":["docs/STATUS.md"]}},)"
             R"("required":["path"],"additionalProperties":false}}},)"
             R"({"type":"function","function":{"name":"noop"}}],"tool_choice":)") +
         std::string(choice) + R"(,"parallel_tool_calls":)" +
         (parallel ? "true" : "false");
}

constexpr auto kCall =
    "<tool_call>\n<function=read>\n"
    "<parameter=path>docs/STATUS.md</parameter>\n</function>\n</tool_call>";
}  // namespace

Q4T_TEST(structured_decoder_json_utf8_and_stop_mask) {
  const auto plan = Plan(
      R"("enable_thinking":false,"response_format":{)"
      R"("type":"json_schema","json_schema":{"name":"answer","strict":true,)"
      R"("schema":{"type":"object","properties":{"text":{)"
      R"("type":"string","enum":["中文"]}},"required":["text"],)"
      R"("additionalProperties":false}}})");
  Q4T_CHECK(Exact(plan, R"({"text":"中文"})"));
  Q4T_CHECK(!Exact(plan, R"({"text":"wrong"})"));
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(StructuredDecoder::Create(plan, Vocabulary(), 260, {0}, &decoder));
  std::vector<uint16_t> logits(260, Bf16(-20));
  logits[0] = Bf16(50);    // EOS before root completion.
  logits[257] = Bf16(60);  // Root + garbage in one token.
  logits[259] = Bf16(70);  // A padded token.
  logits['{' + 1] = Bf16(20);
  int32_t token;
  Q4T_CHECK(decoder->Select(logits, &token) && token == '{' + 1);
  return true;
}

Q4T_TEST(structured_decoder_required_named_and_parallel_masks) {
  const auto single = Plan(Tools(R"("required")", false));
  const auto multiple = Plan(Tools(R"("required")", true));
  Q4T_CHECK(Exact(single, kCall));
  Q4T_CHECK(!Exact(single, "ordinary answer"));
  Q4T_CHECK(!Exact(single, std::string(kCall) + '\n' + kCall));
  Q4T_CHECK(Exact(multiple, std::string(kCall) + '\n' + kCall));
  const auto named =
      Plan(Tools(R"({"type":"function","function":{"name":"read"}})", true));
  Q4T_CHECK(Exact(named, kCall));
  Q4T_CHECK(!Exact(named, std::string(kCall) + '\n' + kCall));
  Q4T_CHECK(!Exact(named,
                   "<tool_call>\n<function=noop>\n"
                   "\n</function>\n</tool_call>"));
  return true;
}

Q4T_TEST(structured_decoder_sampling_obeys_mask_before_accept) {
  const auto plan = Plan(
      R"("enable_thinking":false,"response_format":{"type":"json_object"})");
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(StructuredDecoder::Create(plan, Vocabulary(), 260, {0}, &decoder));
  q4t::server::SamplingOptions options;
  options.seed = 123;
  options.temperature = 0.8;
  options.logprobs = true;
  options.top_logprobs = 2;
  options.logit_bias = {{0, 100}, {259, 100}};
  q4t::server::TokenSampler sampler;
  Q4T_CHECK(sampler.Init(options, 260, 0));
  q4t::server::SampledToken sample;
  int32_t token = -1;
  std::vector<uint16_t> logits(260, Bf16(-20));
  logits[259] = Bf16(40);  // Invalid padding cannot win, even with bias.
  logits[0] = Bf16(50);    // EOS cannot truncate a required JSON root.
  logits['{' + 1] = Bf16(20);
  Q4T_CHECK(!decoder->Select(logits, &token, 0, &sampler, &sample));
  Q4T_CHECK(token == -1 && sample.token == -1 && !decoder->Terminated());
  Q4T_CHECK(decoder->Select(logits, &token, -1, &sampler, &sample));
  Q4T_CHECK(token == '{' + 1 && sample.token == token);
  Q4T_CHECK(sample.top.size() == 2 && sample.top[0].token == 0);
  Q4T_CHECK(sampler.Observe(token));
  logits['{' + 1] = Bf16(-20);
  logits['}' + 1] = Bf16(20);
  Q4T_CHECK(decoder->Select(logits, &token, -1, &sampler, &sample));
  Q4T_CHECK(token == '}' + 1 && sampler.Observe(token));
  Q4T_CHECK(decoder->Select(logits, &token, -1, &sampler, &sample));
  Q4T_CHECK(token == 0 && sample.token == 0 && decoder->Terminated());
  return true;
}

Q4T_TEST(structured_decoder_none_and_auto_control) {
  const auto none = Plan(Tools(R"("none")", true));
  Q4T_CHECK(Exact(none, "a normal answer"));
  Q4T_CHECK(!Exact(none, kCall));
  const auto automatic = Plan(Tools(R"("auto")", false));
  Q4T_CHECK(Exact(automatic, "a normal answer"));
  Q4T_CHECK(Exact(automatic, std::string("reading ") + kCall));
  Q4T_CHECK(!Exact(automatic, std::string(kCall) + '\n' + kCall));
  return true;
}

Q4T_TEST(structured_decoder_json_object_and_thinking) {
  const auto closed = Plan(
      R"("enable_thinking":false,"response_format":{"type":"json_object"})");
  Q4T_CHECK(Exact(closed, R"({"arbitrary":[1,{"nested":true},null]})"));
  Q4T_CHECK(!Exact(closed, "[]"));
  const auto thinking = Plan(R"("response_format":{"type":"json_object"})");
  Q4T_CHECK(Exact(thinking, "reason</think>\n\n{\"text\":\"<think>\"}"));
  Q4T_CHECK(!Exact(thinking, "reason only"));
  return true;
}

Q4T_TEST(structured_decoder_no_finite_candidate_or_bad_vocabulary) {
  const auto plan = Plan(
      R"("enable_thinking":false,"response_format":{"type":"json_object"})");
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(!StructuredDecoder::Create(plan, {}, 260, {0}, &decoder));
  Q4T_CHECK(
      !StructuredDecoder::Create(plan, Vocabulary(), 260, {260}, &decoder));
  Q4T_CHECK(!StructuredDecoder::Create(plan, Vocabulary(), 10, {0}, &decoder));
  Q4T_CHECK(StructuredDecoder::Create(plan, Vocabulary(), 260, {0}, &decoder));
  std::vector<uint16_t> logits(260,
                               Bf16(std::numeric_limits<float>::quiet_NaN()));
  int32_t token = 123;
  Q4T_CHECK(!decoder->Select(logits, &token) && token == -1);
  Q4T_CHECK(!decoder->Terminated());
  return true;
}

Q4T_TEST(structured_decoder_cancel_after_compiler_entry_reaps) {
  const auto plan = Plan(
      R"("enable_thinking":false,"response_format":{"type":"json_object"})");
  q4t::server::GrammarControl control;
  bool compiling = false, ready = false, reaped = false;
  control.worker_ready = [&] { ready = true; };
  control.compile_started = [&] { compiling = true; };
  control.cancelled = [&] { return compiling; };
  control.worker_finished = [&](const auto& report) {
    reaped = report.reaped && report.reported_rss_bytes > 0 &&
             report.reported_rss_bytes <= control.address_space_bytes;
  };
  q4t::server::GrammarFailure failure;
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(!StructuredDecoder::Create(plan, Vocabulary(), 260, {0}, &decoder,
                                       control, &failure));
  Q4T_CHECK(ready && compiling && reaped && !decoder &&
            failure == q4t::server::GrammarFailure::kCancelled);
  return true;
}

Q4T_TEST(structured_decoder_expired_and_memory_failure_are_closed) {
  const auto plan = Plan(
      R"("enable_thinking":false,"response_format":{"type":"json_object"})");
  q4t::server::GrammarControl control;
  control.compile_deadline = std::chrono::steady_clock::now();
  q4t::server::GrammarFailure failure;
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(!StructuredDecoder::Create(plan, Vocabulary(), 260, {0}, &decoder,
                                       control, &failure));
  Q4T_CHECK(failure == q4t::server::GrammarFailure::kTimeout && !decoder);
  control = {};
  control.address_space_bytes = 1024 * 1024;  // Below loaded ELF baseline.
  bool reaped = false;
  control.worker_finished = [&](const auto& report) { reaped = report.reaped; };
  Q4T_CHECK(!StructuredDecoder::Create(plan, Vocabulary(), 260, {0}, &decoder,
                                       control, &failure));
  Q4T_CHECK(failure == q4t::server::GrammarFailure::kResource && !decoder &&
            reaped);
  Q4T_CHECK(StructuredDecoder::Create(plan, Vocabulary(), 260, {0}, &decoder));
  return true;
}

Q4T_TEST(structured_decoder_forced_token_intersection_is_transactional) {
  const auto plan = Plan(
      R"("enable_thinking":false,"response_format":{"type":"json_object"})");
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(StructuredDecoder::Create(plan, Vocabulary(), 260, {0}, &decoder));
  std::vector<uint16_t> logits(260, Bf16(-20));
  int32_t token = -1;
  for (int32_t invalid : {0, 'a' + 1, 257, 259, 260, -2}) {
    Q4T_CHECK(!decoder->Select(logits, &token, invalid));
    Q4T_CHECK(token == -1 && !decoder->Terminated());
  }
  logits['{' + 1] = Bf16(std::numeric_limits<float>::quiet_NaN());
  Q4T_CHECK(!decoder->Select(logits, &token, '{' + 1));
  logits['{' + 1] = Bf16(-20);
  // None of the rejected selections advanced the matcher.
  Q4T_CHECK(decoder->Select(logits, &token, '{' + 1) && token == '{' + 1);
  Q4T_CHECK(decoder->Select(logits, &token, '}' + 1) && token == '}' + 1);
  Q4T_CHECK(decoder->Select(logits, &token, 0) && token == 0);
  Q4T_CHECK(decoder->Terminated());
  return true;
}

Q4T_TEST(structured_decoder_budget_closure_and_utf8_share_accepted_tokens) {
  const auto plan = Plan(R"("response_format":{"type":"json_object"})");
  for (int limit : {0, 2}) {
    auto vocabulary = Vocabulary();
    std::unique_ptr<StructuredDecoder> decoder;
    Q4T_CHECK(StructuredDecoder::Create(plan, vocabulary, 260, {0}, &decoder));
    q4t::server::ThinkingBudget budget;
    Q4T_CHECK(budget.Init(limit, "<think>\n", true, false));
    const int32_t stops[] = {0};
    Q4T_CHECK(budget.PrepareTokens(
        [](std::string_view text, std::vector<int32_t>* ids) {
          ids->clear();
          for (unsigned char c : text) ids->push_back(c + 1);
          return q4t::Status();
        },
        [&](int32_t id, std::string* text) {
          *text = vocabulary[id];
          return q4t::Status();
        },
        258, stops));
    std::vector<uint16_t> logits(260, Bf16(-20));
    const int32_t prefix[] = {0xe4 + 1, 0xb8 + 1};
    for (int i = 0; i < limit; ++i) {
      const auto expected = prefix[i];
      logits[expected] = Bf16(20);
      int32_t token;
      Q4T_CHECK(decoder->Select(logits, &token) && token == expected);
      Q4T_CHECK(budget.Observe(token, vocabulary[token]));
      logits[expected] = Bf16(-20);
    }
    std::string forced;
    for (int32_t next; (next = budget.ForcedToken()) >= 0;) {
      int32_t token;
      // Prefer a different, valid text token in the raw logits. The forced
      // token must win before acceptance, including the UTF-8 continuation
      // byte.
      logits['x' + 1] = Bf16(100);
      Q4T_CHECK(decoder->Select(logits, &token, next) && token == next);
      Q4T_CHECK(budget.Observe(token, vocabulary[token]));
      forced += vocabulary[token];
    }
    Q4T_CHECK(forced == (limit ? "\x80\n</think>\n\n" : "\n</think>\n\n"));
    Q4T_CHECK(budget.Consumed() == limit);
    Q4T_CHECK(budget.ForcedTokens() == (limit ? 12 : 11) && !budget.Thinking());
    for (const unsigned char c :
         std::string("{\"example\":\"<think>literal</think>\"}")) {
      int32_t token;
      Q4T_CHECK(decoder->Select(logits, &token, c + 1) && token == c + 1);
      Q4T_CHECK(budget.Allows(vocabulary[token]));
      Q4T_CHECK(budget.Observe(token, vocabulary[token]));
    }
    int32_t token;
    Q4T_CHECK(decoder->Select(logits, &token, 0) && token == 0);
    Q4T_CHECK(decoder->Terminated() &&
              budget.ReasoningTokens() == (limit ? 14 : 11));
  }
  return true;
}
