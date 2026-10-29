// HTTP response helpers shared by the server and host contract tests.
#pragma once

#include <cstddef>
#include <chrono>
#include <string>
#include <string_view>

#include "q4t/server/request_control.h"

namespace q4t::server::detail {

enum class ResponseState { kUnsent, kStreaming, kComplete };
// Allocation-free best effort after an exception; never adds a second terminal.
void SendExceptionResponse(int fd, ResponseState state) noexcept;

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
struct WriteOptions {
  std::chrono::steady_clock::time_point deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(30);
  RequestControl* control = nullptr;
};
// One deadline covers the entire logical write, including partial progress.
bool WriteAll(int fd, const char* data, size_t len,
              const WriteOptions& options = {});
bool WriteAll(int fd, const std::string& s, const WriteOptions& options = {});
bool SendSimple(int fd, int code, const char* status, const std::string& body,
                const std::string& content_type,
                const WriteOptions& options = {});
void SendError(int fd, int code, const std::string& message,
               const WriteOptions& options = {});
std::string SseChunk(const std::string& id, const std::string& model,
                     const std::string& delta_role, const std::string& content,
                     const std::string& finish_reason, int index);

}  // namespace q4t::server::detail
