#pragma once

#include "q4t/audio/aligner_postprocess.h"
#include "q4t/audio/diarization.h"
#include "q4t/server/actual_usage.h"

namespace q4t::server {
struct DiarizedSegment {
  std::string id, speaker, text;
  size_t begin_sample = 0, end_sample = 0;
  bool operator==(const DiarizedSegment&) const = default;
};
// One request owns all segments. Word boundaries are real aligner results;
// speaker labels come exclusively from maximum acoustic interval overlap.
// Failed append poisons final publication. Unknown time coverage is an error.
class DiarizedTranscript {
 public:
  explicit DiarizedTranscript(size_t total_samples);
  Status Append(std::string_view text, const audio::VadChunk& chunk,
                std::span<const audio::AlignerWord> words,
                const audio::AlignerTimes& times,
                std::span<const audio::SpeakerTurn> turns,
                uint64_t audio_tokens,
                const std::function<bool()>& cancelled = {});
  Status Finish(const ActualUsage& usage, bool stream, std::string* out) const;
  const std::string& Text() const { return text_; }
  std::span<const DiarizedSegment> Segments() const { return segments_; }
  static Status SegmentJson(const DiarizedSegment& segment, std::string* out);
  static Status DeltaJson(const DiarizedSegment& segment, bool leading_space,
                          std::string* out);

 private:
  bool valid_;
  size_t total_samples_, previous_end_ = 0, clips_ = 0;
  size_t serialized_bytes_ = 1024;
  uint64_t audio_tokens_ = 0;
  std::string text_;
  std::vector<DiarizedSegment> segments_;
};
}  // namespace q4t::server
