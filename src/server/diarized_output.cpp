#include "q4t/server/diarized_output.h"

#include <algorithm>

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
bool LabelValid(std::string_view label) {
  return !label.empty() && label.size() <= 256 &&
         label.find('\0') == label.npos &&
         detail::NormalizeUtf8(label) == label;
}
Json SegmentObject(const DiarizedSegment& segment) {
  return Object({{"id", String(segment.id)},
                 {"type", String("transcript.text.segment")},
                 {"start", Seconds(segment.begin_sample)},
                 {"end", Seconds(segment.end_sample)},
                 {"speaker", String(segment.speaker)},
                 {"text", String(segment.text)}});
}
}  // namespace

DiarizedTranscript::DiarizedTranscript(size_t total_samples)
    : valid_(total_samples > 0 && total_samples <= audio::kVadMaxSamples),
      total_samples_(total_samples) {}

Status DiarizedTranscript::Append(std::string_view text,
                                  const audio::VadChunk& chunk,
                                  std::span<const audio::AlignerWord> words,
                                  const audio::AlignerTimes& times,
                                  std::span<const audio::SpeakerTurn> turns,
                                  uint64_t audio_tokens,
                                  const std::function<bool()>& cancelled) {
  const bool prior = valid_;
  valid_ = false;
  if (!prior || clips_ >= 256 || text.size() > 64 * 1024 ||
      text.size() + 1 > kByteLimit - text_.size() || words.size() > 2048 ||
      turns.size() > 1024 || chunk.begin_sample < previous_end_ ||
      chunk.end_sample <= chunk.begin_sample ||
      chunk.end_sample > total_samples_ ||
      times.fixed_ms.size() != words.size() * 2 ||
      times.raw_ms.size() != times.fixed_ms.size() ||
      audio_tokens > kActualUsageLimit - audio_tokens_ ||
      detail::NormalizeUtf8(text) != text)
    return Status::Fail("diarized output owner, shape or budget invalid");
  const auto interrupted = [&] { return cancelled && cancelled(); };
  if (interrupted()) return Status::Fail("diarized output cancelled");
  size_t previous = 0;
  for (const auto& turn : turns) {
    if (interrupted()) return Status::Fail("diarized output cancelled");
    if (turn.begin_sample < previous || turn.end_sample <= turn.begin_sample ||
        turn.end_sample > total_samples_ || !LabelValid(turn.speaker))
      return Status::Fail("diarized acoustic turn invalid");
    previous = turn.end_sample;
  }
  const auto speaker_at = [&](size_t begin, size_t end) -> std::string {
    size_t overlap = 0;
    const audio::SpeakerTurn* best = nullptr;
    for (const auto& turn : turns) {
      const auto first = std::max(begin, turn.begin_sample);
      const auto last = std::min(end, turn.end_sample);
      if (last > first && last - first > overlap) {
        overlap = last - first;
        best = &turn;
      }
      // Repaired aligner times may coincide. Use the actual point, retaining
      // the word's zero duration instead of fabricating an artificial span.
      if (begin == end && turn.begin_sample <= begin &&
          (begin < turn.end_sample ||
           (begin == chunk.end_sample && begin == turn.end_sample))) {
        best = &turn;
        break;
      }
    }
    return best ? best->speaker : std::string{};
  };
  std::vector<DiarizedSegment> pending;
  size_t source_end = 0;
  int64_t previous_time = 0;
  const int64_t duration_ms = (chunk.end_sample - chunk.begin_sample) / 16;
  const auto boundary = [&](size_t at) {
    return at == text.size() ||
           (static_cast<unsigned char>(text[at]) & 0xc0) != 0x80;
  };
  for (size_t i = 0; i < words.size(); ++i) {
    if (interrupted()) return Status::Fail("diarized output cancelled");
    const auto& word = words[i];
    if (word.text.empty() || word.source_begin < source_end ||
        word.source_end <= word.source_begin || word.source_end > text.size() ||
        !boundary(word.source_begin) || !boundary(word.source_end) ||
        detail::NormalizeUtf8(word.text) != word.text)
      return Status::Fail("diarized alignment source invalid");
    for (size_t j = i * 2; j < i * 2 + 2; ++j) {
      if (times.fixed_ms[j] < previous_time ||
          times.fixed_ms[j] > duration_ms || times.raw_ms[j] < 0 ||
          times.raw_ms[j] > 399920)
        return Status::Fail("diarized alignment observation invalid");
      previous_time = times.fixed_ms[j];
    }
    source_end = word.source_end;
    const size_t begin = chunk.begin_sample + times.fixed_ms[2 * i] * 16;
    const size_t end = chunk.begin_sample + times.fixed_ms[2 * i + 1] * 16;
    const auto speaker = speaker_at(begin, end);
    if (speaker.empty())
      return Status::Fail("diarized word lacks acoustic coverage");
    const size_t from = i ? word.source_begin : 0;
    const size_t to =
        i + 1 == words.size() ? text.size() : words[i + 1].source_begin;
    if (to < word.source_end || to > text.size() || !boundary(to))
      return Status::Fail("diarized next source boundary invalid");
    if (!pending.empty() && pending.back().speaker == speaker) {
      pending.back().text += text.substr(from, to - from);
      pending.back().end_sample = end;
    } else {
      pending.push_back({std::to_string(segments_.size() + pending.size()),
                         speaker, std::string(text.substr(from, to - from)),
                         begin, end});
    }
  }
  if (words.empty() && !text.empty()) {
    // Punctuation-only output has no word slots. Its label uses the actual
    // clip's dominant acoustic intersection, consistent with its clip span.
    const auto label = speaker_at(chunk.begin_sample, chunk.end_sample);
    if (label.empty())
      return Status::Fail("diarized punctuation lacks acoustic coverage");
    pending.push_back({std::to_string(segments_.size()), label,
                       std::string(text), chunk.begin_sample,
                       chunk.end_sample});
  }
  if (pending.size() > kSegmentLimit - segments_.size())
    return Status::Fail("diarized segment capacity exhausted");
  // Account escaped text and row overhead before retaining another clip.
  // The fixed margin covers task/duration/usage and the terminal event type.
  size_t bytes =
      serialized_bytes_ + detail::JsonEscape(std::string(text)).size() + 1;
  for (const auto& segment : pending) {
    if (interrupted()) return Status::Fail("diarized output cancelled");
    bytes += OutputJson(SegmentObject(segment)).size() + 1;
    if (bytes > kByteLimit)
      return Status::Fail("diarized serialized byte capacity exhausted");
  }
  if (bytes > kByteLimit)
    return Status::Fail("diarized serialized byte capacity exhausted");
  if (interrupted()) return Status::Fail("diarized output cancelled");
  // Allocation failures also leave the final output poisoned; a partial
  // internal append can never be emitted as successful completion.
  for (auto& segment : pending) segments_.push_back(std::move(segment));
  if (!text.empty()) {
    if (!text_.empty()) text_ += ' ';
    text_ += text;
  }
  previous_end_ = chunk.end_sample;
  ++clips_;
  audio_tokens_ += audio_tokens;
  serialized_bytes_ = bytes;
  valid_ = true;
  return {};
}

