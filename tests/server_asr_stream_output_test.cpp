#include "q4t/server/asr_stream_output.h"

#include <limits>
#include <stdexcept>

#include "q4t/audio/asr_config.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using C = q4t::audio::AsrConfig;
using q4t::io::Json;
AsrPrompt Prompt(size_t output) {
  AsrPrompt prompt;
  prompt.tokens = {100, C::kAudioPad, 101};
  prompt.audio_start = prompt.audio_tokens = 1;
  prompt.output_tokens = output;
  return prompt;
}
AsrStreamPass Pass(const AsrPrompt& prompt,
                   std::initializer_list<AsrStreamToken> tokens,
                   double probability = -.25, double temperature = .2) {
  AsrStreamPass pass(prompt);
  if (!pass.Begin(prompt)) throw std::runtime_error("bad pass fixture");
  for (const auto& token : tokens)
    if (!pass.Observe({token.id, -9999, {}, probability}, token.bytes,
                      temperature))
      throw std::runtime_error("bad selection fixture");
  if (!pass.Observe({C::kImEnd, -.1, {}, -.1}, {}, temperature))
    throw std::runtime_error("bad actual EOS fixture");
  return pass;
}
AsrStreamPrefixLimits Immediate(size_t rollback = 1) {
  AsrStreamPrefixLimits limits;
  limits.unfixed_passes = 1;
  limits.rollback_tokens = rollback;
  return limits;
}
const AsrStreamOutput::Sink kDiscard = [](const AsrTextDelta&) { return true; };
}  // namespace

Q4T_TEST(asr_stream_capture_only_actual_eos_completes_and_consumes_budget) {
  for (int32_t eos : {C::kEos, C::kImEnd}) {
    const auto prompt = Prompt(2);
    AsrStreamPass pass(prompt);
    Q4T_CHECK(pass.Valid() && pass.Begin(prompt));
    Q4T_CHECK(pass.Observe({17, -99, {}, -.125}, "word", .2));
    Q4T_CHECK(!pass.Complete() && pass.Generated() == 1);
    Q4T_CHECK(pass.Observe({eos, -.3, {}, -.3}, {}, .4));
    Q4T_CHECK(pass.Complete() && pass.Generated() == 2);
    Q4T_CHECK(pass.Selections().size() == 1);
    const auto& first = pass.Selections().front();
    Q4T_CHECK(first.identity.id == 17 && first.identity.bytes == "word");
    Q4T_CHECK(first.raw_logprob == -.125 && first.logprob == -99 &&
              first.temperature == .2);
    Q4T_CHECK(!pass.Observe({eos, -.3, {}, -.3}, {}, .4));
    Q4T_CHECK(!pass.Complete());
  }
  auto prompt = Prompt(1);
  AsrStreamPass exhausted(prompt);
  Q4T_CHECK(exhausted.Begin(prompt));
  Q4T_CHECK(exhausted.Observe({1, -.1, {}, -.1}, "partial", 0));
  Q4T_CHECK(!exhausted.Complete());
  Q4T_CHECK(!exhausted.Observe({C::kEos, -.1, {}, -.1}, {}, 0));
  Q4T_CHECK(!exhausted.Complete() && exhausted.Generated() == 1);
  return true;
}

Q4T_TEST(asr_stream_capture_binds_exact_native_invocation_before_selection) {
  for (int mismatch = 0; mismatch < 6; ++mismatch) {
    auto actual = Prompt(8);
    AsrStreamPass pass(actual);
    if (mismatch == 0) actual.tokens[0] = 999;
    if (mismatch == 1) actual.audio_start = 0;
    if (mismatch == 2) actual.audio_tokens = 2;
    if (mismatch == 3) actual.output_tokens = 7;
    if (mismatch == 4) actual.continuation_tokens = 1;
    if (mismatch == 5) actual.no_speech_prefix = {1};
    Q4T_CHECK(!pass.Begin(actual));
    Q4T_CHECK(!pass.Observe({C::kEos, -.1, {}, -.1}, {}, 0));
    Q4T_CHECK(!pass.Complete() && pass.Generated() == 0);
  }
  AsrStreamPass unbound(Prompt(8));
  Q4T_CHECK(!unbound.Observe({1, -.1, {}, -.1}, "text", 0));
  Q4T_CHECK(!unbound.Begin(Prompt(8)));
  AsrStreamPass reused(Prompt(8));
  Q4T_CHECK(reused.Begin(Prompt(8)) && !reused.Begin(Prompt(8)));
  return true;
}

