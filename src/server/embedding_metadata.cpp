#include "q4t/server/embedding_metadata.h"

#include "q4t/server/embeddings_plan.h"

namespace q4t::server {
Status ParseEmbeddingMetadata(const std::string& body, io::Json* metadata,
                              bool wrapped, std::string* compact) {
  if (compact) compact->clear();
  *metadata = {};
  if (body.size() >
      (wrapped ? kExpandedApplicationBytes + 65536 : kChatBodyBytes))
    return Status::Fail("Embedding JSON exceeds finite byte capacity");
  auto limits = kApplicationJsonLimits;
  limits.max_values = kEmbeddingInputs * kEmbeddingDimensions + 65536 + 32;
  limits.max_depth += wrapped ? 4 : 0;
  limits.max_string_bytes = kExpandedApplicationBytes + 65536;
  const auto status = io::ParseJsonProjection(
      body, metadata,
      [wrapped](std::span<const std::string_view> path) {
        if (!wrapped) return !(path.size() == 1 && path[0] == "data");
        return !(path.size() == 3 && path[0] == "response" &&
                 path[1] == "body" && path[2] == "data");
      },
      limits);
  if (!status) return status;
  if (!metadata->IsObject()) return Status::Fail("Expected JSON object");
  if (!compact) return {};
  std::string result;
  result.reserve(body.size());
  bool quoted = false, escaped = false;
  for (const char value : body) {
    if (quoted) {
      result += value;
      if (escaped)
        escaped = false;
      else if (value == '\\')
        escaped = true;
      else if (value == '"')
        quoted = false;
    } else if (value == '"') {
      quoted = true;
      result += value;
    } else if (value != ' ' && value != '\t' && value != '\r' &&
               value != '\n') {
      result += value;
    }
  }
  *compact = std::move(result);
  return {};
}
}  // namespace q4t::server
