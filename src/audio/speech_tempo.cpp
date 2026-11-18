#include "q4t/audio/speech_tempo.h"

#include <algorithm>
#include <cmath>
#include <numbers>

namespace q4t::audio {
namespace detail {
Status SpeechTempoStage::Init(double speed, size_t max_input_samples,
                              size_t max_output_samples) {
  // Match the maximum complete code-frame budget. Sizes remain exact in
  // binary64, and no index or sample count can overflow size_t.
  constexpr size_t kMaxInput = 4096 * 1920;
  if (initialized_ || !std::isfinite(speed) || speed < 0.25 || speed > 2 ||
      !max_input_samples || max_input_samples > kMaxInput ||
      !max_output_samples || max_output_samples > kMaxInput * 4)
    return Status::Fail("Invalid speech tempo speed or sample budget");
  input_.reserve(kBufferSamples);
  speed_ = speed;
  max_input_ = max_input_samples;
  max_output_ = max_output_samples;
  for (size_t i = 0; i < kHop; ++i)
    fade_[i] = static_cast<float>(
        0.5 - 0.5 * std::cos(std::numbers::pi * i / (kHop - 1)));
  initialized_ = true;
  return {};
}

size_t SpeechTempoStage::OutputCount(size_t input) const {
  return static_cast<size_t>(std::ceil(static_cast<double>(input) / speed_));
}
size_t SpeechTempoStage::Nominal() const {
  return static_cast<size_t>(
      std::floor(static_cast<double>(produced_) * speed_));
}
float SpeechTempoStage::Sample(size_t position) const {
  // Final right extension uses the last real sample. It never reads outside
  // the submitted stream or creates fake silence for failed computation.
  return input_[std::min(position, received_ - 1) - begin_];
}

Status SpeechTempoStage::Append(std::span<const float> samples, bool final) {
  if (!initialized_ || !need_input_ || final_ || done_ ||
      (samples.empty() && !final) || samples.size() > kMaxAppend ||
      samples.size() > max_input_ - received_ ||
      samples.size() > kBufferSamples - input_.size() ||
      (!received_ && samples.empty()) ||
      OutputCount(received_ + samples.size()) > max_output_)
    return Status::Fail("Speech tempo input exceeds state or sample budget");
  for (float value : samples)
    if (!std::isfinite(value) || value < -1 || value > 1)
      return Status::Fail("Speech tempo requires finite normalized PCM");
  input_.insert(input_.end(), samples.begin(), samples.end());
  received_ += samples.size();
  final_ = final;
  need_input_ = false;
  return {};
}

void SpeechTempoStage::Discard() {
  const size_t nominal = speed_ == 1 ? produced_ : Nominal();
  size_t keep =
      speed_ == 1 ? nominal : (nominal > kSearch ? nominal - kSearch : 0);
  if (speed_ != 1 && produced_) keep = std::max(keep, previous_ + 1);
  keep = std::min(keep, received_ - 1);  // Keep the final sample for extension.
  if (keep > begin_) {
    input_.erase(input_.begin(), input_.begin() + (keep - begin_));
    begin_ = keep;
  }
}

Status SpeechTempoStage::Advance(SpeechTempoStep* step,
                                 std::span<const float>* output) {
  if (!initialized_ || !step || !output || done_)
    return Status::Fail("Speech tempo advance out of order");
  *output = {};
  if (final_ && produced_ == OutputCount(received_)) {
    done_ = true;
    *step = SpeechTempoStep::kDone;
    return {};
  }
  const size_t nominal = speed_ == 1 ? produced_ : Nominal();
  const size_t needed = speed_ == 1  ? nominal + kHop
                        : !produced_ ? kWindow
                                     : nominal + kSearch + kWindow;
  if (!final_ && received_ < needed) {
    need_input_ = true;
    *step = SpeechTempoStep::kNeedInput;
    return {};
  }
  need_input_ = false;
  const size_t count =
      final_ ? std::min(kHop, OutputCount(received_) - produced_) : kHop;
  if (speed_ == 1) {
    for (size_t i = 0; i < count; ++i) output_[i] = Sample(produced_ + i);
  } else {
    const size_t lower =
        produced_
            ? std::min(received_ - 1,
                       std::max(previous_ + 1,
                                nominal > kSearch ? nominal - kSearch : 0))
            : 0;
    const size_t upper =
        produced_ ? std::min(nominal + kSearch, received_ - 1) : 0;
    if (lower < begin_ || upper < lower)
      return Status::Fail("Speech tempo retained window invariant failed");
    size_t selected = std::clamp(nominal, lower, upper);
    if (produced_) {
      double previous_energy = 0;
      for (const float value : overlap_)
        previous_energy += double(value) * value;
      double best = -2;
      // A fixed search independent of currently available packet lengths.
      // Prefer the nominal position in equal-energy/flat regions.
      for (size_t candidate = lower; candidate <= upper; ++candidate) {
        double dot = 0, energy = 0;
        for (size_t i = 0; i < kHop; ++i) {
          const double value = Sample(candidate + i);
          dot += value * overlap_[i];
          energy += value * value;
        }
        const double score = energy > 1e-20 && previous_energy > 1e-20
                                 ? dot / std::sqrt(energy * previous_energy)
                                 : 0;
        const size_t distance =
            candidate > nominal ? candidate - nominal : nominal - candidate;
        const size_t old_distance =
            selected > nominal ? selected - nominal : nominal - selected;
        if (score > best + 1e-12 ||
            (std::abs(score - best) <= 1e-12 && distance < old_distance)) {
          best = score;
          selected = candidate;
        }
      }
    }
    for (size_t i = 0; i < count; ++i) {
      const float current = Sample(selected + i);
      output_[i] = produced_ ? overlap_[i] * (1 - fade_[i]) + current * fade_[i]
                             : current;
    }
    for (size_t i = 0; i < kHop; ++i) overlap_[i] = Sample(selected + kHop + i);
    previous_ = selected;
  }
  produced_ += count;
  Discard();
  *output = {output_.data(), count};
  *step = SpeechTempoStep::kOutput;
  return {};
}
}  // namespace detail

Status SpeechTempo::Init(double speed, size_t max_input_samples,
                         size_t max_output_samples) {
  constexpr size_t kMaxInput = 4096 * 1920;
  if (initialized_ || !std::isfinite(speed) || speed < 0.25 || speed > 4 ||
      !max_input_samples || max_input_samples > kMaxInput ||
      !max_output_samples || max_output_samples > kMaxInput * 4)
    return Status::Fail("Invalid speech tempo speed or sample budget");
  if (speed <= 2) {
    if (auto status = first_.Init(speed, max_input_samples, max_output_samples);
        !status)
      return status;
  } else {
    const size_t middle = (max_input_samples + 1) / 2;
    if (auto status = first_.Init(2, max_input_samples, middle); !status)
      return status;
    // The two ceil operations can add one rounding sample. The public
    // output always ends at ceil(original input / requested speed).
    const size_t limit = std::min(max_output_samples, max_input_samples) + 1;
    if (auto status = second_.Init(speed / 2, middle, limit); !status)
      return status;
  }
  speed_ = speed;
  max_output_ = max_output_samples;
  initialized_ = true;
  return {};
}

Status SpeechTempo::Append(std::span<const float> samples, bool final) {
  if (!initialized_ || final_ || done_ || samples.size() > kMaxAppend ||
      std::ceil(double(input_samples() + samples.size()) / speed_) >
          double(max_output_))
    return Status::Fail("Speech tempo input exceeds state or sample budget");
  if (auto status = first_.Append(samples, final); !status) return status;
  final_ = final;
  return {};
}

Status SpeechTempo::Advance(SpeechTempoStep* step,
                            std::span<const float>* output) {
  if (!initialized_ || !step || !output || done_)
    return Status::Fail("Speech tempo advance out of order");
  *output = {};
  const size_t target =
      static_cast<size_t>(std::ceil(double(input_samples()) / speed_));
  if (final_ && produced_ == target) {
    done_ = true;
    *step = SpeechTempoStep::kDone;
    return {};
  }
  if (speed_ <= 2) {
    if (auto status = first_.Advance(step, output); !status) return status;
  } else {
    // Each second-stage hop consumes at most two first-stage grains.
    // Startup/tail may require an additional grain and EOS notification.
    bool ready = false;
    for (int grain = 0; grain < 8; ++grain) {
      if (auto status = second_.Advance(step, output); !status) return status;
      if (*step != SpeechTempoStep::kNeedInput) {
        ready = true;
        break;
      }
      SpeechTempoStep first_step;
      std::span<const float> middle;
      if (auto status = first_.Advance(&first_step, &middle); !status)
        return status;
      if (first_step == SpeechTempoStep::kNeedInput) return {};
      if (first_step == SpeechTempoStep::kDone) {
        if (second_final_)
          return Status::Fail("Speech tempo repeated intermediate EOS");
        if (auto status = second_.Append({}, true); !status) return status;
        second_final_ = true;
      } else {
        if (auto status = second_.Append(middle, false); !status) return status;
      }
    }
    if (!ready) return Status::Fail("Speech tempo grain budget exhausted");
  }
  if (*step == SpeechTempoStep::kOutput) {
    if (final_ && output->size() > target - produced_)
      *output = output->first(target - produced_);
    produced_ += output->size();
  } else if (*step == SpeechTempoStep::kDone) {
    return Status::Fail("Speech tempo ended before requested output length");
  }
  return {};
}
}  // namespace q4t::audio
