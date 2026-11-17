#pragma once
#include <cstdint>
namespace q4t::server::asset_detail {
inline constexpr uint64_t kMagic = 0x32414d4542543451ULL;
struct Request {
  uint64_t magic = kMagic;
  uint64_t profile = 0;
  uint64_t path_bytes = 0;
  uint64_t include_weights = 1;
};
struct Reply {
  uint64_t magic = kMagic;
  uint64_t profile = 0;
  uint64_t code = 1;
  uint64_t file_count = 0;
  uint64_t total_bytes = 0;
};
}  // namespace q4t::server::asset_detail
