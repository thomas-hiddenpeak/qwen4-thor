#include "q4t/server/realtime_audio_export.h"

#include <algorithm>
#include <array>
#include <cmath>

#include "q4t/audio/stream_resample.h"

namespace q4t::server {
uint8_t EncodeRealtimeG711(int16_t sample, bool mu_law) {
  // Arithmetic shifts retain the quantization of negative linear samples.
  int value = int(sample) >> (mu_law ? 2 : 3);
  const bool negative = value < 0;
  if (negative) value = -value - int(!mu_law);
  if (mu_law) value = std::min(value, 8159) + 33;
  const int mask = mu_law ? (negative ? 127 : 255)
                          : (negative ? 85 : 213);
  int segment = 0;
  for (int end = mu_law ? 63 : 31; value > end && segment < 8;
       end = end * 2 + 1)
    ++segment;
  if (segment == 8) return uint8_t(127 ^ mask);
  const int shift = mu_law ? segment + 1 : std::max(segment, 1);
  return uint8_t(((segment << 4) | ((value >> shift) & 15)) ^ mask);
}

Status EncodeRealtimeAudio(std::span<const float> samples,
                            RealtimeAudioFormat format, size_t max_bytes,
                            std::string* output,
                            const std::function<bool()>& cancelled) {
  const bool pcm = format.encoding == RealtimeAudioEncoding::kPcm16;
  const bool mu = format.encoding == RealtimeAudioEncoding::kMuLaw;
  const bool alaw = format.encoding == RealtimeAudioEncoding::kALaw;
  if (!output || samples.empty() || samples.size() > 600ULL * 16000 ||
      !(pcm ? format.rate == 24000 : (mu || alaw) && format.rate == 8000))
    return Status::Fail("Invalid processed realtime audio extent or format");
  const size_t count = uint64_t(samples.size()) * format.rate / 16000;
  const size_t width = pcm ? 2 : 1;
  if (!count || count > max_bytes / width)
    return Status::Fail("Processed realtime audio exceeds its output budget");
  if (cancelled && cancelled())
    return Status::Fail("Processed realtime audio export cancelled");
  std::string bytes;
  bytes.reserve(count * width);
  audio::AsrStreamResampler resampler(16000, samples.size(), format.rate,
                                     false);
  std::array<float, 1024> buffer;
  const auto append = [&](size_t produced) -> Status {
    if (produced > (count * width - bytes.size()) / width)
      return Status::Fail("Processed audio output accounting failed");
    for (const float value : std::span(buffer).first(produced)) {
      if (!std::isfinite(value))
        return Status::Fail("Processed realtime audio is not finite");
      const auto linear = int16_t(std::clamp<long>(
          std::lround(double(std::clamp(value, -1.f, 1.f)) * 32768),
          -32768, 32767));
      if (pcm) {
        const auto word = uint16_t(linear);
        bytes.push_back(char(word & 255));
        bytes.push_back(char(word >> 8));
      } else
        bytes.push_back(char(EncodeRealtimeG711(linear, mu)));
    }
    return {};
  };
  size_t position = 0;
  while (position < samples.size()) {
    if (cancelled && cancelled())
      return Status::Fail("Processed realtime audio export cancelled");
    const auto part = samples.subspan(
        position, std::min<size_t>(1024, samples.size() - position));
    audio::ResampleProgress progress;
    auto status = resampler.Process(part, buffer, &progress);
    if (!status) return status;
    if (!progress.consumed && !progress.produced)
      return Status::Fail("Processed realtime audio export made no progress");
    position += progress.consumed;
    status = append(progress.produced);
    if (!status) return status;
  }
  do {
    if (cancelled && cancelled())
      return Status::Fail("Processed realtime audio export cancelled");
    size_t produced;
    auto status = resampler.Finish(buffer, &produced);
    if (!status) return status;
    status = append(produced);
    if (!status) return status;
  } while (!resampler.Drained());
  if (bytes.size() != count * width || resampler.OutputSamples() != count ||
      (cancelled && cancelled()))
    return Status::Fail("Processed realtime audio export incomplete");
  *output = std::move(bytes);
  return {};
}
}  // namespace q4t::server
