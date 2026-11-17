#include "q4t/server/asr_chunk_output.h"

#include <stdexcept>

#include "q4t/server/asr_alignment_output.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/server_options.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
AsrTranscript Transcript(std::string_view text, std::string language = "en",
                         bool logprobs = false) {
  AsrTranscript result(logprobs, std::move(language), true);
  AsrTextDelta delta;
  if (!result.Append({8, -99, {}, -.25}, text, &delta, .4) ||
      !result.SetNoSpeechProbability(.01) || !result.Finish(&delta))
    throw std::runtime_error("invalid transcript fixture");
  return result;
}
std::string Formatted(const AsrTranscript& transcript, bool segments = true) {
  const auto& text = transcript.Text();
  const std::vector<q4t::audio::AlignerWord> words{{text, 0, text.size()}};
  const q4t::audio::AlignerTimes times{{80, 800}, {80, 800}, 0, 0};
  std::string out;
  const auto status =
      segments
          ? AlignedAsrSegmentsJson(
                transcript, 1000, words, times,
                {text.size(), text.size() + 8, "fixture", std::string(64, 'a')},
                true, &out)
          : AlignedAsrWordsJson(text, AsrLanguageName(transcript.Language()),
                                1000, words, times, &out);
  if (!status) throw std::runtime_error("invalid aligned fixture");
  return out;
}
std::string Plain(const AsrTranscript& transcript) {
  std::string out;
  if (!transcript.ResultJson({50, 1}, 13, false, &out))
    throw std::runtime_error("invalid plain fixture");
  return out;
}
Json Parse(const std::string& source) {
  Json result;
  if (!q4t::io::ParseJson(source, &result))
    throw std::runtime_error("invalid JSON fixture");
  return result;
}
}  // namespace

Q4T_TEST(asr_chunks_global_times_ids_and_seek_keep_actual_clip_statistics) {
  AsrRequest request;
  request.format = AsrFormat::kVerboseJson;
  request.word_timestamps = request.segment_timestamps = true;
  AsrChunkOutput result(request, 64000);
  const auto one = Transcript("Hello"), two = Transcript("你好", "zh");
  Q4T_CHECK(result.Append(one, Formatted(one), {4000, 20000, false}, 13));
  Q4T_CHECK(result.Append(two, Formatted(two), {32016, 48016, false}, 13));
  std::string out;
  Q4T_CHECK(result.Finish({120, 2}, &out));
  const auto json = Parse(out);
  Q4T_CHECK(json.GetString("text") == "Hello 你好");
  Q4T_CHECK(json.Find("duration")->number == 4 &&
            json.Find("usage")->Find("seconds")->number == 4);
  const auto& rows = json.GetArray("segments")->array;
  Q4T_CHECK(rows.size() == 2 && rows[0].Find("id")->number == 0 &&
            rows[1].Find("id")->number == 1);
  Q4T_CHECK(rows[0].Find("start")->number == .330 &&
            rows[1].Find("start")->number == 2.081 &&
            rows[1].Find("end")->number == 2.801);
  Q4T_CHECK(rows[0].Find("seek")->number == 25 &&
            rows[1].Find("seek")->number == 200);
  Q4T_CHECK(rows[1].GetString("text") == "你好" &&
            rows[1].GetArray("tokens")->array[0].number == 8 &&
            rows[1].Find("avg_logprob")->number == -.25 &&
            rows[1].Find("temperature")->number == .4 &&
            rows[1].Find("no_speech_prob")->number == .01 &&
            rows[1].Find("compression_ratio")->number == 6. / 14);
  Q4T_CHECK(json.GetArray("words")->array[1].Find("start")->number == 2.081);
  return true;
}

Q4T_TEST(asr_chunks_word_only_keeps_selection_and_duration_weighted_language) {
  AsrRequest request;
  request.format = AsrFormat::kVerboseJson;
  request.word_timestamps = true;
  AsrChunkOutput result(request, 64000);
  const auto one = Transcript("Hello"), two = Transcript("你好", "zh");
  Q4T_CHECK(result.Append(one, Formatted(one, false), {0, 16000, false}, 13));
  Q4T_CHECK(
      result.Append(two, Formatted(two, false), {16000, 48000, false}, 26));
  std::string out;
  Q4T_CHECK(result.Finish({120, 2}, &out));
  const auto json = Parse(out);
  Q4T_CHECK(!json.Find("segments") &&
            json.GetArray("words")->array.size() == 2);
  Q4T_CHECK(json.GetString("language") == "Chinese");
  return true;
}

