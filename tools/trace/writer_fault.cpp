// Test-only IO fault/pressure shim; never linked into the runner.
#include <dlfcn.h>
#include <sys/uio.h>
#include <unistd.h>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {
bool Inject(int fd) {
  static std::atomic<bool> fired{false};
  const char* mode = std::getenv("Q4T_TEST_TRACE_WRITE");
  if (!mode || fired.load()) return false;
  char path[64], target[4096];
  std::snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
  const auto size = readlink(path, target, sizeof(target) - 1);
  if (size < 0) return false;
  target[size] = 0;
  if (!std::strstr(target, "/request-1.partial") || fired.exchange(true))
    return false;
  std::fprintf(stderr, "[trace-write-fault] %s\n", mode);
  if (std::strcmp(mode, "slow") == 0) {
    usleep(2000000);
    return false;
  }
  errno = ENOSPC;
  return true;
}
}  // namespace
extern "C" ssize_t write(int fd, const void* data, size_t size) {
  static const auto real =
      reinterpret_cast<decltype(&write)>(dlsym(RTLD_NEXT, "write"));
  if (!real) std::_Exit(127);
  if (Inject(fd)) return -1;
  return real(fd, data, size);
}
extern "C" ssize_t writev(int fd, const iovec* data, int count) {
  static const auto real =
      reinterpret_cast<decltype(&writev)>(dlsym(RTLD_NEXT, "writev"));
  if (!real) std::_Exit(127);
  if (Inject(fd)) return -1;
  return real(fd, data, count);
}