Q4T_TEST(asr_stream_capture_rejects_invalid_numbers_bytes_and_ids) {
  for (int fault = 0; fault < 10; ++fault) {
    auto prompt = Prompt(8);
    AsrStreamPass pass(prompt, 4);
    Q4T_CHECK(pass.Begin(prompt));
    SampledToken token{1, -.5, {}, -.25};
    std::string bytes = "word";
    double temperature = 0;
    if (fault == 0) token.token = -1;
    if (fault == 1) token.token = C::kVocab;
    if (fault == 2)
      token.raw_logprob = std::numeric_limits<double>::quiet_NaN();
    if (fault == 3) token.logprob = std::numeric_limits<double>::infinity();
    if (fault == 4) token.raw_logprob = .01;
    if (fault == 5) token.top.push_back({1, -.5});
    if (fault == 6) temperature = 1.01;
    if (fault == 7) temperature = -1;
    if (fault == 8) bytes = "large";
    if (fault == 9) token.token = C::kEos;
    Q4T_CHECK(!pass.Observe(token, bytes, temperature));
    Q4T_CHECK(!pass.Complete() && pass.Generated() == 0);
    Q4T_CHECK(!pass.Observe({C::kEos, -.1, {}, -.1}, {}, 0));
  }
  return true;
}

Q4T_TEST(
    asr_stream_forced_prompt_and_original_probabilities_survive_revisions) {
  AsrStreamOutput output(1, 16, true, "en", Immediate());
  std::string deltas;
  const auto sink = [&](const AsrTextDelta& delta) {
    deltas += delta.text;
    return true;
  };
  auto p1 = Prompt(output.Remaining());
  AsrTranscript t1(true, "en", true);
  Q4T_CHECK(output.Prepare(&p1, 128, &t1));
  Q4T_CHECK(
      output.Accept(Pass(p1, {{11, "Hello"}, {12, " friend"}}), false, sink));
  Q4T_CHECK(deltas == "Hello" && output.Prefix().size() == 1);
  auto p2 = Prompt(output.Remaining());
  AsrTranscript t2(true, "en", true);
  Q4T_CHECK(output.Prepare(&p2, 128, &t2));
  Q4T_CHECK((p2.tokens == std::vector<int32_t>{100, C::kAudioPad, 101, 11}));
  Q4T_CHECK(p2.audio_start == 1 && p2.audio_tokens == 1 &&
            p2.continuation_tokens == 1 && t2.Text() == "Hello");
  Q4T_CHECK(t2.TextTokens().size() == 1 && t2.TextTokens()[0].logprob == -.25);
  Q4T_CHECK(output.Accept(Pass(p2, {{21, " world"}, {22, "!"}}, -.5, .4), false,
                          sink));
  Q4T_CHECK(deltas == "Hello world");
  auto p3 = Prompt(output.Remaining());
  AsrTranscript t3(true, "en", true);
  Q4T_CHECK(output.Prepare(&p3, 128, &t3));
  Q4T_CHECK(
      (p3.tokens == std::vector<int32_t>{100, C::kAudioPad, 101, 11, 21}));
  Q4T_CHECK(output.Accept(Pass(p3, {{31, "?"}}, -.75, .6), true, sink));
  Q4T_CHECK(output.Finished() && output.Generated() == 8);
  Q4T_CHECK(deltas == "Hello world?" && output.Transcript().Text() == deltas);
  const auto actual = output.Transcript().TextTokens();
  Q4T_CHECK(actual.size() == 3 && actual[0].id == 11 && actual[1].id == 21 &&
            actual[2].id == 31 && actual[0].logprob == -.25 &&
            actual[1].logprob == -.5 && actual[2].logprob == -.75 &&
            actual[0].temperature == .2 && actual[1].temperature == .4 &&
            actual[2].temperature == .6);
  // All real selections, including revised-away suffixes and three EOS,
  // are output work. Repeated prompt/forced prefixes are input work.
  const ActualUsage usage{
      p1.tokens.size() + p2.tokens.size() + p3.tokens.size(),
      output.Generated()};
  Q4T_CHECK(usage.input == 12 && usage.output == 8);
  std::string json;
  Q4T_CHECK(output.Transcript().ResultJson(usage, 3, false, &json));
  Json parsed;
  Q4T_CHECK(q4t::io::ParseJson(json, &parsed));
  Q4T_CHECK(parsed.GetString("text") == deltas &&
            parsed.GetArray("logprobs")->array.size() == 3);
  return true;
}

