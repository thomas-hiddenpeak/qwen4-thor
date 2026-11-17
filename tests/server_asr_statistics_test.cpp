#include "q4t/server/asr_statistics.h"

#include <cmath>
#include <limits>

#include "q4t/server/asr_request.h"
#include "q4t/server/asr_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
SampledToken Token(int id = 8, double probability = -.25) {
  return {id, -9999, {}, probability};
}
q4t::Status Encode(std::string_view text, bool special,
                   std::vector<uint32_t>* ids) {
  if (!special && text == "language")
    *ids = {11528};
  else if (!special && text == " None")
    *ids = {2240};
  else
    *ids = {12};
  return {};
}
GrammarControl Control() {
  GrammarControl c;
  c.counters = std::make_shared<GrammarWorkerCounters>();
  return c;
}
bool Clean(const GrammarControl& c) {
  return c.counters->active.load() == 0 &&
         c.counters->started.load() == c.counters->reaped.load() &&
         !detail::IsolatedCleanupPending(IsolatedWorkerDomain::kFileParse);
}
}  // namespace

Q4T_TEST(asr_statistics_keep_selected_content_tokens_and_exclude_header) {
  AsrTranscript t(false, {}, true);
  AsrTextDelta d;
  Q4T_CHECK(t.Append(Token(7, -1), "language English<asr_text>", &d, .2));
  Q4T_CHECK(t.Append(Token(8, -.5), "Hello", &d, .4));
  Q4T_CHECK(t.Append(Token(9, -.75), " world", &d, .6));
  Q4T_CHECK(t.SetNoSpeechProbability(.0123));
  Q4T_CHECK(t.Finish(&d));
  const auto rows = t.TextTokens();
  Q4T_CHECK(t.Text() == "Hello world" && rows.size() == 2);
  Q4T_CHECK(rows[0].id == 8 && rows[0].logprob == -.5 &&
            rows[0].temperature == .4 && rows[0].begin == 0 &&
            rows[0].end == 5);
  Q4T_CHECK(rows[1].id == 9 && rows[1].logprob == -.75 &&
            rows[1].temperature == .6 && rows[1].begin == 5 &&
            rows[1].end == 11);
  Q4T_CHECK(t.NoSpeechProbability() == .0123);
  return true;
}

Q4T_TEST(asr_statistics_follow_repaired_utf8_and_delayed_whitespace_once) {
  AsrTranscript t(false, "zh", true);
  AsrTextDelta d;
  Q4T_CHECK(t.Append(Token(8), " \xe4", &d, .2));
  Q4T_CHECK(t.Append(Token(9), "\xb8\xad ", &d, .4));
  Q4T_CHECK(t.Append(Token(10), "word", &d, .4));
  Q4T_CHECK(t.Finish(&d));
  const auto rows = t.TextTokens();
  Q4T_CHECK(t.Text() == "中 word" && rows.size() == 3);
  Q4T_CHECK(rows[0].begin == 0 && rows[0].end == 3);
  Q4T_CHECK(rows[1].begin == 0 && rows[1].end == 4);
  Q4T_CHECK(rows[2].begin == 4 && rows[2].end == 8);
  Q4T_CHECK(rows[0].temperature == .2 && rows[1].temperature == .4);
  return true;
}

Q4T_TEST(asr_statistics_no_speech_requires_real_single_finite_observation) {
  for (double value : {-.1, 1.1, std::numeric_limits<double>::quiet_NaN()}) {
    AsrTranscript t(false, "en", true);
    Q4T_CHECK(!t.SetNoSpeechProbability(value) && !t.NoSpeechProbability());
  }
  AsrTranscript t(false, "en", true);
  Q4T_CHECK(t.SetNoSpeechProbability(.7));
  Q4T_CHECK(!t.SetNoSpeechProbability(.1) && t.NoSpeechProbability() == .7);
  AsrTextDelta d;
  Q4T_CHECK(!t.Finish(&d));
  return true;
}

Q4T_TEST(
    asr_statistics_empty_and_disabled_paths_do_not_invent_tokens_or_scores) {
  AsrTranscript empty(false, {}, true), plain(false, "en");
  AsrTextDelta d;
  Q4T_CHECK(empty.Append(Token(), "language None<asr_text>", &d));
  Q4T_CHECK(empty.Finish(&d) && empty.Text().empty());
  Q4T_CHECK(empty.TextTokens().empty() && !empty.NoSpeechProbability());
  Q4T_CHECK(plain.Append(Token(), "Hello", &d));
  Q4T_CHECK(plain.Finish(&d) && plain.TextTokens().empty());
  Q4T_CHECK(!plain.NoSpeechProbability());
  return true;
}

Q4T_TEST(asr_statistics_reject_invalid_token_values_and_bound_retention) {
  AsrTextDelta d;
  for (const auto& token :
       {Token(-1), Token(151936), Token(8, .1),
        Token(8, std::numeric_limits<double>::infinity())}) {
    AsrTranscript t(false, "en", true);
    Q4T_CHECK(!t.Append(token, "x", &d) && t.TextTokens().empty());
  }
  for (const double temp :
       {-1., 1.1, std::numeric_limits<double>::quiet_NaN()}) {
    AsrTranscript t(false, "en", true);
    Q4T_CHECK(!t.Append(Token(), "x", &d, temp));
  }
  AsrTranscript t(false, "en", true);
  for (int i = 0; i < 8192; ++i) Q4T_CHECK(t.Append(Token(), "x", &d));
  Q4T_CHECK(!t.Append(Token(), "x", &d) && t.TextTokens().size() == 8192);
  return true;
}

