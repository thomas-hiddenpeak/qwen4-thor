#include "q4t/server/overlapping_output.h"

#include <algorithm>
#include <tuple>

#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
constexpr size_t kByteLimit = 4ULL << 20;
constexpr size_t kSegmentLimit = 65536;
Json Seconds(size_t samples) {
  Json result;
  result.type = Json::Type::kNumber;
  result.number = double(samples) / 16000;
  return result;
}
}  // namespace

OverlappingTranscript::OverlappingTranscript(size_t total_samples)
    : valid_(total_samples > 0 && total_samples <= audio::kVadMaxSamples),
      total_samples_(total_samples) {}

Status OverlappingTranscript::Append(size_t track, std::string_view text,
                                     const audio::VadChunk& chunk,
                                     std::span<const audio::AlignerWord> words,
                                     const audio::AlignerTimes& times,
                                     std::span<const audio::SpeakerTurn> turns,
                                     uint64_t audio_tokens,
                                     const std::function<bool()>& cancelled) {
  const bool prior = valid_;
  valid_ = false;
  if (!prior || track >= previous_end_.size() || clips_ >= 256 ||
      chunk.begin_sample < previous_end_[track] ||
      chunk.begin_sample < watermark_ ||
      audio_tokens > kActualUsageLimit - audio_tokens_)
    return Status::Fail("overlapping output owner or clip ordering invalid");
  // Reuse exact word/source/acoustic-coverage validation without imposing
  // another track's prior end on this independent waveform.
  DiarizedTranscript clip(total_samples_);
  auto status =
      clip.Append(text, chunk, words, times, turns, audio_tokens, cancelled);
  if (!status) return status;
  if (clip.Segments().size() >
      kSegmentLimit - committed_.size() - pending_.size())
    return Status::Fail("overlapping segment capacity exhausted");
  size_t bytes = serialized_bytes_;
  for (const auto& segment : clip.Segments()) {
    if (cancelled && cancelled())
      return Status::Fail("overlapping output cancelled");
    std::string encoded;
    status = DiarizedTranscript::SegmentJson(segment, &encoded);
    if (!status) return status;
    // Include final text, segment text, separators and longer reassigned IDs.
    bytes += encoded.size() + detail::JsonEscape(segment.text).size() + 32;
    if (bytes > kByteLimit)
      return Status::Fail("overlapping serialized byte capacity exhausted");
  }
  if (cancelled && cancelled())
    return Status::Fail("overlapping output cancelled");
  for (const auto& segment : clip.Segments()) pending_.push_back(segment);
  previous_end_[track] = chunk.end_sample;
  ++clips_;
  audio_tokens_ += audio_tokens;
  serialized_bytes_ = bytes;
  valid_ = true;
  return {};
}

Status OverlappingTranscript::DrainBefore(
    size_t next_begin, const std::function<bool()>& cancelled) {
  const bool prior = valid_;
  valid_ = false;
  if (!prior || next_begin < watermark_ || next_begin > total_samples_ + 1)
    return Status::Fail("overlapping output watermark invalid");
  if (cancelled && cancelled())
    return Status::Fail("overlapping output cancelled");
  std::stable_sort(pending_.begin(), pending_.end(),
                   [](const auto& a, const auto& b) {
                     return std::tie(a.begin_sample, a.end_sample) <
                            std::tie(b.begin_sample, b.end_sample);
                   });
  size_t count = 0;
  while (count < pending_.size() && pending_[count].begin_sample < next_begin) {
    if (cancelled && cancelled())
      return Status::Fail("overlapping output cancelled");
    auto segment = pending_[count];
    segment.id = std::to_string(committed_.size());
    if (!text_.empty()) text_ += ' ';
    text_ += segment.text;
    committed_.push_back(std::move(segment));
    ++count;
  }
  pending_.erase(pending_.begin(), pending_.begin() + count);
  if (cancelled && cancelled())
    return Status::Fail("overlapping output cancelled");
  watermark_ = next_begin;
  valid_ = true;
  return {};
}

Status OverlappingTranscript::Finish(const ActualUsage& usage, bool stream,
                                     std::string* out) const {
  if (!out || !valid_ || !pending_.empty() ||
      watermark_ != total_samples_ + 1 || !ValidActualUsage(usage) ||
      audio_tokens_ > usage.input)
    return Status::Fail("overlapping output incomplete or usage invalid");
  auto segments = Array();
  for (const auto& segment : committed_) {
    std::string encoded;
    auto status = DiarizedTranscript::SegmentJson(segment, &encoded);
    if (!status) return status;
    Json value;
    status =
        io::ParseJson(encoded, &value, false, {.reject_duplicate_keys = true});
    if (!status) return status;
    segments.array.push_back(std::move(value));
  }
  auto result = Object(
      {{"duration", Seconds(total_samples_)},
       {"task", String("transcribe")},
       {"text", String(text_)},
       {"segments", std::move(segments)},
       {"usage", Object({{"type", String("tokens")},
                         {"input_tokens", Number(usage.input)},
                         {"output_tokens", Number(usage.output)},
                         {"total_tokens", Number(usage.input + usage.output)},
                         {"input_token_details",
                          Object({{"audio_tokens", Number(audio_tokens_)},
                                  {"text_tokens",
                                   Number(usage.input - audio_tokens_)}})}})}});
  if (stream) Set(&result, "type", String("transcript.text.done"));
  auto encoded = OutputJson(result);
  if (encoded.size() > kByteLimit)
    return Status::Fail("overlapping final output byte capacity exhausted");
  *out = std::move(encoded);
  return {};
}
}  // namespace q4t::server
