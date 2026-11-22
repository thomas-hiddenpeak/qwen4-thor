#include "q4t/audio/pcm_output.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>

namespace q4t::audio {
namespace {
void U16(uint16_t value, std::string* out) {
  out->push_back(static_cast<char>(value & 255));
  out->push_back(static_cast<char>(value >> 8));
}
void U32(uint32_t value, std::string* out) {
  U16(value & 65535, out);
  U16(value >> 16, out);
}
}  // namespace

Status PcmWaveHeader(uint32_t rate, uint16_t channels, size_t frames,
                     size_t max_output_bytes, std::string* out) {
  const uint32_t width = channels * 2u;
  if (!out || !rate || !width || width > UINT16_MAX ||
      rate > UINT32_MAX / width || max_output_bytes < 44 ||
      frames > (max_output_bytes - 44) / width ||
      frames > (std::numeric_limits<uint32_t>::max() - 36) / width)
    return Status::Fail("Speech WAVE length exceeds its byte budget");
  const auto bytes = static_cast<uint32_t>(frames * width);
  std::string result = "RIFF";
  U32(bytes + 36, &result);
  result += "WAVEfmt ";
  U32(16, &result);
  U16(1, &result);
  U16(channels, &result);
  U32(rate, &result);
  U32(rate * width, &result);
  U16(static_cast<uint16_t>(width), &result);
  U16(16, &result);
  result += "data";
  U32(bytes, &result);
  *out = std::move(result);
  return {};
}

Status SpeechWaveHeader(size_t samples, size_t max_output_bytes,
                        std::string* out) {
  if (!samples) return Status::Fail("Empty speech waveform");
  return PcmWaveHeader(24000, 1, samples, max_output_bytes, out);
}

Status EncodeSpeechPcm(std::span<const float> pcm, bool wave,
                       size_t max_output_bytes, std::string* out) {
  const size_t header = wave ? 44 : 0;
  if (!out || pcm.empty() || max_output_bytes < header ||
      pcm.size() > (max_output_bytes - header) / 2 ||
      (wave && pcm.size() > (std::numeric_limits<uint32_t>::max() - 36) / 2))
    return Status::Fail("Speech PCM output exceeds its byte budget");
  for (const auto value : pcm)
    if (!std::isfinite(value) || value < -1 || value > 1)
      return Status::Fail("Speech PCM contains invalid samples");
  const size_t bytes = pcm.size() * 2;
  std::string result;
  result.reserve(bytes + header);
  if (wave) {
    const auto status = SpeechWaveHeader(pcm.size(), max_output_bytes, &result);
    if (!status) return status;
  }
  for (const auto value : pcm) {
    const auto sample =
        std::clamp(std::lround(double(value) * 32768), -32768L, 32767L);
    U16(static_cast<uint16_t>(sample), &result);
  }
  *out = std::move(result);
  return {};
}
}  // namespace q4t::audio
