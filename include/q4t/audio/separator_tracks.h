// Acoustic duplicate suppression for fixed two-source separator outputs.
#pragma once

#include <cstddef>
#include <functional>
#include <span>

#include "q4t/status.h"

namespace q4t::audio {
struct SeparationTrackDecision {
  // A confident duplicate or an empty source uses the original full-band
  // input once. Otherwise retain both sources, including a quiet speaker.
  bool use_original = false;
  double coherence = 0;
  double coherent_energy_fraction = 0;
  bool independent_region = false;
};

// 8 kHz, at most 30 seconds. This is a waveform heuristic, not a speaker
// identity estimator. It never inspects text or treats an output index as a
// person. Phase/sign, global gain and DC offset do not establish a new source.
// The decision is atomic on invalid/nonfinite input or cancellation.
Status InspectSeparationTracks(std::span<const float> first,
                               std::span<const float> second,
                               SeparationTrackDecision* decision,
                               const std::function<bool()>& cancelled = {});
}  // namespace q4t::audio
