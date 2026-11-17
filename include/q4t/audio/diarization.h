// Request-local voice clustering. No identity store or transcript heuristics.
#pragma once

#include <array>

#include "q4t/audio/vad_chunks.h"

namespace q4t::audio {
using SpeakerEmbedding = std::array<float, 192>;
struct SpeakerWindow {
  size_t begin_sample = 0, end_sample = 0;
  size_t core_begin_sample = 0, core_end_sample = 0;
  bool operator==(const SpeakerWindow&) const = default;
};
struct SpeakerWindowOptions {
  size_t window_samples = 48000, step_samples = 12000;
  size_t max_windows = 256;
};
// Windows overlap; cores partition only observed VAD regions. A short region
// stays intact. The executor may pad it to the model's five-frame minimum,
// but must never include that padding in source timestamps.
Status PlanSpeakerWindows(std::span<const VadChunk> chunks,
                          size_t total_samples,
                          const SpeakerWindowOptions& options,
                          std::vector<SpeakerWindow>* out,
                          const std::function<bool()>& cancelled = {});

struct SpeakerReference {
  std::string name;
  SpeakerEmbedding embedding{};
};
struct SpeakerClusteringOptions {
  double merge_cosine = .65;
  double reference_cosine = .65;
  double reference_margin = .05;
  size_t max_windows = 256, max_speakers = 16;
};
struct SpeakerTurn {
  size_t begin_sample = 0, end_sample = 0;
  std::string speaker;
  bool operator==(const SpeakerTurn&) const = default;
};
// Average linkage over actual normalized embeddings; deterministic earliest
// pair on ties. O(N^2) bounded storage, at most N-1 merges with cancellation
// per row. Capacity is an error, never a forced merge of unrelated voices.
// Reference ambiguity fails rather than silently choosing a supplied name.
// Output is atomic. These are single-track turns, not overlap separation.
Status ClusterSpeakerWindows(std::span<const SpeakerWindow> windows,
                             std::span<const SpeakerEmbedding> embeddings,
                             std::span<const SpeakerReference> references,
                             const SpeakerClusteringOptions& options,
                             std::vector<SpeakerTurn>* out,
                             const std::function<bool()>& cancelled = {});
}  // namespace q4t::audio
