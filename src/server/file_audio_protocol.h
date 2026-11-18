#pragma once

#include "q4t/server/file_audio.h"

namespace q4t::server::audio_detail {
// Same-ELF private IPC: the request layout now includes output_sample_rate.
inline constexpr uint64_t kMagic = 0x5134544155443032;
// Valid limits bound products below uint64_t; no fractional-sample admission.
inline uint64_t OutputSampleLimit(const FileAudioLimits& limits) {
  return limits.max_duration_ns * limits.output_sample_rate / 1000000000ULL;
}
inline uint64_t OutputDuration(uint64_t samples, uint32_t rate) {
  return samples * 1000000000ULL / rate;
}
enum class Container {
  kUnknown,
  kWave,
  kFlac,
  kOgg,
  kMpeg,
  kAdts,
  kMp4,
  kWebm
};
enum class Ready : uint64_t { kReady = 1, kDependency, kSandbox };
enum class Failure : uint64_t { kNone, kDependency, kInvalid, kCapacity };
struct Request {
  uint64_t magic = kMagic, bytes = 0;
  FileAudioLimits limits;
};
struct Result {
  uint64_t magic = kMagic;
  Failure failure = Failure::kNone;
  uint64_t metadata_bytes = 0, samples = 0;
};
Container Detect(std::string_view source);
// Zero is malformed/absent; validates RIFF chunk extents before decoding.
uint16_t WaveTag(std::string_view source);
}  // namespace q4t::server::audio_detail