Q4T_TEST(asr_stream_output_rejects_decoder_that_did_not_use_fixed_prefix) {
  for (bool same_size : {false, true}) {
    AsrStreamOutput output(2, 16, false, "en", Immediate());
    Q4T_CHECK(output.Accept(Pass(Prompt(16), {{1, "Hello"}, {2, " tail"}}),
                            false, kDiscard));
    auto bad = Prompt(output.Remaining());
    if (same_size) {
      bad.tokens.push_back(99);
      bad.continuation_tokens = 1;
    }
    Q4T_CHECK(!output.Accept(Pass(bad, {{3, " forged"}}), true, kDiscard));
    Q4T_CHECK(!output.Finished() && output.Transcript().Text() == "Hello");
    Q4T_CHECK(!output.Finalize(kDiscard));
  }
  return true;
}

Q4T_TEST(asr_stream_output_shared_budget_counts_unpublished_tokens_and_eos) {
  AsrStreamOutput output(3, 4, false, "en", Immediate());
  Q4T_CHECK(output.Accept(Pass(Prompt(4), {{1, "Hello"}, {2, " tail"}}), false,
                          kDiscard));
  Q4T_CHECK(output.Remaining() == 1 && output.Generated() == 3);
  auto excessive = Prompt(2);
  AsrTranscript temporary(false, "en");
  Q4T_CHECK(!output.Prepare(&excessive, 128, &temporary));
  Q4T_CHECK(excessive.tokens == Prompt(2).tokens && temporary.Text().empty());
  auto last = Prompt(1);
  Q4T_CHECK(output.Prepare(&last, 128, &temporary));
  Q4T_CHECK(output.Accept(Pass(last, {}), true, kDiscard));
  Q4T_CHECK(output.Finished() && output.Remaining() == 0 &&
            output.Transcript().Text() == "Hello");
  return true;
}

Q4T_TEST(asr_stream_output_eof_reuses_prior_eos_and_does_not_add_work) {
  AsrStreamOutput output(4, 16, false, "en");
  Q4T_CHECK(output.Accept(Pass(Prompt(16), {{1, "Hello"}, {2, " world"}}),
                          false, kDiscard));
  Q4T_CHECK(output.Transcript().Text().empty() && !output.Finished());
  std::string delta;
  Q4T_CHECK(output.Finalize([&](const AsrTextDelta& part) {
    delta += part.text;
    return true;
  }));
  Q4T_CHECK(output.Finished() && delta == "Hello world" &&
            output.Generated() == 3);
  AsrStreamOutput empty(5, 16, false, "en");
  Q4T_CHECK(!empty.Finalize(kDiscard) && !empty.Finished());
  AsrStreamPass unfinished(Prompt(16));
  Q4T_CHECK(unfinished.Begin(Prompt(16)));
  Q4T_CHECK(unfinished.Observe({1, -.1, {}, -.1}, "partial", 0));
  AsrStreamOutput partial(6, 16, false, "en");
  Q4T_CHECK(!partial.Accept(unfinished, true, kDiscard));
  Q4T_CHECK(!partial.Finalize(kDiscard) && !partial.Finished());
  return true;
}

