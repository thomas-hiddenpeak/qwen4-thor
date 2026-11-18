// Weight I/O in the isolated asset child; never fork during these reads.
#pragma once

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>

namespace q4t::server::detail {
inline constexpr size_t kModelReadAlignment = 4096;
inline constexpr size_t kModelReadBlockBytes = 256 * 1024;

// O_DIRECT alone is insufficient: some filesystems silently buffer
// misaligned I/O. Require the kernel's per-file alignment contract before
// excluding newly read weight pages from the allocation estimate.
inline bool DirectModelAssetReady(int fd) {
  const int flags = fcntl(fd, F_GETFL);
  struct statx info {};
  return flags >= 0 && (flags & O_DIRECT) &&
         statx(fd, "", AT_EMPTY_PATH, STATX_DIOALIGN, &info) == 0 &&
         (info.stx_mask & STATX_DIOALIGN) && info.stx_dio_mem_align &&
         info.stx_dio_offset_align &&
         kModelReadAlignment % info.stx_dio_mem_align == 0 &&
         kModelReadBlockBytes % info.stx_dio_offset_align == 0;
}

template <typename Sink>
bool StreamDirectModelAsset(int fd, size_t bytes, const Sink& sink) {
  if (!bytes || !DirectModelAssetReady(fd)) return false;
  alignas(kModelReadAlignment) std::array<uint8_t, kModelReadBlockBytes> buffer;
  for (size_t offset = 0; offset < bytes;) {
    // The final syscall remains aligned; EOF may return a partial block.
    // Only actual file bytes are sent and included in the pinned SHA256.
    const size_t expected = std::min(bytes - offset, buffer.size());
    ssize_t count;
    do {
      count = pread(fd, buffer.data(), buffer.size(), offset);
    } while (count < 0 && errno == EINTR);
    if (count != static_cast<ssize_t>(expected) ||
        !sink(buffer.data(), expected))
      return false;
    offset += expected;
  }
  return true;
}
}  // namespace q4t::server::detail
