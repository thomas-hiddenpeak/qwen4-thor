#include "q4t/audio/aligner_postprocess.h"
#include "q4t/io/json.h"
#include "q4t/test.h"

#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace {
using namespace q4t::audio;
using q4t::io::Json;
Json Golden() {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                     "fixtures/aligner/postprocess-golden.json");
  std::ostringstream text;
  text << file.rdbuf();
  Json value;
  if (!file || !q4t::io::ParseJson(text.str(), &value))
    throw std::runtime_error("alignment processing fixture missing");
  return value;
}
}  // namespace

Q4T_TEST(aligner_words_match_reference_cleaning_and_original_byte_ranges) {
  const auto golden = Golden();
  const auto* cases = golden.GetArray("words");
  Q4T_CHECK(cases && cases->array.size() == 13);
  for (const auto& row : cases->array) {
    std::vector<AlignerWord> words;
    const auto text = row.GetString("text");
    Q4T_CHECK(SplitAlignerWords(text, "en", {}, &words));
    const auto* expected = row.GetArray("words");
    Q4T_CHECK(expected && expected->array.size() == words.size());
    size_t end = 0;
    for (size_t i = 0; i < words.size(); ++i) {
      Q4T_CHECK(words[i].text == expected->array[i].str);
      Q4T_CHECK(words[i].source_begin >= end);
      Q4T_CHECK(words[i].source_begin < words[i].source_end);
      Q4T_CHECK(words[i].source_end <= text.size());
      std::vector<AlignerWord> isolated;
      Q4T_CHECK(SplitAlignerWords(
          text.substr(words[i].source_begin,
                      words[i].source_end - words[i].source_begin),
          "en", {}, &isolated));
      Q4T_CHECK(isolated.size() == 1 && isolated[0].text == words[i].text);
      end = words[i].source_end;
    }
  }
  return true;
}

Q4T_TEST(aligner_native_japanese_korean_words_keep_real_source_spans) {
  for (const auto& [language, text] :
       std::vector<std::pair<std::string, std::string>>{
           {"ja", "日本語の音声を確認します。"},
           {"ko", "한국어 음성을 확인합니다."}}) {
    std::vector<AlignerWord> words;
    Q4T_CHECK(SplitAlignerWords(text, language, {}, &words));
    Q4T_CHECK(words.size() >= 3);
    size_t end = 0;
    std::string combined;
    for (const auto& word : words) {
      Q4T_CHECK(!word.text.empty() && word.source_begin >= end &&
                word.source_end <= text.size());
      Q4T_CHECK(text.substr(word.source_begin,
                            word.source_end - word.source_begin) == word.text);
      end = word.source_end;
      combined += word.text;
    }
    Q4T_CHECK(combined == (language == "ja" ? "日本語の音声を確認します"
                                            : "한국어음성을확인합니다"));
  }
  return true;
}

Q4T_TEST(aligner_words_reject_invalid_utf8_and_budgets_atomically) {
  std::vector<AlignerWord> words{{"preserved", 0, 9}};
  for (const auto& text : {std::string("\xff"), std::string("a\xed\xa0\x80"),
                           std::string("\xf0\x80\x80\x80")}) {
    Q4T_CHECK(!SplitAlignerWords(text, "en", {}, &words));
    Q4T_CHECK(words.size() == 1 && words[0].text == "preserved");
  }
  Q4T_CHECK(!SplitAlignerWords("one two", "en", {.max_bytes = 3}, &words));
  Q4T_CHECK(!SplitAlignerWords("one two", "en", {.max_words = 1}, &words));
  Q4T_CHECK(words[0].text == "preserved");
  Q4T_CHECK(SplitAlignerWords("", "en", {}, &words) && words.empty());
  return true;
}

Q4T_TEST(aligner_words_cancel_without_output_and_clean_injected_markers) {
  std::vector<AlignerWord> words{{"preserved", 0, 9}};
  int calls = 0;
  Q4T_CHECK(!SplitAlignerWords(std::string(1024, 'a'), "en", {}, &words,
                               [&] { return ++calls == 2; }));
  Q4T_CHECK(words[0].text == "preserved");
  Q4T_CHECK(SplitAlignerWords("<timestamp> <|audio_pad|>", "en", {}, &words));
  Q4T_CHECK(words.size() == 2 && words[0].text == "timestamp" &&
            words[1].text == "audiopad");
  return true;
}

