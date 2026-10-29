// HTTP response helpers shared by the server and host contract tests.
#pragma once

#include <cstddef>
#include <string>
#include <string_view>

namespace q4t::server::detail {

// Emits complete UTF-8 scalars, replacing each malformed maximal subpart with
// U+FFFD. At most three bytes survive Append; Finish resolves an incomplete
// final scalar. One instance belongs to one response, across token boundaries.
class Utf8Stream {
 public:
  std::string Append(std::string_view bytes);
  std::string Finish();

 private:
  std::string pending_;
};

std::string NormalizeUtf8(std::string_view bytes);
std::string JsonEscape(const std::string& s);
bool WriteAll(int fd, const char* data, size_t len);
bool WriteAll(int fd, const std::string& s);
bool SendSimple(int fd, int code, const char* status, const std::string& body,
                const std::string& content_type);
void SendError(int fd, int code, const std::string& message);
std::string SseChunk(const std::string& id, const std::string& model,
                     const std::string& delta_role, const std::string& content,
                     const std::string& finish_reason, int index);

}  // namespace q4t::server::detail
