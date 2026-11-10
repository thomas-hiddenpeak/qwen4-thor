#pragma once

#include <cstdint>

namespace q4t::server::webp_detail {
inline constexpr uint64_t kMagic = 0x5134545745425031;
enum class Ready : uint64_t {
  kReady = 1,
  kDependency,
  kSandbox,
  kCapacity,
  kInvalid
};
enum class Failure : uint64_t {
  kNone,
  kDependency,
  kSandbox,
  kInvalid,
  kCapacity
};
struct Request {
  uint64_t magic = kMagic, bytes = 0, pixels = 0, edge = 0, output_bytes = 0;
};
struct Result {
  uint64_t magic = kMagic;
  Failure failure = Failure::kNone;
  uint64_t bytes = 0;
  char decoder_version[64]{}, encoder_version[64]{};
  char decoder_sha256[65]{}, encoder_sha256[65]{};
};
}  // namespace q4t::server::webp_detail