Q4T_TEST(asr_stream_output_utf8_revisable_suffix_is_not_public_text) {
  AsrStreamOutput output(7, 16, true, "zh", Immediate(0));
  std::string delta;
  const auto sink = [&](const AsrTextDelta& part) {
    delta += part.text;
    return true;
  };
  Q4T_CHECK(output.Accept(Pass(Prompt(16), {{1, "\xe4\xbd"}}), false, sink));
  Q4T_CHECK(delta.empty() && output.Prefix().empty());
  // Revisions replace only uncommitted tokens and preserve actual byte pieces.
  Q4T_CHECK(output.Accept(
      Pass(Prompt(output.Remaining()), {{2, "\xe4"}, {3, "\xbd\xa0"}}, -.5),
      true, sink));
  Q4T_CHECK(output.Finished() && delta == "你");
  const auto tokens = output.Transcript().TextTokens();
  Q4T_CHECK(tokens.size() == 2 && tokens[0].id == 2 && tokens[1].id == 3 &&
            tokens[0].logprob == -.5 && tokens[1].logprob == -.5);
  AsrStreamOutput final_repair(8, 16, false, "zh", Immediate(0));
  Q4T_CHECK(final_repair.Accept(Pass(Prompt(16), {{1, "\xe4\xbd"}}), false,
                                kDiscard));
  Q4T_CHECK(final_repair.Finalize(kDiscard));
  Q4T_CHECK(final_repair.Finished() &&
            final_repair.Transcript().Text() == "\xef\xbf\xbd");
  return true;
}

Q4T_TEST(asr_stream_output_native_header_and_prefix_do_not_leak_or_duplicate) {
  AsrStreamOutput output(9, 16, true, "", Immediate());
  Q4T_CHECK(output.Accept(Pass(Prompt(16), {{1, "language English"},
                                            {2, "<asr_text>"},
                                            {3, "Hello"},
                                            {4, " old"}}),
                          false, kDiscard));
  Q4T_CHECK(output.Transcript().Text() == "Hello" &&
            output.Transcript().Language() == "en");
  auto prompt = Prompt(output.Remaining());
  AsrTranscript temporary(true, "", true);
  Q4T_CHECK(output.Prepare(&prompt, 128, &temporary));
  Q4T_CHECK(prompt.continuation_tokens == 3 && temporary.Text() == "Hello");
  Q4T_CHECK(output.Accept(Pass(prompt, {{5, " world"}}), true, kDiscard));
  Q4T_CHECK(output.Finished() && output.Transcript().Text() == "Hello world");
  Q4T_CHECK(output.Transcript().TextTokens().size() == 2);
  return true;
}

Q4T_TEST(asr_stream_output_failed_delivery_cannot_later_claim_completion) {
  AsrStreamOutput output(10, 16, false, "en", Immediate(0));
  int attempts = 0;
  Q4T_CHECK(!output.Accept(Pass(Prompt(16), {{1, "Hello"}}), true,
                           [&](const AsrTextDelta&) {
                             ++attempts;
                             return false;
                           }));
  Q4T_CHECK(attempts == 1 && !output.Finished());
  Q4T_CHECK(!output.Finalize(kDiscard));
  auto prompt = Prompt(output.Remaining());
  AsrTranscript temporary(false, "en");
  Q4T_CHECK(!output.Prepare(&prompt, 128, &temporary));
  return true;
}

Q4T_TEST(asr_stream_prepare_checks_context_before_mutating_model_input) {
  AsrStreamOutput output(11, 32, false, "en", Immediate());
  Q4T_CHECK(output.Accept(Pass(Prompt(32), {{1, "Hello"}, {2, " tail"}}), false,
                          kDiscard));
  auto prompt = Prompt(output.Remaining());
  prompt.tokens.resize(100, 100);
  const auto before = prompt.tokens;
  AsrTranscript temporary(false, "en");
  Q4T_CHECK(!output.Prepare(&prompt, 128, &temporary));
  Q4T_CHECK(prompt.tokens == before && prompt.continuation_tokens == 0 &&
            temporary.Text().empty());
  for (int fault = 0; fault < 6; ++fault) {
    auto invalid = Prompt(8);
    if (fault == 0) invalid.tokens.clear();
    if (fault == 1) invalid.tokens[0] = C::kVocab;
    if (fault == 2) invalid.audio_start = 4;
    if (fault == 3) invalid.audio_tokens = 4;
    if (fault == 4) invalid.continuation_tokens = 2;
    if (fault == 5) invalid.output_tokens = 4097;
    AsrStreamPass pass(invalid);
    Q4T_CHECK(!pass.Valid() && !pass.Begin(invalid));
  }
  return true;
}
