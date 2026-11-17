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
struct Window {
  double pair_energy = 0;
  double coherence = 0;
  bool both_material = false;
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
  double total = 0, matched = 0, supported = 0, peak = 0;
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
    window.pair_energy = std::sqrt(aa * bb);
    window.coherence = window.pair_energy > 0
                           ? std::min(1., std::abs(ab) / window.pair_energy)
                           : 0;
    window.both_material = std::min(aa, bb) >= std::max(aa, bb) / 16.;
    total += window.pair_energy;
    matched += window.pair_energy * window.coherence;
    if (window.coherence >= kLocalCoherence) supported += window.pair_energy;
    peak = std::max(peak, window.pair_energy);
  }
  SeparationTrackDecision result;
  if (total > 0) {
    result.coherence = matched / total;
    result.coherent_energy_fraction = supported / total;
    // Two consecutive locally independent, materially audible windows veto
    // whole-record collapse, even if the rest is coherent leakage. A single
    // window straddling a sign/gain switch is insufficient. Low energy alone
    // never selects a single source: uncorrelated quiet speech keeps both.
    size_t independent_windows = 0;
    for (size_t i = 0; i < count; ++i) {
      const auto& window = windows[i];
      const bool independent = window.pair_energy >= peak / 100. &&
                               window.both_material &&
                               window.coherence < kLocalCoherence;
      independent_windows = independent ? independent_windows + 1 : 0;
      result.independent_region |= independent_windows >= 2;
    }
    result.use_original = result.coherence >= kMeanCoherence &&
                          result.coherent_energy_fraction >= kRequiredSupport &&
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