Status DiarizedTranscript::SegmentJson(const DiarizedSegment& segment,
                                       std::string* out) {
  if (!out || segment.id.empty() || segment.id.size() > 32 ||
      !std::all_of(segment.id.begin(), segment.id.end(),
                   [](unsigned char c) { return c >= '0' && c <= '9'; }) ||
      !LabelValid(segment.speaker) || segment.text.size() > 64 * 1024 ||
      detail::NormalizeUtf8(segment.text) != segment.text ||
      segment.begin_sample > segment.end_sample ||
      segment.end_sample > audio::kVadMaxSamples)
    return Status::Fail("diarized segment contract invalid");
  *out = OutputJson(SegmentObject(segment));
  return {};
}

Status DiarizedTranscript::DeltaJson(const DiarizedSegment& segment,
                                     bool leading_space, std::string* out) {
  std::string checked;
  if (!out) return Status::Fail("diarized delta owner unavailable");
  if (auto status = SegmentJson(segment, &checked); !status) return status;
  *out = OutputJson(
      Object({{"type", String("transcript.text.delta")},
              {"segment_id", String(segment.id)},
              {"delta", String((leading_space ? " " : "") + segment.text)}}));
  return {};
}

Status DiarizedTranscript::Finish(const ActualUsage& usage, bool stream,
                                  std::string* out) const {
  if (!out || !valid_ || !ValidActualUsage(usage) ||
      audio_tokens_ > usage.input)
    return Status::Fail("diarized output incomplete or usage invalid");
  auto segments = Array();
  for (const auto& segment : segments_)
    segments.array.push_back(SegmentObject(segment));
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
  auto rendered = OutputJson(result);
  if (rendered.size() > kByteLimit)
    return Status::Fail("diarized final output byte capacity exhausted");
  *out = std::move(rendered);
  return {};
}
}  // namespace q4t::server
