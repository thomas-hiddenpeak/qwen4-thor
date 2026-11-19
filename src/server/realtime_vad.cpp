#include "q4t/server/realtime_vad.h"

#include <algorithm>
#include <cmath>

#include "q4t/server/realtime_pcm.h"

namespace q4t::server {
RealtimeVad::RealtimeVad(RealtimeAudioFormat format, uint64_t begin_ticks,
                         RealtimeVadOptions options, Step step)
    : format_(format),
      origin_(begin_ticks),
      options_(options),
      step_(std::move(step)),
      resampler_(format.rate, 3600ULL * format.rate) {
  valid_ = RealtimeAudioQueue(1, format).Valid() && resampler_.Valid() &&
           origin_ <= kRealtimeSessionTicks && step_ &&
           std::isfinite(options.threshold) && options.threshold >= 0 &&
           options.threshold <= 1 && options.prefix_ms <= 30000 &&
           options.silence_ms <= 30000;
}
uint64_t RealtimeVad::CursorTicks() const {
  return valid_ ? origin_ + resampler_.InputSamples() *
                                (kRealtimeTicksPerSecond / format_.rate)
                : origin_;
}
Status RealtimeVad::Advance(const RealtimeAudioQueue& queue,
                            RealtimeVadProgress* out) {
  if (!valid_ || failed_ || !out || !queue.Valid() || queue.Cancelled() ||
      queue.Format() != format_ || CursorTicks() < queue.PendingBeginTicks() ||
      CursorTicks() > queue.AcceptedTicks())
    return Status::Fail("realtime VAD lifecycle or sample cursor differs");
  *out = {};
  const auto bytes = queue.PendingFrom(CursorTicks());
  const bool f32 = format_.encoding == RealtimeAudioEncoding::kFloat32;
  const size_t width = f32                                                 ? 4
                       : format_.encoding == RealtimeAudioEncoding::kPcm16 ? 2
                                                                           : 1;
  const auto count =
      std::min<size_t>(1024, (bytes[0].size() + bytes[1].size()) / width);
  const auto byte = [&](size_t at) {
    return at < bytes[0].size() ? bytes[0][at] : bytes[1][at - bytes[0].size()];
  };
  std::array<float, 1024> input;
  for (size_t i = 0; i < count; ++i)
    input[i] = DecodeRealtimeSample(
        format_.encoding, byte(i * width), width > 1 ? byte(i * width + 1) : 0,
        f32 ? byte(i * width + 2) : 0, f32 ? byte(i * width + 3) : 0);
  audio::ResampleProgress progress;
  auto status =
      resampler_.Process(std::span(input).first(count),
                         std::span(frame_).subspan(filled_), &progress);
  if (!status) {
    failed_ = true;
    return status;
  }
  filled_ += progress.produced;
  if (filled_ != frame_.size()) return {};
  float probability = 0;
  status = step_(frame_, &state_, &probability);
  if (!status || !std::isfinite(probability) || probability < 0 ||
      probability > 1) {
    failed_ = true;
    return status ? Status::Fail("realtime VAD probability invalid") : status;
  }
  const uint64_t begin = origin_ + frames_ * frame_.size() * 3;
  const uint64_t end = begin + frame_.size() * 3;
  const auto sample_step = kRealtimeTicksPerSecond / format_.rate;
  const auto floor_sample = [&](uint64_t ticks) {
    return origin_ + (ticks - origin_) / sample_step * sample_step;
  };
  const auto prefix = uint64_t(options_.prefix_ms) * 48;
  const auto silence = uint64_t(options_.silence_ms) * 48;
  filled_ = 0;
  ++frames_;
  out->frame = true;
  if (probability >= options_.threshold) {
    silent_ = false;
    if (!active_) {
      speech_begin_ =
          std::max(queue.PendingBeginTicks(),
                   floor_sample(begin - std::min(begin - origin_, prefix)));
      active_ = true;
      out->started = true;
    }
  } else if (active_) {
    if (!silent_) {
      silence_begin_ = begin;
      silent_ = true;
    }
    if (end - silence_begin_ >= silence) {
      const auto exact = silence_begin_ + silence;
      out->end_ticks = origin_ + (exact - origin_ + sample_step - 1) /
                                     sample_step * sample_step;
      out->stopped = true;
      active_ = silent_ = false;
    }
  }
  out->begin_ticks = speech_begin_;
  out->trim_before =
      active_ ? speech_begin_
      : out->stopped
          ? out->end_ticks
          : std::max(queue.PendingBeginTicks(),
                     floor_sample(end - std::min(end - origin_, prefix)));
  return {};
}
}  // namespace q4t::server
