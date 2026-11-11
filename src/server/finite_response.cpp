#include "q4t/server/finite_response.h"

#include <stdexcept>

#include "q4t/server/request_json.h"
#include "q4t/server/embedding_metadata.h"

namespace q4t::server {
FiniteResponse::FiniteResponse(size_t max_bytes, FiniteResponseFormat format)
    : max_bytes_(max_bytes), format_(format) {
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
    const auto validation = format_ == FiniteResponseFormat::kEmbeddings
                            ? ParseEmbeddingMetadata(value, &parsed)
                            : io::ParseJson(value, &parsed, true,
                                             kApplicationJsonLimits);
    if (!validation ||
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
