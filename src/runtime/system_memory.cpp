#include "q4t/runtime/system_memory.h"

#include <fcntl.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <charconv>
#include <limits>

namespace q4t::runtime {
namespace {
constexpr size_t kMaxMeminfoBytes = 16384;

bool ParseKilobytes(std::string_view value, uint64_t* bytes) {
  while (!value.empty() && (value.front() == ' ' || value.front() == '\t'))
    value.remove_prefix(1);
  uint64_t kb = 0;
  const char* end = value.data() + value.size();
  const auto parsed = std::from_chars(value.data(), end, kb);
  if (parsed.ec != std::errc() || parsed.ptr == end ||
      (*parsed.ptr != ' ' && *parsed.ptr != '\t') ||
      kb > std::numeric_limits<uint64_t>::max() / 1024)
    return false;
  value.remove_prefix(parsed.ptr - value.data());
  while (!value.empty() && (value.front() == ' ' || value.front() == '\t'))
    value.remove_prefix(1);
  if (!value.starts_with("kB")) return false;
  value.remove_prefix(2);
  for (char ch : value)
    if (ch != ' ' && ch != '\t') return false;
  *bytes = kb * 1024;
  return true;
}
}  // namespace

SystemMemory ParseSystemMemory(std::string_view text) {
  if (text.empty() || text.size() > kMaxMeminfoBytes || text.back() != '\n')
    return {};
  SystemMemory result;
  bool total_seen = false;
  bool available_seen = false;
  while (!text.empty()) {
    const size_t end = text.find('\n');
    const auto line = text.substr(0, end);
    text.remove_prefix(end + 1);
    bool* seen = nullptr;
    uint64_t* bytes = nullptr;
    size_t prefix = 0;
    if (line.starts_with("MemTotal:")) {
      seen = &total_seen;
      bytes = &result.total_bytes;
      prefix = 9;
    } else if (line.starts_with("MemAvailable:")) {
      seen = &available_seen;
      bytes = &result.available_bytes;
      prefix = 13;
    }
    if (seen) {
      if (*seen || !ParseKilobytes(line.substr(prefix), bytes)) return {};
      *seen = true;
    }
  }
  if (!total_seen || !available_seen || result.total_bytes == 0 ||
      result.available_bytes > result.total_bytes)
    return {};
  result.valid = true;
  return result;
}

SystemMemory ReadSystemMemory() {
  const int fd = open("/proc/meminfo", O_RDONLY | O_CLOEXEC);
  if (fd < 0) return {};
  std::array<char, kMaxMeminfoBytes + 1> buffer;
  size_t size = 0;
  bool complete = false;
  while (size < buffer.size()) {
    const ssize_t n = read(fd, buffer.data() + size, buffer.size() - size);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) {
      complete = n == 0;
      break;
    }
    size += static_cast<size_t>(n);
  }
  close(fd);
  return complete ? ParseSystemMemory({buffer.data(), size}) : SystemMemory{};
}

}  // namespace q4t::runtime
