// Native internal execution receives the same finite JSON as HTTP clients,
// without a socket, transport worker, proxy, or a second request/admission.
#pragma once

#include <string>
#include <string_view>
#include <utility>

#include "q4t/server/request_limits.h"

namespace q4t::server {
class FiniteResponse {
 public:
  explicit FiniteResponse(size_t max_bytes = kChatBodyBytes);
  bool Record(int status, std::string_view content_type, std::string_view body);
  void Reject() noexcept { failed_ = true; }
  bool Complete() const { return received_ && !failed_; }
  bool Failed() const { return failed_; }
  int Status() const { return status_; }
  const std::string& Body() const { return body_; }
  std::string TakeBody() { return std::move(body_); }

 private:
  size_t max_bytes_;
  int status_ = 0;
  bool received_ = false, failed_ = false;
  std::string body_;
};
}  // namespace q4t::server
