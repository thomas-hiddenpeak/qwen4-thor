#include "q4t/audio/vad_chunks.h"

#include <algorithm>
#include <cmath>
#include <optional>

namespace q4t::audio {
Status SplitVadChunks(std::span<const VadFrame> frames, size_t total_samples,
                      const VadChunkOptions& options,
                      std::vector<VadChunk>* out,
                      const std::function<bool()>& cancelled) {
  if (!out || !total_samples || total_samples > options.max_samples ||
      options.max_samples > kVadMaxSamples || !options.max_chunk_samples ||
      options.max_chunk_samples > options.max_samples || !options.max_chunks ||
      options.max_chunks > 256 || !std::isfinite(options.threshold) ||
      options.threshold < 0 || options.threshold > 1 ||
      options.prefix_padding_ms < 0 || options.prefix_padding_ms > 30000 ||
      options.silence_duration_ms < 0 || options.silence_duration_ms > 30000 ||
      frames.size() !=
          (total_samples + kVadFrameSamples - 1) / kVadFrameSamples)
    return Status::Fail("VAD chunk options or audio budget invalid");
  if (cancelled && cancelled()) return Status::Fail("VAD chunking cancelled");
  const auto padding = size_t(options.prefix_padding_ms) * 16;
  const auto silence = size_t(options.silence_duration_ms) * 16;
  const auto negative = std::max(options.threshold - .15, .01);
  std::vector<VadChunk> chunks;
  std::optional<size_t> start, quiet;
  size_t previous = 0;
  const auto publish = [&](size_t begin, size_t end) -> Status {
    begin -= std::min(begin, padding);
    if (!chunks.empty()) begin = std::max(begin, chunks.back().end_sample);
    while (begin < end) {
      if (cancelled && cancelled())
        return Status::Fail("VAD chunking cancelled");
      if (chunks.size() == options.max_chunks)
        return Status::Fail("VAD chunk count exceeded");
      const auto next =
          begin + std::min(end - begin, options.max_chunk_samples);
      chunks.push_back({begin, next, next < end});
      begin = next;
    }
    return {};
  };
  for (const auto& frame : frames) {
    if (cancelled && cancelled()) return Status::Fail("VAD chunking cancelled");
    if (frame.begin_sample != previous ||
        frame.end_sample !=
            std::min(previous + kVadFrameSamples, total_samples) ||
        !std::isfinite(frame.speech_probability) ||
        frame.speech_probability < 0 || frame.speech_probability > 1)
      return Status::Fail("VAD frame observations invalid");
    previous = frame.end_sample;
    if (frame.speech_probability >= options.threshold) {
      if (!start) start = frame.begin_sample;
      quiet.reset();
    } else if (start && frame.speech_probability < negative) {
      if (!quiet) quiet = frame.begin_sample;
      if (frame.end_sample - *quiet >= silence) {
        if (auto s = publish(*start, *quiet); !s) return s;
        start.reset();
        quiet.reset();
      }
    }
  }
  // A trailing pause shorter than silence_duration remains part of this
  // clip; the caller may align its exact spoken words after transcription.
  if (start)
    if (auto s = publish(*start, total_samples); !s) return s;
  if (cancelled && cancelled()) return Status::Fail("VAD chunking cancelled");
  *out = std::move(chunks);
  return {};
}
}  // namespace q4t::audio