Q4T_TEST(asr_label_prompt_keeps_forced_language_out_of_silence_classifier) {
  AsrRequest request;
  AsrPrompt ordinary, forced;
  ChatPlanError error;
  Q4T_CHECK(BuildAsrPrompt(request, 13, {}, Encode, &ordinary, &error));
  Q4T_CHECK(ordinary.no_speech_prefix.empty() &&
            ordinary.no_speech_token == -1);
  request.language = "en";
  request.segment_timestamps = true;
  Q4T_CHECK(BuildAsrPrompt(request, 13, {}, Encode, &forced, &error));
  auto prefix = ordinary.tokens;
  prefix.push_back(11528);
  Q4T_CHECK(forced.no_speech_prefix == prefix &&
            forced.no_speech_token == 2240);
  Q4T_CHECK(forced.tokens.back() == 12 &&
            forced.audio_start == ordinary.audio_start);
  return true;
}

Q4T_TEST(
    asr_label_prompt_rejects_changed_tokenizer_and_retains_previous_owner) {
  AsrRequest request;
  request.segment_timestamps = true;
  AsrPrompt out;
  out.tokens = {42};
  ChatPlanError error;
  Q4T_CHECK(!BuildAsrPrompt(
      request, 13, {},
      [](std::string_view text, bool special, std::vector<uint32_t>* ids) {
        auto s = Encode(text, special, ids);
        if (text == " None") *ids = {1, 2240};
        return s;
      },
      &out, &error));
  Q4T_CHECK(out.tokens == std::vector<int32_t>{42} && error.http_status == 500);
  Q4T_CHECK(!BuildAsrPrompt(
      request, 13, {},
      [](std::string_view, bool, std::vector<uint32_t>*) {
        return q4t::Status::Fail("cancelled");
      },
      &out, &error));
  Q4T_CHECK(out.tokens == std::vector<int32_t>{42});
  return true;
}

Q4T_TEST(asr_compression_reports_actual_zlib_size_ratio_and_library_identity) {
  auto control = Control();
  AsrCompression result;
  GrammarFailure failure;
  std::string text;
  for (int i = 0; i < 128; ++i) text += "hello world ";
  Q4T_CHECK(MeasureAsrCompression(text, &result, control, &failure));
  Q4T_CHECK(result.text_bytes == 1536 && result.compressed_bytes == 33 &&
            result.Ratio() == 1536. / 33);
  Q4T_CHECK(!result.library_version.empty() &&
            result.library_sha256.size() == 64);
  Q4T_CHECK(failure == GrammarFailure::kNone && Clean(control));
  return true;
}

Q4T_TEST(asr_compression_empty_input_and_oversize_preserve_measured_owner) {
  AsrCompression result;
  Q4T_CHECK(MeasureAsrCompression({}, &result));
  Q4T_CHECK(result.text_bytes == 0 && result.compressed_bytes == 8);
  const auto previous = result.library_sha256;
  GrammarFailure failure;
  Q4T_CHECK(!MeasureAsrCompression(std::string((4 << 20) + 1, 'x'), &result, {},
                                   &failure));
  Q4T_CHECK(failure == GrammarFailure::kResource &&
            result.compressed_bytes == 8 && result.library_sha256 == previous);
  return true;
}

Q4T_TEST(asr_compression_missing_dependency_is_request_scoped_and_recovers) {
  SetAsrStatisticsWorkerForTesting("--asr-statistics-missing-worker");
  ScopeExit reset([] { SetAsrStatisticsWorkerForTesting({}); });
  auto control = Control();
  AsrCompression result{42, 17, "previous", "previous"};
  GrammarFailure failure;
  Q4T_CHECK(!MeasureAsrCompression("Hello world", &result, control, &failure));
  Q4T_CHECK(failure == GrammarFailure::kDependency && result.text_bytes == 42 &&
            result.library_version == "previous" && Clean(control));
  SetAsrStatisticsWorkerForTesting({});
  Q4T_CHECK(MeasureAsrCompression("Hello world", &result, control));
  Q4T_CHECK(result.text_bytes == 11 && result.compressed_bytes == 19 &&
            Clean(control));
  return true;
}

Q4T_TEST(asr_compression_cancel_timeout_and_reap_keep_result_unpublished) {
  auto control = Control();
  control.cancelled = [] { return true; };
  AsrCompression result{42, 17, "previous", "previous"};
  GrammarFailure failure;
  Q4T_CHECK(!MeasureAsrCompression("hello", &result, control, &failure));
  Q4T_CHECK(failure == GrammarFailure::kCancelled && Clean(control));
  SetAsrStatisticsWorkerForTesting("--grammar-fixture-silent");
  ScopeExit reset([] { SetAsrStatisticsWorkerForTesting({}); });
  control = Control();
  control.compile_deadline = std::chrono::steady_clock::now() + 100ms;
  Q4T_CHECK(!MeasureAsrCompression("hello", &result, control, &failure));
  Q4T_CHECK(failure == GrammarFailure::kTimeout && Clean(control));
  Q4T_CHECK(result.text_bytes == 42 && result.library_version == "previous");
  return true;
}
