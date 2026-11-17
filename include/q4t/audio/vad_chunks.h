#pragma once

#include "q4t/audio/vad.h"

namespace q4t::audio {
struct VadChunkOptions {
  double threshold = .5;
  int prefix_padding_ms = 300;
  int silence_duration_ms = 200;
  size_t max_samples = kVadMaxSamples;
  size_t max_chunk_samples = 30ULL * 16000;
  size_t max_chunks = 64;
};
struct VadChunk {
  size_t begin_sample = 0, end_sample = 0;
  // A hard resource boundary, distinct from an observed silence boundary.
  bool forced_split = false;
  bool operator==(const VadChunk&) const = default;
};

// Hysteresis uses the actual posterior (enter >= threshold, leave below
// max(threshold-.15,.01)). Short pauses remain inside a region. Prefix
// padding is bounded by the file and prior region; no sample is duplicated.
// No minimum-speech filter discards short words. Long speech is bounded at
// actual sample positions and explicitly marked. Failure is atomic.
Status SplitVadChunks(std::span<const VadFrame> frames, size_t total_samples,
                      const VadChunkOptions& options,
                      std::vector<VadChunk>* out,
                      const std::function<bool()>& cancelled = {});
}  // namespace q4t::audio
