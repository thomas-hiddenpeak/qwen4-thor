// Test-only IO fault/pressure shim; never linked into the runner.
#include <dlfcn.h>
#include <sys/uio.h>
#include <unistd.h>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string_view>

namespace {
std::atomic<int> closing_fd{-1};
void MarkClose(int fd, const void* data, size_t size) {
  const char* mode = std::getenv("Q4T_TEST_TRACE_WRITE");
  if (!mode || std::strcmp(mode, "close") != 0 ||
      std::string_view(static_cast<const char*>(data), size)
              .find("\"complete\":true") == std::string_view::npos)
    return;
  char path[64], target[4096];
  std::snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
  const auto length = readlink(path, target, sizeof(target) - 1);
  if (length < 0) return;
  target[length] = 0;
  if (std::strstr(target, "/manifest.partial")) closing_fd.store(fd);
}
bool Inject(int fd) {
  static std::atomic<bool> fired{false};
  const char* mode = std::getenv("Q4T_TEST_TRACE_WRITE");
  if (!mode || std::strcmp(mode, "close") == 0 || fired.load()) return false;
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
  MarkClose(fd, data, size);
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

extern "C" int close(int fd) {
  static const auto real =
      reinterpret_cast<decltype(&close)>(dlsym(RTLD_NEXT, "close"));
  if (!real) std::_Exit(127);
  const int result = real(fd);
  int expected = fd;
  if (closing_fd.compare_exchange_strong(expected, -1)) {
    std::fprintf(stderr, "[trace-write-fault] close\n");
    errno = EIO;
    return -1;
  }
  return result;
}

extern "C" int fclose(FILE* file) {
  static const auto real =
      reinterpret_cast<decltype(&fclose)>(dlsym(RTLD_NEXT, "fclose"));
  if (!real) std::_Exit(127);
  const int fd = fileno(file);
  const int result = real(file);
  int expected = fd;
  if (closing_fd.compare_exchange_strong(expected, -1)) {
    std::fprintf(stderr, "[trace-write-fault] close\n");
    errno = EIO;
    return EOF;
  }
  return result;
}
