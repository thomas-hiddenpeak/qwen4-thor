#include "q4t/server/diarized_output.h"
#include "q4t/io/json.h"
#include "q4t/test.h"

namespace {
using namespace q4t;
using namespace q4t::server;
audio::AlignerTimes Times(std::vector<int64_t> values) {
  return {values, values, 0, 0};
}
const std::vector<audio::SpeakerTurn> kTurns{
    {0, 16000, "A"}, {16000, 32000, "guest"}, {40000, 64000, "A"}};
}  // namespace

Q4T_TEST(diarized_output_actual_word_times_preserve_cjk_and_punctuation) {
  DiarizedTranscript transcript(64000);
  const std::string text = "你好！ 我好。";
  const std::vector<audio::AlignerWord> words{{"你好", 0, 6}, {"我好", 10, 16}};
  Q4T_CHECK(transcript.Append(text, {0, 32000, false}, words,
                              Times({100, 800, 1200, 1900}), kTurns, 50));
  const auto segments = transcript.Segments();
  Q4T_CHECK(segments.size() == 2);
  Q4T_CHECK(segments[0].text == "你好！ " && segments[1].text == "我好。");
  Q4T_CHECK(segments[0].begin_sample == 1600 &&
            segments[0].end_sample == 12800);
  Q4T_CHECK(segments[1].speaker == "guest" &&
            segments[1].begin_sample == 19200);
  Q4T_CHECK(transcript.Text() == text);
  std::string output;
  Q4T_CHECK(transcript.Finish({60, 5}, false, &output));
  io::Json json;
  Q4T_CHECK(io::ParseJson(output, &json));
  Q4T_CHECK(json.GetString("task") == "transcribe");
  Q4T_CHECK(json.Find("duration")->number == 4);
  Q4T_CHECK(json.GetArray("segments")->array[1].GetString("speaker") ==
            "guest");
  return true;
}

Q4T_TEST(diarized_output_cross_clip_ids_sse_and_actual_usage) {
  DiarizedTranscript transcript(64000);
  const std::vector<audio::AlignerWord> words{{"one", 0, 3}};
  Q4T_CHECK(transcript.Append("one.", {0, 16000, false}, words,
                              Times({80, 900}), kTurns, 10));
  Q4T_CHECK(transcript.Append("two?", {40000, 64000, false},
                              std::vector<audio::AlignerWord>{{"two", 0, 3}},
                              Times({160, 1400}), kTurns, 15));
  Q4T_CHECK(transcript.Text() == "one. two?");
  Q4T_CHECK(transcript.Segments()[1].id == "1");
  Q4T_CHECK(transcript.Segments()[1].begin_sample == 42560);
  std::string final, event;
  Q4T_CHECK(transcript.Finish({30, 4}, true, &final));
  Q4T_CHECK(DiarizedTranscript::SegmentJson(transcript.Segments()[1], &event));
  io::Json json, segment;
  Q4T_CHECK(io::ParseJson(final, &json) && io::ParseJson(event, &segment));
  Q4T_CHECK(json.GetString("type") == "transcript.text.done");
  Q4T_CHECK(segment.GetString("type") == "transcript.text.segment");
  Q4T_CHECK(segment.GetString("id") == "1");
  Q4T_CHECK(segment.Find("start")->number == 2.66);
  const auto* usage = json.Find("usage");
  Q4T_CHECK(usage->Find("input_tokens")->number == 30);
  Q4T_CHECK(usage->Find("total_tokens")->number == 34);
  Q4T_CHECK(usage->Find("input_token_details")->Find("audio_tokens")->number ==
            25);
  Q4T_CHECK(usage->Find("input_token_details")->Find("text_tokens")->number ==
            5);
  return true;
}

