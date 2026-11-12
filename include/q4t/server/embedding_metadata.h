// Full JSON validation with bounded metadata retention for large vectors.
#pragma once

#include "q4t/io/json.h"

namespace q4t::server {
// wrapped selects the private Batch JSONL envelope. Only the actual embedding
// data subtree is discarded from metadata; the original body stays intact.
// Optional compact removes formatting whitespace, preserving every string and
// number spelling. The metadata projection must never replace the wire body.
Status ParseEmbeddingMetadata(const std::string& body, io::Json* metadata,
                              bool wrapped = false,
                              std::string* compact = nullptr);
}  // namespace q4t::server
