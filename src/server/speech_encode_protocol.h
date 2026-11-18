#pragma once

#include "q4t/server/speech_encode.h"

namespace q4t::server::speech_encode_detail {
inline constexpr uint64_t kMagic = 0x5134545350453031;
// VisualOn consumes complete 1024-sample frames and does not drain EOS.
// Complete the final frame and feed two zero frames through the actual
// encoder. These are disclosed codec tail input, never synthesized speech.
inline uint64_t EncoderTailSamples(SpeechFormat format, uint64_t samples) {
  return format == SpeechFormat::kAac ? (1024 - samples % 1024) % 1024 + 2048
                                      : 0;
}
enum class Ready : uint64_t { kReady = 1, kDependency, kSandbox };
enum class Failure : uint64_t { kNone, kDependency, kInvalid, kCapacity };
struct Request {
  uint64_t magic = kMagic, samples = 0;
  SpeechEncodeLimits limits;
  SpeechFormat format = SpeechFormat::kMp3;
};
struct Result {
  uint64_t magic = kMagic;
  Failure failure = Failure::kNone;
  uint64_t metadata_bytes = 0, bytes = 0;
};
}  // namespace q4t::server::speech_encode_detail