Q4T_TEST(diarized_output_invalid_coverage_alignment_and_sticky_failure) {
  const std::vector<audio::AlignerWord> words{{"word", 0, 4}};
  for (int failure = 0; failure < 5; ++failure) {
    DiarizedTranscript transcript(64000);
    auto times = Times({100, 900});
    auto turns = kTurns;
    auto source = words;
    if (failure == 0) turns.clear();
    if (failure == 1) times.fixed_ms = {900, 100};
    if (failure == 2) times.raw_ms.clear();
    if (failure == 3) source[0].source_end = 6;
    if (failure == 4) turns[1].begin_sample = 15000;
    Q4T_CHECK(
        !transcript.Append("word", {0, 16000, false}, source, times, turns, 1));
    Q4T_CHECK(transcript.Segments().empty() && transcript.Text().empty());
    Q4T_CHECK(!transcript.Append("word", {0, 16000, false}, words,
                                 Times({100, 900}), kTurns, 1));
    std::string out = "retained";
    Q4T_CHECK(!transcript.Finish({2, 2}, false, &out) && out == "retained");
  }
  return true;
}

Q4T_TEST(diarized_output_silence_zero_time_and_cancel) {
  DiarizedTranscript silence(64000);
  std::string output;
  Q4T_CHECK(silence.Finish({}, false, &output));
  io::Json json;
  Q4T_CHECK(io::ParseJson(output, &json));
  Q4T_CHECK(json.GetString("text").empty() &&
            json.GetArray("segments")->array.empty());
  DiarizedTranscript transcript(64000);
  const std::vector<audio::AlignerWord> words{{"ok", 0, 2}};
  Q4T_CHECK(transcript.Append("ok", {0, 32000, false}, words,
                              Times({1000, 1000}), kTurns, 1));
  Q4T_CHECK(transcript.Segments()[0].speaker == "guest");
  Q4T_CHECK(transcript.Segments()[0].begin_sample ==
            transcript.Segments()[0].end_sample);
  for (int stop : {1, 4, 6}) {
    DiarizedTranscript cancelled(64000);
    int calls = 0;
    Q4T_CHECK(!cancelled.Append("ok", {0, 32000, false}, words,
                                Times({1000, 1000}), kTurns, 1,
                                [&] { return ++calls == stop; }));
    Q4T_CHECK(cancelled.Segments().empty());
    Q4T_CHECK(!cancelled.Finish({2, 2}, false, &output));
  }
  return true;
}

Q4T_TEST(diarized_output_segment_and_usage_budgets_keep_output) {
  DiarizedTranscript transcript(64000);
  Q4T_CHECK(transcript.Append("...", {0, 16000, false}, {}, {}, kTurns, 10));
  Q4T_CHECK(transcript.Segments().size() == 1);
  std::string out = "retained";
  Q4T_CHECK(!transcript.Finish({9, 1}, false, &out) && out == "retained");
  Q4T_CHECK(!transcript.Finish({kActualUsageLimit, 1}, false, &out));
  auto segment = transcript.Segments()[0];
  segment.id = "bad\nframe";
  Q4T_CHECK(!DiarizedTranscript::SegmentJson(segment, &out) &&
            out == "retained");
  segment = transcript.Segments()[0];
  segment.speaker = std::string(1, '\xff');
  Q4T_CHECK(!DiarizedTranscript::SegmentJson(segment, &out));
  segment = transcript.Segments()[0];
  segment.end_sample = audio::kVadMaxSamples + 1;
  Q4T_CHECK(!DiarizedTranscript::SegmentJson(segment, &out));
  Q4T_CHECK(out == "retained");
  Q4T_CHECK(!DiarizedTranscript(0).Finish({}, false, &out));
  DiarizedTranscript bounded(128 * 16000);
  const std::string punctuation(64 * 1024, '.');
  size_t accepted = 0;
  for (size_t i = 0; i < 128; ++i) {
    const audio::VadChunk chunk{i * 16000, (i + 1) * 16000, false};
    const std::vector<audio::SpeakerTurn> turns{
        {chunk.begin_sample, chunk.end_sample, "voice"}};
    const auto status = bounded.Append(punctuation, chunk, {}, {}, turns, 0);
    if (!status) break;
    ++accepted;
  }
  Q4T_CHECK(accepted > 0 && accepted < 64);
  Q4T_CHECK(bounded.Segments().size() == accepted);
  Q4T_CHECK(!bounded.Finish({}, false, &out) && out == "retained");
  return true;
}
