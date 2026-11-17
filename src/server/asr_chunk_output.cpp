#include "q4t/server/asr_chunk_output.h"

#include <algorithm>
#include <cmath>

#include "q4t/server/output_schema.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
constexpr size_t kByteLimit = 4ULL << 20;
Json Real(double value) {
  Json result;
  result.type = Json::Type::kNumber;
  result.number = value;
  return result;
}
}  // namespace

AsrChunkOutput::AsrChunkOutput(const AsrRequest& request, size_t total_samples)
    : format_(request.format),
      stream_(request.stream),
      logprobs_(request.logprobs),
      word_timestamps_(request.word_timestamps),
      segment_timestamps_(request.segment_timestamps),
      total_samples_(total_samples),
      forced_language_(request.language),
      words_(Array()),
      segments_(Array()),
      probabilities_(Array()) {
  valid_ = total_samples > 0 && total_samples <= audio::kVadMaxSamples;
}

Status AsrChunkOutput::Append(const AsrTranscript& transcript,
                              std::string formatted,
                              const audio::VadChunk& chunk,
                              uint64_t audio_tokens, size_t subtitle_cues) {
  const bool was_valid = valid_;
  valid_ = false;
  if (!was_valid || !transcript.Complete() || clips_ >= 256 ||
      chunk.begin_sample < previous_end_ ||
      chunk.end_sample <= chunk.begin_sample ||
      chunk.end_sample > total_samples_ ||
      formatted.size() > kByteLimit - bytes_ ||
      transcript.Text().size() + 1 > kByteLimit - text_.size() ||
      audio_tokens > kActualUsageLimit - audio_tokens_ ||
      subtitle_cues > 65536 - cues_)
    return Status::Fail("ASR chunk output budget or owner invalid");
  const bool subtitle =
      format_ == AsrFormat::kSrt || format_ == AsrFormat::kVtt;
  const auto duration_ms = (chunk.end_sample - chunk.begin_sample) / 16;
  const auto origin_ms = chunk.begin_sample / 16;
  Json parsed, words = Array(), segments = Array(), probabilities = Array();
  if (!subtitle) {
    const auto status = io::ParseJson(formatted, &parsed, false,
                                      {.max_depth = 12,
                                       .max_values = 131072,
                                       .max_string_bytes = kByteLimit,
                                       .reject_duplicate_keys = true});
    if (!status || parsed.GetString("text") != transcript.Text())
      return Status::Fail("ASR chunk formatted text differs");
    if (format_ == AsrFormat::kVerboseJson) {
      const auto shift = [&](const char* key, bool required, Json* target) {
        const auto* rows = parsed.GetArray(key);
        if (!rows) return !required;
        *target = *rows;
        for (auto& row : target->array) {
          const auto* start = row.Find("start");
          const auto* end = row.Find("end");
          if (!row.IsObject() || !start || !end || !start->IsNumber() ||
              !end->IsNumber() || !std::isfinite(start->number) ||
              !std::isfinite(end->number) || start->number < 0 ||
              end->number < start->number || end->number > duration_ms / 1000.)
            return false;
          const auto first = std::llround(start->number * 1000);
          const auto last = std::llround(end->number * 1000);
          Set(&row, "start", Real((origin_ms + first) / 1000.));
          Set(&row, "end", Real((origin_ms + last) / 1000.));
        }
        return true;
      };
      if (!shift("words", word_timestamps_, &words) ||
          !shift("segments", segment_timestamps_, &segments))
        return Status::Fail("ASR chunk time observations invalid");
      for (size_t i = 0; i < segments.array.size(); ++i) {
        Set(&segments.array[i], "id", Number(segments_.array.size() + i));
        // seek denotes the source's 10ms feature-frame index. start/end keep
        // the additional real millisecond offset of non-frame-aligned padding.
        Set(&segments.array[i], "seek", Number(chunk.begin_sample / 160));
      }
    } else if (logprobs_) {
      const auto* rows = parsed.GetArray("logprobs");
      if (!rows) return Status::Fail("ASR chunk actual logprobs absent");
      probabilities = *rows;
    }
  }
  const auto& text = transcript.Text();
  if (!text.empty()) {
    if (!text_.empty()) text_ += ' ';
    text_ += text;
  }
  if (!transcript.Language().empty()) {
    auto it = std::find_if(
        languages_.begin(), languages_.end(),
        [&](const auto& item) { return item.first == transcript.Language(); });
    const auto samples = chunk.end_sample - chunk.begin_sample;
    if (it == languages_.end())
      languages_.emplace_back(transcript.Language(), samples);
    else
      it->second += samples;
  }
  if (subtitle) subtitles_ += formatted;
  for (auto& row : words.array) words_.array.push_back(std::move(row));
  for (auto& row : segments.array) segments_.array.push_back(std::move(row));
  for (auto& row : probabilities.array)
    probabilities_.array.push_back(std::move(row));
  audio_tokens_ += audio_tokens;
  previous_end_ = chunk.end_sample;
  ++clips_;
  cues_ += subtitle_cues;
  bytes_ += formatted.size();
  valid_ = true;
  return {};
}

Status AsrChunkOutput::Finish(const ActualUsage& usage,
                              std::string* out) const {
  if (!out || !valid_ || !ValidActualUsage(usage) ||
      audio_tokens_ > usage.input)
    return Status::Fail("Incomplete ASR chunk output or usage");
  if (format_ == AsrFormat::kSrt || format_ == AsrFormat::kVtt) {
    *out = format_ == AsrFormat::kVtt && !clips_ ? "WEBVTT\n\n" : subtitles_;
    return {};
  }
  if (format_ == AsrFormat::kText && !stream_) {
    *out = text_;
    return {};
  }
  Json result = Object({{"text", String(text_)}});
  if (format_ == AsrFormat::kVerboseJson) {
    std::string language = forced_language_;
    if (!languages_.empty()) {
      const auto best = std::max_element(
          languages_.begin(), languages_.end(),
          [](const auto& a, const auto& b) { return a.second < b.second; });
      language = best->first;
    }
    Set(&result, "language", String(std::string(AsrLanguageName(language))));
    Set(&result, "duration", Real((total_samples_ / 16) / 1000.));
    if (word_timestamps_) Set(&result, "words", words_);
    if (segment_timestamps_) Set(&result, "segments", segments_);
    Set(&result, "usage",
        Object({{"type", String("duration")},
                {"seconds", Real((total_samples_ / 16) / 1000.)}}));
  } else {
    if (stream_) Set(&result, "type", String("transcript.text.done"));
    if (!languages_.empty()) {
      auto values = Array();
      for (const auto& [language, samples] : languages_) {
        (void)samples;
        values.array.push_back(Object({{"code", String(language)}}));
      }
      Set(&result, "languages", std::move(values));
    }
    if (logprobs_) Set(&result, "logprobs", probabilities_);
    Set(&result, "usage",
        Object({{"type", String("tokens")},
                {"input_tokens", Number(usage.input)},
                {"output_tokens", Number(usage.output)},
                {"total_tokens", Number(usage.input + usage.output)},
                {"input_token_details",
                 Object({{"audio_tokens", Number(audio_tokens_)},
                         {"text_tokens",
                          Number(usage.input - audio_tokens_)}})}}));
  }
  auto rendered = OutputJson(result);
  if (rendered.size() > kByteLimit)
    return Status::Fail("ASR final output byte budget exceeded");
  *out = std::move(rendered);
  return {};
}
}  // namespace q4t::server
