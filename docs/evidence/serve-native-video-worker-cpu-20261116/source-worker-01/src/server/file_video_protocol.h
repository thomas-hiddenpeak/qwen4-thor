#pragma once

#include <cstdint>

#include "q4t/server/file_video.h"

namespace q4t::server::video_detail {
inline constexpr uint64_t kMagic = 0x5134545649443031;
enum class Ready : uint64_t { kReady = 1, kDependency, kSandbox, kInvalid };
enum class Failure : uint64_t { kNone, kDependency, kInvalid, kCapacity };
struct Request {
  uint64_t magic = kMagic, bytes = 0;
  FileVideoLimits limits;
};
struct Result {
  uint64_t magic = kMagic;
  Failure failure = Failure::kNone;
  uint64_t metadata_bytes = 0, frames = 0;
  uint64_t width = 0, height = 0, duration_ns = 0, scanned_frames = 0;
};
struct Frame {
  uint64_t bytes = 0, timestamp_ns = 0, source_index = 0;
};
bool EnterVideoSandbox();
}  // namespace q4t::server::video_detail
