#include "q4t/server/http_request.h"

#include <unistd.h>

#include <charconv>
#include <string_view>

namespace q4t::server {
namespace {

bool IsToken(unsigned char c) {
  return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
         (c >= '0' && c <= '9') ||
         std::string_view("!#$%&'*+-.^_`|~").find(c) != std::string_view::npos;
}

std::string_view Trim(std::string_view s) {
  while (!s.empty() && (s.front() == ' ' || s.front() == '\t'))
    s.remove_prefix(1);
  while (!s.empty() && (s.back() == ' ' || s.back() == '\t'))
    s.remove_suffix(1);
  return s;
}

}  // namespace

bool ReadHttpRequest(int fd, std::string* method, std::string* path,
                     std::string* body) {
  constexpr size_t kMaxHeader = 64 * 1024;
  constexpr size_t kMaxBody = 16 * 1024 * 1024;
  std::string buf;
  char tmp[4096];
  size_t header_end = std::string::npos;
  while ((header_end = buf.find("\r\n\r\n")) == std::string::npos) {
    if (buf.size() >= kMaxHeader) return false;
    const ssize_t n = ::read(fd, tmp, sizeof(tmp));
    if (n <= 0) return false;
    buf.append(tmp, static_cast<size_t>(n));
  }
  const size_t head_len = header_end + 4;
  if (head_len > kMaxHeader) return false;
  const std::string_view head(buf.data(), header_end + 2);
  const size_t line_end = head.find("\r\n");
  if (line_end == std::string_view::npos) return false;
  const std::string_view line = head.substr(0, line_end);
  const size_t first = line.find(' ');
  const size_t last = line.rfind(' ');
  if (first == std::string_view::npos || first == 0 || last <= first + 1)
    return false;
  const auto verb = line.substr(0, first);
  const auto target = line.substr(first + 1, last - first - 1);
  const auto version = line.substr(last + 1);
  if (version != "HTTP/1.0" && version != "HTTP/1.1") return false;
  for (unsigned char c : verb)
    if (!IsToken(c)) return false;
  if (target.front() != '/') return false;
  for (unsigned char c : target)
    if (c <= 0x20 || c >= 0x7F || c == '#') return false;

  size_t length = 0;
  bool seen_length = false, seen_host = false;
  size_t pos = line_end + 2;
  while (pos < head.size()) {
    const size_t end = head.find("\r\n", pos);
    if (end == std::string_view::npos) return false;
    const auto field = head.substr(pos, end - pos);
    const size_t colon = field.find(':');
    if (colon == std::string_view::npos || colon == 0) return false;
    std::string name(field.substr(0, colon));
    for (char& c : name) {
      if (!IsToken(static_cast<unsigned char>(c))) return false;
      if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    }
    const auto value = Trim(field.substr(colon + 1));
    for (unsigned char c : value)
      if ((c < 0x20 && c != '\t') || c == 0x7F) return false;
    if (name == "content-length") {
      if (seen_length || value.empty()) return false;
      seen_length = true;
      for (char c : value)
        if (c < '0' || c > '9') return false;
      const auto result = std::from_chars(value.data(),
                                          value.data() + value.size(), length);
      if (result.ec != std::errc{} ||
          result.ptr != value.data() + value.size() || length > kMaxBody)
        return false;
    } else if (name == "transfer-encoding" || name == "expect") {
      return false;
    } else if (name == "host") {
      if (seen_host || value.empty()) return false;
      seen_host = true;
    }
    pos = end + 2;
  }
  if (version == "HTTP/1.1" && !seen_host) return false;
  *method = verb;
  *path = target;
  // Do not keep a string_view into buf across an append/reallocation.
  while (buf.size() - head_len < length) {
    const ssize_t n = ::read(fd, tmp, sizeof(tmp));
    if (n <= 0) return false;
    buf.append(tmp, static_cast<size_t>(n));
  }
  *body = buf.substr(head_len, length);
  return true;
}

}  // namespace q4t::server