Q4T_TEST(asr_chunks_json_stream_and_text_preserve_real_usage_and_logprobs) {
  for (bool stream : {false, true}) {
    AsrRequest request;
    request.stream = stream;
    request.logprobs = true;
    AsrChunkOutput result(request, 32000);
    const auto one = Transcript("Hello", "en", true);
    const auto two = Transcript("你好", "zh", true);
    Q4T_CHECK(result.Append(one, Plain(one), {0, 16000, false}, 13));
    Q4T_CHECK(result.Append(two, Plain(two), {16000, 32000, false}, 13));
    std::string out;
    Q4T_CHECK(result.Finish({110, 4}, &out));
    const auto json = Parse(out);
    Q4T_CHECK(json.GetString("text") == "Hello 你好");
    Q4T_CHECK((json.Find("type") != nullptr) == stream);
    if (stream) Q4T_CHECK(json.GetString("type") == "transcript.text.done");
    Q4T_CHECK(json.GetArray("languages")->array.size() == 2);
    const auto& probabilities = json.GetArray("logprobs")->array;
    Q4T_CHECK(probabilities.size() == 2 &&
              probabilities[0].GetString("token") == "Hello" &&
              probabilities[1].GetString("token") == "你好" &&
              probabilities[1].Find("logprob")->number == -.25);
    const auto* usage = json.Find("usage");
    Q4T_CHECK(
        usage->Find("total_tokens")->number == 114 &&
        usage->Find("input_token_details")->Find("audio_tokens")->number ==
            26 &&
        usage->Find("input_token_details")->Find("text_tokens")->number == 84);
  }
  AsrRequest request;
  request.format = AsrFormat::kText;
  AsrChunkOutput result(request, 32000);
  const auto one = Transcript("Hello"), two = Transcript("你好", "zh");
  Q4T_CHECK(result.Append(one, Plain(one), {0, 16000, false}, 13));
  Q4T_CHECK(result.Append(two, Plain(two), {16000, 32000, false}, 13));
  std::string out;
  Q4T_CHECK(result.Finish({110, 4}, &out) && out == "Hello 你好");
  return true;
}

Q4T_TEST(asr_chunks_vad_silence_has_no_invented_words_tokens_or_time_cues) {
  for (auto format : {AsrFormat::kJson, AsrFormat::kVerboseJson,
                      AsrFormat::kText, AsrFormat::kSrt, AsrFormat::kVtt}) {
    AsrRequest request;
    request.format = format;
    request.word_timestamps = request.segment_timestamps = true;
    AsrChunkOutput result(request, 16384);
    std::string out;
    Q4T_CHECK(result.Finish({}, &out));
    if (format == AsrFormat::kVtt)
      Q4T_CHECK(out == "WEBVTT\n\n");
    else if (format == AsrFormat::kSrt || format == AsrFormat::kText)
      Q4T_CHECK(out.empty());
    else {
      const auto json = Parse(out);
      Q4T_CHECK(json.GetString("text").empty());
      if (format == AsrFormat::kVerboseJson) {
        Q4T_CHECK(json.GetArray("words")->array.empty() &&
                  json.GetArray("segments")->array.empty() &&
                  json.Find("duration")->number == 1.024);
      } else
        Q4T_CHECK(json.Find("usage")->Find("total_tokens")->number == 0);
    }
  }
  return true;
}

Q4T_TEST(asr_chunks_failed_append_cannot_publish_prior_partial_result) {
  for (int fault = 0; fault < 6; ++fault) {
    AsrRequest request;
    request.format = AsrFormat::kVerboseJson;
    request.word_timestamps = true;
    AsrChunkOutput result(request, 32000);
    auto transcript = Transcript("Hello");
    Q4T_CHECK(result.Append(transcript, Formatted(transcript, false),
                            {0, 16000, false}, 13));
    auto part = Formatted(transcript, false);
    q4t::audio::VadChunk chunk{16000, 32000, false};
    if (fault == 0) --chunk.begin_sample;
    if (fault == 1) ++chunk.end_sample;
    if (fault == 2) part.replace(part.find("Hello"), 5, "other");
    if (fault == 3) part = R"({"text":"Hello","words":[{"start":0,"end":2}]})";
    if (fault == 4) part = R"({"text":"Hello"})";
    if (fault == 5) {
      AsrTextDelta delta;
      Q4T_CHECK(!transcript.Finish(&delta));
    }
    Q4T_CHECK(!result.Append(transcript, part, chunk, 13));
    std::string out = "preserved";
    Q4T_CHECK(!result.Finish({100, 2}, &out) && out == "preserved");
  }
  return true;
}

