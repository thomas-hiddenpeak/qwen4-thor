#include "q4t/server/asr_alignment_output.h"

#include <stdexcept>

#include "q4t/io/json.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::audio::AlignerTimes;
using q4t::audio::AlignerWord;
std::vector<AlignerWord> Words(std::string_view source,
                               std::initializer_list<std::string> words) {
  std::vector<AlignerWord> result;
  size_t at = 0;
  for (const auto& text : words) {
    at = source.find(text, at);
    if (at == source.npos) throw std::runtime_error("word fixture absent");
    result.push_back({text, at, at + text.size()});
    at += text.size();
  }
  return result;
}
AlignerTimes Times(std::initializer_list<int64_t> times) {
  return {times, times, 0, 0};
}
q4t::io::Json Json(const std::string& raw) {
  q4t::io::Json result;
  if (!q4t::io::ParseJson(raw, &result)) throw std::runtime_error("bad JSON");
  return result;
}
}  // namespace

Q4T_TEST(aligned_asr_words_keep_full_text_and_actual_duration) {
  const std::string source = "你好，Thor！";
  const auto words = Words(source, {"你", "好", "Thor"});
  auto times = Times({80, 160, 240, 400, 560, 800});
  times.raw_ms[1] = 0;  // The serializer uses the explicitly repaired times.
  times.repaired = 1;
  std::string out;
  Q4T_CHECK(AlignedAsrWordsJson(source, "Chinese", 825, words, times, &out));
  const auto json = Json(out);
  Q4T_CHECK(json.GetString("text") == source &&
            json.GetString("language") == "Chinese");
  Q4T_CHECK(json.Find("duration")->number == 0.825);
  const auto* rows = json.GetArray("words");
  Q4T_CHECK(rows && rows->array.size() == 3);
  Q4T_CHECK(rows->array[0].GetString("word") == "你" &&
            rows->array[0].Find("end")->number == 0.160);
  Q4T_CHECK(json.Find("usage")->GetString("type") == "duration" &&
            json.Find("usage")->Find("seconds")->number == 0.825);
  Q4T_CHECK(!json.Find("segments"));  // No fabricated segment confidences.
  return true;
}

Q4T_TEST(aligned_asr_srt_cues_use_word_edges_and_original_punctuation) {
  const std::string source = "Hello world. Next!";
  const auto words = Words(source, {"Hello", "world", "Next"});
  const auto times = Times({80, 240, 320, 640, 800, 960});
  std::string out;
  Q4T_CHECK(AlignedAsrSubtitle(source, 1000, words, times, false, &out));
  Q4T_CHECK(out ==
            "1\n00:00:00,080 --> 00:00:00,640\nHello world. \n\n"
            "2\n00:00:00,800 --> 00:00:00,960\nNext!\n\n");
  return true;
}

Q4T_TEST(aligned_asr_vtt_escapes_markup_and_cue_injection) {
  const std::string source = "<hello> &\n\nworld --> end";
  const auto words = Words(source, {"hello", "world", "end"});
  std::string out;
  Q4T_CHECK(AlignedAsrSubtitle(
      source, 1000, words, Times({0, 160, 240, 400, 480, 800}), true, &out));
  Q4T_CHECK(out ==
            "WEBVTT\n\n1\n00:00:00.000 --> 00:00:00.800\n"
            "&lt;hello&gt; &amp;  world --&gt; end\n\n");
  return true;
}

Q4T_TEST(aligned_asr_subtitle_gaps_split_on_real_next_word) {
  const std::string source = "first second third";
  const auto words = Words(source, {"first", "second", "third"});
  std::string out;
  Q4T_CHECK(AlignedAsrSubtitle(
      source, 64000, words, Times({60800, 61040, 62000, 62480, 62560, 63920}),
      true, &out));
  Q4T_CHECK(out ==
            "WEBVTT\n\n1\n00:01:00.800 --> 00:01:01.040\nfirst \n\n"
            "2\n00:01:02.000 --> 00:01:03.920\nsecond third\n\n");
  return true;
}

Q4T_TEST(aligned_asr_zero_duration_merges_only_existing_word_boundaries) {
  const std::string source = "one. two. three.";
  const auto words = Words(source, {"one", "two", "three"});
  std::string out;
  Q4T_CHECK(AlignedAsrSubtitle(source, 1000, words,
                               Times({0, 0, 0, 320, 320, 320}), false, &out));
  Q4T_CHECK(out == "1\n00:00:00,000 --> 00:00:00,320\none. two. three.\n\n");
  out = "preserved";
  Q4T_CHECK(!AlignedAsrSubtitle(source, 1000, words, Times({0, 0, 0, 0, 0, 0}),
                                false, &out));
  Q4T_CHECK(out == "preserved");
  return true;
}

Q4T_TEST(aligned_asr_output_rejects_bad_source_spans_and_times_atomically) {
  const std::string source = "你好";
  const auto words = Words(source, {"你", "好"});
  const auto times = Times({0, 160, 240, 320});
  std::string out = "preserved";
  auto bad = words;
  bad[1].source_begin = 4;
  Q4T_CHECK(!AlignedAsrWordsJson(source, "Chinese", 1000, bad, times, &out));
  bad = words;
  bad[1].source_end = source.size() + 1;
  Q4T_CHECK(!AlignedAsrWordsJson(source, "Chinese", 1000, bad, times, &out));
  Q4T_CHECK(!AlignedAsrWordsJson("\xff", "", 1000, {}, {}, &out));
  Q4T_CHECK(!AlignedAsrWordsJson(source, "Chinese", 300, words, times, &out));
  auto invalid = times;
  invalid.fixed_ms[2] = 80;
  Q4T_CHECK(
      !AlignedAsrWordsJson(source, "Chinese", 1000, words, invalid, &out));
  invalid = times;
  invalid.raw_ms.clear();
  Q4T_CHECK(
      !AlignedAsrWordsJson(source, "Chinese", 1000, words, invalid, &out));
  Q4T_CHECK(out == "preserved");
  return true;
}

Q4T_TEST(aligned_asr_empty_and_punctuation_have_no_invented_words_or_cues) {
  for (std::string_view text : {"", "..."}) {
    std::string out;
    Q4T_CHECK(AlignedAsrWordsJson(text, "", 1234, {}, {}, &out));
    const auto json = Json(out);
    Q4T_CHECK(json.GetString("text") == text &&
              json.GetArray("words")->array.empty());
    Q4T_CHECK(AlignedAsrSubtitle(text, 1234, {}, {}, true, &out));
    Q4T_CHECK(out == "WEBVTT\n\n");
  }
  return true;
}

Q4T_TEST(aligned_asr_subtitle_wrap_target_never_splits_utf8_or_word) {
  std::string source;
  for (int i = 0; i < 22; ++i) source += "你";
  source += ' ';
  for (int i = 0; i < 22; ++i) source += "好";
  source += " end";
  const auto words =
      Words(source, {source.substr(0, 66), source.substr(67, 66), "end"});
  std::string out;
  Q4T_CHECK(AlignedAsrSubtitle(
      source, 1000, words, Times({0, 160, 240, 320, 400, 560}), false, &out));
  Q4T_CHECK(out.find("00:00:00,000 --> 00:00:00,320") != out.npos);
  Q4T_CHECK(out.find("2\n00:00:00,400 --> 00:00:00,560\nend") != out.npos);
  return true;
}
