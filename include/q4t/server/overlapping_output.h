// Request-owned merge of independently aligned, potentially overlapping tracks.
#pragma once

#include <array>

#include "q4t/server/diarized_output.h"

namespace q4t::server {
class OverlappingTranscript {
 public:
  explicit OverlappingTranscript(size_t total_samples);
  // Each track's chunks are nonoverlapping. Track labels are NOT speaker IDs;
  // each chunk carries labels from cross-track acoustic clustering.
  Status Append(size_t track, std::string_view text,
                const audio::VadChunk& chunk,
                std::span<const audio::AlignerWord> words,
                const audio::AlignerTimes& times,
                std::span<const audio::SpeakerTurn> turns,
                uint64_t audio_tokens,
                const std::function<bool()>& cancelled = {});
  // The caller supplies the earliest begin among ALL unprocessed track clips.
  // Only segments strictly before that watermark become immutable SSE output.
  // Call with total_samples+1 after all clips. Earlier future clips are errors.
  Status DrainBefore(size_t next_begin,
                     const std::function<bool()>& cancelled = {});
  Status Finish(const ActualUsage& usage, bool stream, std::string* out) const;
  std::span<const DiarizedSegment> Segments() const { return committed_; }
  const std::string& Text() const { return text_; }

 private:
  bool valid_;
  size_t total_samples_, watermark_ = 0, clips_ = 0;
  size_t serialized_bytes_ = 1024;
  std::array<size_t, 2> previous_end_{};
  uint64_t audio_tokens_ = 0;
  std::string text_;
  std::vector<DiarizedSegment> pending_, committed_;
};
}  // namespace q4t::server
