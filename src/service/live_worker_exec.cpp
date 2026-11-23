// Execute exactly the qualified bytes, even if the source is replaced while
// the service is running. All failures are local to this auxiliary process.
#include "q4t/server/live_worker.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <filesystem>
#include <vector>

#include "q4t/trace/sha256.h"

namespace q4t::server {
int RunPinnedLiveWorker(int argc, char** argv) {
  if (argc < 5) return 2;
  const std::string_view name(argv[2]), expected(argv[3]);
  if ((name != "q4t-live-webrtc" && name != "q4t-live-sip" &&
       name != "q4t-sip-media") || !ValidWorkerDigest(expected))
    return 2;
  std::error_code ec;
  auto self = std::filesystem::read_symlink("/proc/self/exe", ec);
  if (ec || !self.is_absolute()) return 1;
  const auto path = self.parent_path() / name;
  int input = open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
  struct stat info{};
  constexpr off_t kLimit = 8 * 1024 * 1024;
  if (input < 0 || fstat(input, &info) || !S_ISREG(info.st_mode) ||
      !(info.st_mode & S_IXUSR) || info.st_size < 4 || info.st_size > kLimit)
    return 1;
  std::vector<uint8_t> bytes(info.st_size);
  size_t offset = 0;
  while (offset < bytes.size()) {
    auto n = read(input, bytes.data() + offset, bytes.size() - offset);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) return 1;
    offset += n;
  }
  uint8_t extra;
  ssize_t tail;
  do {
    tail = read(input, &extra, 1);
  } while (tail < 0 && errno == EINTR);
  close(input);
  if (tail != 0 || bytes[0] != 0x7f || bytes[1] != 'E' || bytes[2] != 'L' ||
      bytes[3] != 'F' || trace::HexDigest(trace::Sha256(bytes)) != expected)
    return 1;
  const int image = memfd_create(argv[2], MFD_ALLOW_SEALING | MFD_CLOEXEC);
  if (image < 0 || fchmod(image, 0500)) return 1;
  offset = 0;
  while (offset < bytes.size()) {
    auto n = write(image, bytes.data() + offset, bytes.size() - offset);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) return 1;
    offset += n;
  }
  if (fcntl(image, F_ADD_SEALS, F_SEAL_WRITE | F_SEAL_GROW |
                                F_SEAL_SHRINK | F_SEAL_SEAL) < 0)
    return 1;
  argv[3] = argv[2];
  fexecve(image, argv + 3, environ);
  return 1;
}
}  // namespace q4t::server
