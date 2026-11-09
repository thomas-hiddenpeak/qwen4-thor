#pragma once

#include <cstdint>

namespace q4t::server::cache_detail {
inline constexpr uint64_t kMagic = 0x31304f4943543451ULL;  // Q4TCIO01
enum Operation : uint64_t { kInit = 1, kCreate, kRead, kWrite, kStat, kClose };
inline constexpr uint64_t kTestTruncate = 100;
inline constexpr uint64_t kTestShortWrite = 101;
enum Code : uint64_t { kOk, kInvalid, kIoError, kFull };
struct Frame {
  uint64_t magic = kMagic;
  uint64_t sequence = 0;
  uint64_t generation = 0;
  uint64_t operation = 0;
  uint64_t file = 0;
  uint64_t offset = 0;
  uint64_t bytes = 0;
  uint64_t reserved = 0;
};
struct Reply {
  uint64_t magic = kMagic;
  uint64_t sequence = 0;
  uint64_t generation = 0;
  uint64_t code = kIoError;
  uint64_t file = 0;
  uint64_t bytes = 0;
  uint64_t allocated = 0;
  uint64_t span = 0;
  uint64_t rss = 0;
};
static_assert(sizeof(Frame) == 64 && sizeof(Reply) == 72);
}  // namespace q4t::server::cache_detail
