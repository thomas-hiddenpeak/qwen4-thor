// Bounded, connection-close HTTP request reader for the runner.
#pragma once

#include <atomic>
#include <chrono>
#include <cstddef>
#include <functional>
#include <string>
#include <string_view>

#include "q4t/server/http_connection.h"
#include "q4t/server/http_observation.h"

namespace q4t::server {

inline constexpr size_t kChatBodyBytes = 16 * 1024 * 1024;
inline constexpr size_t kAggregateChatBodyBytes = 48 * 1024 * 1024;

inline bool ValidHttpRequestId(std::string_view id) {
  if (id.empty() || id.size() > 128) return false;
  for (unsigned char c : id)
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
          c == ':'))
      return false;
  return true;
}

struct HttpRequestHeaders {
  std::string request_id;
  std::string host;
  std::string authorization;
  std::string content_type;
  std::string client_id;
  std::string gateway_token;
  std::string origin;
  std::string cors_method;
  std::string cors_headers;
  std::string error_code;
  std::string error_param;
  size_t content_length = 0;
  HttpReceiveFailure failure = HttpReceiveFailure::kNone;
  int system_error = 0;
};

// Reservation spans reading, parsing, queueing and response completion.
class HttpBodyBudget {
 public:
  explicit HttpBodyBudget(size_t limit) : limit_(limit) {}
  bool Acquire(size_t bytes) {
    size_t old = used_.load();
    do {
      if (bytes > limit_ || old > limit_ - bytes) return false;
    } while (!used_.compare_exchange_weak(old, old + bytes));
    return true;
  }
  void Release(size_t bytes) { used_.fetch_sub(bytes); }
  size_t Used() const { return used_.load(); }

 private:
  const size_t limit_;
  std::atomic<size_t> used_{0};
};

class HttpBodyLease {
 public:
  explicit HttpBodyLease(HttpBodyBudget& budget) : budget_(budget) {}
  ~HttpBodyLease() { budget_.Release(bytes_); }
  HttpBodyLease(const HttpBodyLease&) = delete;
  HttpBodyLease& operator=(const HttpBodyLease&) = delete;
  bool Acquire(size_t bytes) {
    if (bytes_ || !budget_.Acquire(bytes)) return false;
    bytes_ = bytes;
    return true;
  }

 private:
  HttpBodyBudget& budget_;
  size_t bytes_ = 0;
};

// Supports HTTP/1.0 and HTTP/1.1 origin-form requests with Content-Length.
// Rejects transfer encoding, Expect, duplicate framing/Host, malformed fields,
// Headers <=64 KiB; chat/preflight bodies <=16 MiB, other routes <=4 KiB.
// timeout_ms is an absolute header+body deadline, unaffected by progress.
// A supplied lease reserves chat bytes before body reads (released by caller).
// Headers expose validated transport metadata. admit runs after headers and
// before body reservation/reads; false rejects without consuming the body.
// Failure status is 400, 408 (deadline), or 503 (budget); caller closes socket.
// A transport adapter must honor the same absolute deadline; ETIMEDOUT denotes
// a deadline, zero EOF, and other negative results a socket failure.
using HttpReceive = std::function<ssize_t(
    void*, size_t, std::chrono::steady_clock::time_point)>;
bool ReadHttpRequest(int fd, std::string* method, std::string* path,
                     std::string* body, int timeout_ms = 30000,
                     HttpBodyLease* lease = nullptr,
                     int* failure_status = nullptr,
                     HttpRequestHeaders* headers = nullptr,
                     const std::function<bool()>& admit = {},
                     const HttpReceive& receive = {});

}  // namespace q4t::server
