#include "q4t/server/finite_response.h"

#include <stdexcept>

#include "q4t/server/request_json.h"

namespace q4t::server {
FiniteResponse::FiniteResponse(size_t max_bytes) : max_bytes_(max_bytes) {
  if (!max_bytes || max_bytes > kChatBodyBytes)
    throw std::invalid_argument("Invalid internal response capacity");
}
bool FiniteResponse::Record(int status, std::string_view content_type,
                            std::string_view body) {
  if (received_ || failed_ || status < 200 || status > 599 ||
      (status >= 300 && status < 400) || content_type != "application/json" ||
      body.size() > max_bytes_) {
    failed_ = true;
    return false;
  }
  // Validate before accepting an endpoint result. Exact numeric spellings and
  // every response field remain in the original body, including usage/tools.
  try {
    std::string value(body);
    io::Json parsed;
    if (!io::ParseJson(value, &parsed, true, kApplicationJsonLimits) ||
        !parsed.IsObject()) {
      failed_ = true;
      return false;
    }
    body_ = std::move(value);
    status_ = status;
    received_ = true;
    return true;
  } catch (...) {
    failed_ = true;
    throw;
  }
}
}  // namespace q4t::server
