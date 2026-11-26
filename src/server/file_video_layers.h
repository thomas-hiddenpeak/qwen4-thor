#pragma once

#include <algorithm>
#include <cstdint>
#include <optional>
#include <span>
#include <vector>

#include "file_video_protocol.h"

namespace q4t::server::video_detail {

struct LayerFrameTime {
  uint64_t start = 0, duration = 0;
};

struct LayerTimeline {
  uint64_t duration = 0;
  std::vector<LayerFrameTime> frames;

  // An unspecified duration holds until the next frame or the end of the
  // track. Explicit durations can leave a gap before the next frame.
  uint64_t End(size_t index) const {
    const auto& frame = frames[index];
    const auto next =
        index + 1 < frames.size() ? frames[index + 1].start : duration;
    return frame.duration ? std::min(frame.start + frame.duration, next) : next;
  }
};

struct MovieSample {
  uint64_t start, duration;
  size_t index;
  std::vector<std::optional<size_t>> layer_frames;
};

struct MovieSchedule {
  uint64_t duration = 0;
  size_t presentation_frames = 0;
  std::vector<MovieSample> samples;
};

inline Failure ScheduleMovie(std::span<const LayerTimeline> layers,
                             const FileVideoLimits& limits,
                             MovieSchedule* result) {
  if (!result || layers.empty() || limits.sample_frames < 2)
    return Failure::kInvalid;
  MovieSchedule next;
  std::vector<uint64_t> times{0};
  size_t scanned = 0;
  for (const auto& layer : layers) {
    if (!layer.duration || layer.frames.empty()) return Failure::kInvalid;
    if (layer.duration > limits.max_duration_ns ||
        layer.frames.size() > limits.max_scanned_frames - scanned)
      return Failure::kCapacity;
    scanned += layer.frames.size();
    next.duration = std::max(next.duration, layer.duration);
    for (size_t i = 0; i < layer.frames.size(); ++i) {
      const auto& frame = layer.frames[i];
      if (frame.start >= layer.duration ||
          frame.duration > layer.duration - frame.start ||
          (i && frame.start <= layer.frames[i - 1].start))
        return Failure::kInvalid;
      times.push_back(frame.start);
      times.push_back(layer.End(i));
    }
  }
  times.push_back(next.duration);
  std::sort(times.begin(), times.end());
  times.erase(std::unique(times.begin(), times.end()), times.end());
  next.presentation_frames = times.size() - 1;
  if (next.presentation_frames < 2) return Failure::kInvalid;
  if (next.presentation_frames > limits.max_scanned_frames)
    return Failure::kCapacity;
  for (size_t i = 0; i < next.presentation_frames; ++i) {
    const auto time = times[i];
    const auto selected = next.samples.size();
    const auto threshold = static_cast<unsigned __int128>(next.duration) *
                           selected / (limits.sample_frames - 1);
    if (i && i + 1 != next.presentation_frames &&
        (selected + 1 >= limits.sample_frames || time < threshold))
      continue;
    MovieSample sample{time, times[i + 1] - time, i, {}};
    sample.layer_frames.reserve(layers.size());
    for (const auto& layer : layers) {
      const auto after = std::upper_bound(
          layer.frames.begin(), layer.frames.end(), time,
          [](uint64_t t, const LayerFrameTime& f) { return t < f.start; });
      std::optional<size_t> active;
      if (after != layer.frames.begin()) {
        const auto frame = size_t(after - layer.frames.begin() - 1);
        if (time < layer.End(frame)) active = frame;
      }
      sample.layer_frames.push_back(active);
    }
    next.samples.push_back(std::move(sample));
  }
  *result = std::move(next);
  return Failure::kNone;
}
}  // namespace q4t::server::video_detail