Q4T_TEST(aligner_timestamp_repair_matches_reference_anchors_and_anomalies) {
  const auto golden = Golden();
  const auto* cases = golden.GetArray("timestamps");
  Q4T_CHECK(cases && cases->array.size() == 9);
  for (const auto& row : cases->array) {
    std::vector<uint32_t> classes;
    for (const auto& value : row.GetArray("classes")->array)
      classes.push_back(static_cast<uint32_t>(value.AsInt(-1)));
    AlignerTimes times;
    Q4T_CHECK(
        RepairAlignerTimestamps(classes, classes.size() / 2, 30000, &times));
    const auto* expected = row.GetArray("expected_ms");
    Q4T_CHECK(expected && expected->array.size() == times.fixed_ms.size());
    for (size_t i = 0; i < classes.size(); ++i) {
      Q4T_CHECK(times.raw_ms[i] == classes[i] * 80);
      Q4T_CHECK(times.fixed_ms[i] == expected->array[i].AsInt(-1));
    }
    Q4T_CHECK(std::is_sorted(times.fixed_ms.begin(), times.fixed_ms.end()));
  }
  return true;
}

Q4T_TEST(aligner_actual_predictions_keep_raw_values_and_report_repairs) {
  const auto golden = Golden();
  const auto* cases = golden.GetArray("actual");
  Q4T_CHECK(cases && cases->array.size() == 2);
  size_t count = 0;
  for (const auto& row : cases->array) {
    std::vector<uint32_t> classes;
    for (const auto& value : row.GetArray("classes")->array)
      classes.push_back(static_cast<uint32_t>(value.AsInt(-1)));
    count += classes.size();
    AlignerTimes times;
    Q4T_CHECK(
        RepairAlignerTimestamps(classes, classes.size() / 2, 16000, &times));
    size_t changed = 0;
    for (size_t i = 0; i < classes.size(); ++i) {
      Q4T_CHECK(times.raw_ms[i] == classes[i] * 80);
      Q4T_CHECK(times.fixed_ms[i] ==
                row.GetArray("expected_ms")->array[i].AsInt(-1));
      changed += times.fixed_ms[i] != times.raw_ms[i];
    }
    Q4T_CHECK(times.repaired == changed && times.clipped == 0);
  }
  Q4T_CHECK(count == 98);
  return true;
}

Q4T_TEST(aligner_timestamp_range_and_audio_duration_fail_atomically) {
  AlignerTimes result;
  result.raw_ms = {999};
  for (const auto& classes : std::vector<std::vector<uint32_t>>{
           {1}, {1, 5000}, {1, std::numeric_limits<uint32_t>::max()}, {1, 5}}) {
    Q4T_CHECK(!RepairAlignerTimestamps(classes, 1, 319, &result));
    Q4T_CHECK(result.raw_ms == std::vector<int64_t>{999});
  }
  const std::array<uint32_t, 2> classes{1, 4};
  Q4T_CHECK(!RepairAlignerTimestamps(classes, 1, 0, &result));
  Q4T_CHECK(!RepairAlignerTimestamps(classes, 2, 320, &result));
  Q4T_CHECK(RepairAlignerTimestamps(classes, 1, 300, &result));
  Q4T_CHECK(result.raw_ms == std::vector<int64_t>({80, 320}));
  Q4T_CHECK(result.fixed_ms == std::vector<int64_t>({80, 300}));
  Q4T_CHECK(result.clipped == 1 && result.repaired == 0);
  return true;
}

Q4T_TEST(aligner_timestamp_cancel_and_storage_limits_preserve_owner) {
  AlignerTimes result;
  result.raw_ms = {999};
  std::vector<uint32_t> classes(4096, 1);
  int calls = 0;
  Q4T_CHECK(!RepairAlignerTimestamps(classes, 2048, 1000, &result,
                                     [&] { return ++calls == 4; }));
  Q4T_CHECK(result.raw_ms == std::vector<int64_t>{999});
  Q4T_CHECK(!RepairAlignerTimestamps(
      classes, std::numeric_limits<size_t>::max(), 1000, &result));
  Q4T_CHECK(result.raw_ms == std::vector<int64_t>{999});
  return true;
}
