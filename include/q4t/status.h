// Lightweight status/result type for q4t.
//
// Avoids exceptions on the hot path; fallible operations return a Status and
// the caller checks it. For value-returning operations, use a small struct
// carrying a Status alongside the payload.
#pragma once

#include <string>

namespace q4t {

// A fallible operation result. `ok()` is true when no error occurred.
class Status {
 public:
  Status() = default;
  Status(std::string msg) : message_(std::move(msg)) {}

  bool ok() const { return message_.empty(); }
  explicit operator bool() const { return ok(); }
  const std::string& message() const { return message_; }

  // Convenience: a failed status with a formatted message.
  static Status Fail(std::string msg) { return Status(std::move(msg)); }

 private:
  std::string message_;
};

}  // namespace q4t
