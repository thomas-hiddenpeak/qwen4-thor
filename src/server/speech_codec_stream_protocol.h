#pragma once

#include "q4t/server/speech_codec_stream.h"
#include "speech_encode_protocol.h"

namespace q4t::server::speech_stream_detail {
inline constexpr uint64_t kMagic = 0x5134545350533031;
using speech_encode_detail::Failure;
using speech_encode_detail::Ready;
struct Request {
  uint64_t magic = kMagic;
  SpeechEncodeLimits limits;
  SpeechFormat format = SpeechFormat::kMp3;
};
enum class EventKind : uint64_t { kNeedInput = 1, kOutput, kDone, kFailure };
struct Event {
  uint64_t magic = kMagic;
  EventKind kind = EventKind::kFailure;
  uint64_t bytes = 0;
  Failure failure = Failure::kNone;
};
struct Input {
  uint64_t magic = kMagic, samples = 0, final = 0;
};
}  // namespace q4t::server::speech_stream_detail
