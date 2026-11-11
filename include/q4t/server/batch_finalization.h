// Private output aggregation limits and durable capacity-failure checkpoint.
#pragma once

#include "q4t/server/binary_store.h"

namespace q4t::server {
struct BatchOutputLimits {
  uint64_t bytes = kBinaryMaxBytes;
  size_t chunks = kBinaryMaxChunks;
  bool Valid() const {
    return bytes > 0 && bytes <= kBinaryMaxBytes && chunks > 0 &&
           chunks <= kBinaryMaxChunks;
  }
};
struct BatchOutputFailure {
  // Only rows strictly before this boundary may appear in partial Files.
  size_t row_stop = 0;
  std::string reason;
  int64_t retain_until = 0;
};
io::Json BatchOutputFailureJson(const BatchOutputFailure& failure);
bool ParseBatchOutputFailure(const io::Json& value, BatchOutputFailure* out);
bool ValidBatchOutputFailure(const io::Json& data);
bool ValidBatchOutputFailureTransition(const io::Json& data,
                                       const io::Json* previous);
}  // namespace q4t::server
