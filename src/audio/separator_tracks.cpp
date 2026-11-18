#include "q4t/audio/separator_tracks.h"

#include <algorithm>
#include <array>
#include <cmath>

namespace q4t::audio {
namespace {
constexpr size_t kWindow = 2000;  // 250 ms at the separator's 8 kHz rate.
constexpr size_t kHop = 1000;
constexpr size_t kMaxSamples = 240000;
constexpr double kLocalCoherence = .3;
constexpr double kMeanCoherence = .5;
constexpr double kRequiredSupport = .9;
// Twenty dB below each source's own local peak energy, independent of the
// separator's arbitrary per-source output gain. No absolute silence floor.
constexpr double kActiveEnergyFraction = .01;
struct Window {
  double first_energy = 0, second_energy = 0;
  double pair_energy = 0;
  double coherence = 0;
};
}  // namespace

Status InspectSeparationTracks(std::span<const float> first,
                               std::span<const float> second,
                               SeparationTrackDecision* decision,
                               const std::function<bool()>& cancelled) {
  if (!decision || first.empty() || first.size() != second.size() ||
      first.size() > kMaxSamples)
    return Status::Fail("separation track shape invalid");
  std::array<Window, kMaxSamples / kHop> windows;
  size_t count = 0;
  double total = 0, matched = 0, supported = 0;
  double first_peak = 0, second_peak = 0;
  for (size_t begin = 0; begin < first.size(); begin += kHop) {
    if (cancelled && cancelled())
      return Status::Fail("separation track inspection cancelled");
    const size_t end = std::min(first.size(), begin + kWindow);
    double mean_a = 0, mean_b = 0;
    for (size_t i = begin; i < end; ++i) {
      if (!std::isfinite(first[i]) || !std::isfinite(second[i]))
        return Status::Fail("separation track input nonfinite");
      mean_a += first[i];
      mean_b += second[i];
    }
    mean_a /= end - begin;
    mean_b /= end - begin;
    double aa = 0, bb = 0, ab = 0;
    for (size_t i = begin; i < end; ++i) {
      const double a = first[i] - mean_a, b = second[i] - mean_b;
      aa += a * a;
      bb += b * b;
      ab += a * b;
    }
    // Geometric energy weights avoid a loud source masking coherent leakage.
    // An absolute energy cutoff would erase quiet independent speech.
    auto& window = windows[count++];
    window.first_energy = aa;
    window.second_energy = bb;
    first_peak = std::max(first_peak, aa);
    second_peak = std::max(second_peak, bb);
    window.pair_energy = std::sqrt(aa * bb);
    window.coherence = window.pair_energy > 0
                           ? std::min(1., std::abs(ab) / window.pair_energy)
                           : 0;
    total += window.pair_energy;
    matched += window.pair_energy * window.coherence;
    if (window.coherence >= kLocalCoherence) supported += window.pair_energy;
  }
  SeparationTrackDecision result;
  if (total > 0) {
    result.coherence = matched / total;
    result.coherent_energy_fraction = supported / total;
    // A separator may place successive people on different output tracks.
    // Quiet, uncorrelated residuals on the idle track must not by themselves
    // establish a simultaneous person. Require sustained exclusive activity
    // on both tracks and veto it on two independent co-active windows. One
    // boundary window cannot establish overlapping speech. Each
    // activity floor is relative to that same track, retaining quiet sources
    // even when its entire waveform is independently scaled down.
    size_t first_alone = 0, second_alone = 0, independent = 0;
    bool first_turn = false, second_turn = false;
    for (size_t i = 0; i < count; ++i) {
      const auto& window = windows[i];
      const bool first = first_peak > 0 &&
                         window.first_energy >=
                             first_peak * kActiveEnergyFraction;
      const bool second = second_peak > 0 &&
                          window.second_energy >=
                              second_peak * kActiveEnergyFraction;
      first_alone = first && !second ? first_alone + 1 : 0;
      second_alone = second && !first ? second_alone + 1 : 0;
      first_turn |= first_alone >= 2;
      second_turn |= second_alone >= 2;
      independent = first && second && window.coherence < kLocalCoherence
                        ? independent + 1
                        : 0;
      result.independent_region |= independent >= 2;
    }
    result.alternating_sources = first_turn && second_turn &&
                                 !result.independent_region;
    result.use_original =
        ((result.coherence >= kMeanCoherence &&
          result.coherent_energy_fraction >= kRequiredSupport) ||
         result.alternating_sources) &&
        !result.independent_region;
  } else {
    // Empty/constant outputs cannot justify two voices. The original input
    // still goes through real VAD/ASR; no text or silence is fabricated here.
    result.use_original = true;
  }
  if (cancelled && cancelled())
    return Status::Fail("separation track inspection cancelled");
  *decision = result;
  return {};
}
}  // namespace q4t::audio
