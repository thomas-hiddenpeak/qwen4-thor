// Bounded, connection-close HTTP request reader for the runner.
#pragma once

#include <atomic>
#include <cstddef>
#include <string>

namespace q4t::server {

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
// Headers <=64 KiB; chat bodies <=16 MiB, other routes <=4 KiB.
// timeout_ms is an absolute header+body deadline, unaffected by progress.
// A supplied lease reserves chat bytes before body reads (released by caller).
// Failure status is 400, 408 (deadline), or 503 (budget); caller closes socket.
bool ReadHttpRequest(int fd, std::string* method, std::string* path,
                     std::string* body, int timeout_ms = 30000,
                     HttpBodyLease* lease = nullptr,
                     int* failure_status = nullptr);

}  // namespace q4t::server