Q4T_TEST(asr_chunks_output_bounds_and_unknown_usage_preserve_destination) {
  AsrRequest request;
  const auto transcript = Transcript("Hello");
  AsrChunkOutput result(request, 16000);
  Q4T_CHECK(
      result.Append(transcript, Plain(transcript), {0, 16000, false}, 13));
  std::string out = "preserved";
  Q4T_CHECK(!result.Finish({12, 1}, &out));
  Q4T_CHECK(!result.Finish({kActualUsageLimit, 1}, &out));
  Q4T_CHECK(out == "preserved");
  AsrChunkOutput oversized(request, 16000);
  Q4T_CHECK(!oversized.Append(transcript, std::string((4ULL << 20) + 1, 'x'),
                              {0, 16000, false}, 13));
  Q4T_CHECK(!oversized.Finish({50, 1}, &out) && out == "preserved");
  return true;
}

Q4T_TEST(asr_chunks_subtitles_keep_global_cues_offsets_and_one_vtt_header) {
  const auto transcript = Transcript("Hello");
  const std::vector<q4t::audio::AlignerWord> words{{"Hello", 0, 5}};
  const q4t::audio::AlignerTimes times{{80, 800}, {80, 800}, 0, 0};
  AsrRequest request;
  request.format = AsrFormat::kVtt;
  AsrChunkOutput result(request, 64000);
  for (size_t i = 0; i < 2; ++i) {
    std::string part;
    size_t cues = 99;
    Q4T_CHECK(AlignedAsrSubtitle("Hello", 1000, words, times, true, &part,
                                 i * 2000, result.CueCount() + 1, !i, &cues));
    Q4T_CHECK(cues == 1);
    Q4T_CHECK(result.Append(transcript, part,
                            {i * 32000, i * 32000 + 16000, false}, 13, cues));
  }
  std::string out;
  Q4T_CHECK(result.Finish({100, 2}, &out));
  Q4T_CHECK(out ==
            "WEBVTT\n\n1\n00:00:00.080 --> 00:00:00.800\nHello\n\n"
            "2\n00:00:02.080 --> 00:00:02.800\nHello\n\n");
  out = "preserved";
  size_t cues = 99;
  Q4T_CHECK(!AlignedAsrSubtitle("Hello", 1000, words, times, true, &out, 599001,
                                1, true, &cues));
  Q4T_CHECK(out == "preserved" && cues == 99);
  Q4T_CHECK(!AlignedAsrSubtitle("Hello", 1000, words, times, true, &out, 0, 0,
                                true, &cues));
  return true;
}

Q4T_TEST(serve_vad_options_require_asr_and_bounded_absolute_assets) {
  ServerOptions options;
  options.model_dir = "/readonly-model";
  const auto parse = [&](std::initializer_list<std::string_view> args) {
    return ParseServerOptions({args.begin(), args.size()}, &options).ok();
  };
  Q4T_CHECK(!parse({"--vad-model-dir", "/optional/vad"}));
  Q4T_CHECK(options.vad_model_dir.empty());
  Q4T_CHECK(parse({"--asr-model-dir", "/optional/asr", "--vad-model-dir",
                   "/optional/vad", "--vad-max-chunks", "2"}));
  for (auto invalid : {"relative", "/", "/a/", "/a//b", "/a/../b"})
    Q4T_CHECK(!parse({"--vad-model-dir", invalid}));
  for (auto invalid : {"0", "257", "2.5", "-1", "1x"})
    Q4T_CHECK(!parse({"--vad-max-chunks", invalid}));
  Q4T_CHECK(!parse({"--vad-model-dir"}));
  Q4T_CHECK(options.vad_model_dir == "/optional/vad" &&
            options.vad_max_chunks == 2);
  return true;
}

Q4T_TEST(serve_vad_capability_reports_cpu_optional_backend_and_chunk_bound) {
  for (bool enabled : {false, true}) {
    const auto json = Parse(ChatCapabilitiesJson(
        "chat", false, true, 0, nullptr, false, true, true, enabled, 2));
    const auto* asr = json.Find("transcriptions");
    const auto* vad = asr ? asr->Find("vad") : nullptr;
    Q4T_CHECK(vad && vad->GetBool("configured", !enabled) == enabled);
    Q4T_CHECK(vad->GetString("backend") == "native_silero_cpu" &&
              vad->Find("max_chunks")->number == 2 &&
              !vad->GetBool("core_readiness_dependency", true));
    Q4T_CHECK(json.GetBool("implementation_only", false) &&
              !json.GetBool("release_attestation", true));
  }
  return true;
}
