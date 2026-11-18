#include "q4t/server/realtime_pcm.h"

#include <algorithm>
#include <array>

#include "q4t/audio/stream_resample.h"

namespace q4t::server {
namespace {
int DecodeMuLaw(uint8_t value) {
  const unsigned code = value ^ 255;
  const int magnitude = (((code & 15) << 3) + 132) << ((code >> 4) & 7);
  return code & 128 ? 132 - magnitude : magnitude - 132;
}
int DecodeALaw(uint8_t value) {
  const unsigned code = value ^ 85;
  const unsigned exponent = (code >> 4) & 7;
  int magnitude = (code & 15) << 4;
  magnitude += exponent == 0 ? 8 : 264;
  if (exponent > 1) magnitude <<= exponent - 1;
  return code & 128 ? magnitude : -magnitude;
}
}  // namespace

Status DecodeRealtimePcm(const RealtimeAudioItem& item,
                         const std::array<std::span<const uint8_t>, 2>& bytes,
                         size_t max_output_samples, std::vector<float>* output,
                         const std::function<bool()>& cancelled) {
  const auto format = item.format;
  const bool pcm16 = format.encoding == RealtimeAudioEncoding::kPcm16;
  const bool g711 = format.encoding == RealtimeAudioEncoding::kMuLaw ||
                    format.encoding == RealtimeAudioEncoding::kALaw;
  const bool rate = pcm16 ? (format.rate == 8000 || format.rate == 16000 ||
                             format.rate == 24000 || format.rate == 48000)
                          : g711 && format.rate == 8000;
  const size_t sample_bytes = pcm16 ? 2 : 1;
  if (!output || !rate || !item.epoch || !item.sequence || !item.samples ||
      item.bytes > kRealtimeAudioBytes ||
      item.samples > kRealtimeAudioBytes / sample_bytes ||
      item.bytes != item.samples * sample_bytes ||
      bytes[0].size() > item.bytes ||
      bytes[1].size() != item.bytes - bytes[0].size() ||
      item.end_ticks > kRealtimeSessionTicks ||
      item.begin_ticks > item.end_ticks ||
      item.end_ticks - item.begin_ticks !=
          item.samples * (kRealtimeTicksPerSecond / format.rate) ||
      max_output_samples == 0 || max_output_samples > 600ULL * 16000)
    return Status::Fail("Invalid realtime audio ownership or sample extent");
  const auto samples = uint64_t(item.samples) * 16000 / format.rate;
  if (!samples || samples > max_output_samples)
    return Status::Fail("Realtime audio exceeds ASR sample budget");
  if (cancelled && cancelled())
    return Status::Fail("Realtime audio decoding cancelled");
  std::vector<float> decoded(samples);
  audio::AsrStreamResampler resampler(format.rate, item.samples);
  if (!resampler.Valid()) return Status::Fail("Invalid realtime resampler");
  const auto byte = [&](size_t offset) {
    return offset < bytes[0].size() ? bytes[0][offset]
                                    : bytes[1][offset - bytes[0].size()];
  };
  std::array<float, 1024> input{};
  size_t position = 0, produced = 0;
  while (position < item.samples) {
    if (cancelled && cancelled())
      return Status::Fail("Realtime audio decoding cancelled");
    const auto count = std::min(input.size(), item.samples - position);
    for (size_t i = 0; i < count; ++i) {
      int value;
      if (pcm16) {
        const auto offset = (position + i) * 2;
        const unsigned word = byte(offset) | (unsigned(byte(offset + 1)) << 8);
        value = word >= 32768 ? int(word) - 65536 : int(word);
      } else {
        const auto code = byte(position + i);
        value = format.encoding == RealtimeAudioEncoding::kMuLaw
                    ? DecodeMuLaw(code)
                    : DecodeALaw(code);
      }
      input[i] = float(value) / 32768.0f;
    }
    audio::ResampleProgress progress;
    auto status =
        resampler.Process(std::span(input).first(count),
                          std::span(decoded).subspan(produced), &progress);
    if (!status) return status;
    if (progress.consumed != count)
      return Status::Fail("Realtime resampler output accounting failed");
    position += progress.consumed;
    produced += progress.produced;
  }
  size_t tail = 0;
  auto status = resampler.Finish(std::span(decoded).subspan(produced), &tail);
  if (!status) return status;
  if (!resampler.Drained() || produced + tail != samples ||
      resampler.InputSamples() != item.samples)
    return Status::Fail("Realtime audio did not drain its exact sample extent");
  if (cancelled && cancelled())
    return Status::Fail("Realtime audio decoding cancelled");
  *output = std::move(decoded);
  return {};
}
}  // namespace q4t::server
