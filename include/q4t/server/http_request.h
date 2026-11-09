// Bounded HTTP/1.x request reader shared by the runner and native ingress.
#pragma once

#include <atomic>
#include <chrono>
#include <cstddef>
#include <functional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "q4t/server/http_connection.h"
#include "q4t/server/http_observation.h"

namespace q4t::server {

inline constexpr size_t kChatBodyBytes = 16 * 1024 * 1024;
inline constexpr size_t kAggregateChatBodyBytes = 48 * 1024 * 1024;
inline constexpr size_t kCancelBodyBudgetBytes = 32 * 1024 * 1024;

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
  // Untrusted until an explicit ingress proxy policy validates the TCP peer.
  std::string forwarded;
  std::string forwarded_for;
  std::string forwarded_host;
  std::string forwarded_proto;
  std::string forwarded_prefix;
  std::string error_code;
  std::string error_param;
  size_t content_length = 0;
  // Unknown decoded length during admission. content_length becomes the actual
  // decoded length on success. Admission may lower body_limit before reads.
  bool chunked = false;
  bool expect_continue = false;
  bool keep_alive = false;
  bool http11 = false;
  size_t body_limit = kChatBodyBytes;
  // Separate from routing/auth/framing. These never override header fields.
  std::vector<std::pair<std::string, std::string>> trailers;
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
  explicit HttpBodyLease(HttpBodyBudget& budget) : budget_(&budget) {}
  ~HttpBodyLease() { budget_->Release(bytes_); }
  HttpBodyLease(const HttpBodyLease&) = delete;
  HttpBodyLease& operator=(const HttpBodyLease&) = delete;
  // Trusted route admission selects a reserved partition before any body work.
  bool SelectBudget(HttpBodyBudget& budget) {
    if (bytes_) return false;
    budget_ = &budget;
    return true;
  }
  bool Acquire(size_t bytes) {
    if (bytes_ || !budget_->Acquire(bytes)) return false;
    bytes_ = bytes;
    return true;
  }
  bool GrowTo(size_t bytes) {
    if (bytes < bytes_ || !budget_->Acquire(bytes - bytes_)) return false;
    bytes_ = bytes;
    return true;
  }

 private:
  HttpBodyBudget* budget_;
  size_t bytes_ = 0;
};

struct HttpRequestState {
  // Only the current request reader may mutate this buffer. No dispatch until
  // the preceding response and all of its cleanup have finished.
  std::string pending;
};

// Set only by trusted route admission. The sink must honor the supplied
// absolute deadline, consume/copy the view before returning, and stage writes
// privately. Only ReadHttpRequest returning true authorizes final publication:
// the sink can receive all payload bytes before a malformed trailer/EOF.
// Reservation includes the sink's bounded working memory, independent of the
// decoded wire-byte limit. Ordinary buffered requests retain their limits.
struct HttpBodyStream {
  size_t max_bytes = 0;
  size_t reservation_bytes = 0;
  size_t max_metadata_bytes = 64 * 1024;
  std::function<bool(std::string_view,
                     std::chrono::steady_clock::time_point)> consume;
  int failure_status = 503;
  std::string failure_code = "body_consumer_failed";
};

// Supports HTTP/1.x origin-form, Content-Length and HTTP/1.1 chunked framing.
// Rejects mixed/duplicate framing, duplicate Host and malformed fields.
// Headers <=64 KiB; chat/preflight bodies <=16 MiB, other routes <=4 KiB.
// timeout_ms is an absolute header+body deadline, unaffected by progress.
// A supplied lease reserves bytes before body reads (released by caller).
// Headers expose validated transport metadata. admit runs after headers and
// before body reservation/reads; false rejects without consuming the body.
// Admission may set body_limit and a stream. Buffered chunked reservations
// grow before each chunk; streams reserve their fixed working set once.
// A supplied state preserves bytes belonging to a subsequent request. Without
// it the caller must close. Any failure requires closing, never resynchronizing.
// Interim 100 is sent only after admission and known capacity/length checks;
// it never starts the final response. Custom transports must supply its sender.
// A transport adapter must honor the same absolute deadline; ETIMEDOUT denotes
// a deadline, zero EOF, and other negative results a socket failure.
using HttpReceive = std::function<ssize_t(
    void*, size_t, std::chrono::steady_clock::time_point)>;
using HttpContinue =
    std::function<bool(std::chrono::steady_clock::time_point)>;
// Starts a new idle window only after the preceding request fully releases its
// leases. Reads at most one byte; TLS adapters also see already decrypted data.
bool AwaitHttpRequest(int fd, HttpRequestState* state, int idle_ms = 5000,
                      const HttpReceive& receive = {});
// After the final response, half-close writes and discard at most 64 KiB for
// 100 ms. This preserves early HTTP errors against a racing request body/RST.
// Never parses/dispatches the tail; ownership and close(fd) remain with caller.
void DrainHttpClose(int fd) noexcept;
bool ReadHttpRequest(int fd, std::string* method, std::string* path,
                     std::string* body, int timeout_ms = 30000,
                     HttpBodyLease* lease = nullptr,
                     int* failure_status = nullptr,
                     HttpRequestHeaders* headers = nullptr,
                     const std::function<bool()>& admit = {},
                     const HttpReceive& receive = {},
                     HttpRequestState* state = nullptr,
                     const HttpContinue& send_continue = {},
                     HttpBodyStream* stream = nullptr);

}  // namespace q4t::server
