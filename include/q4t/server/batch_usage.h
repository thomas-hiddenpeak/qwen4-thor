// Durable Batch accounting from original results, never estimates or budgets.
#pragma once

#include <optional>

#include "q4t/server/batch_state.h"

namespace q4t::server {
struct BatchUsage {
  uint64_t input = 0, output = 0, cached = 0, reasoning = 0;
};
io::Json BatchUsageJson(const BatchUsage& usage);
// Missing/invalid usage remains unknown. Native opaque Completions has no
// cache or hidden reasoning; Chat/Responses must provide actual details.
std::optional<BatchUsage> BatchResponseUsage(std::string_view endpoint,
                                             const io::Json& body);
bool ValidBatchUsageAccounting(const io::Json& data);
size_t BatchUsageRows(const io::Json& data);
// Advances exactly one original completed row. Caller commits this with the
// row result/checkpoint, so a lost receipt cannot charge it a second time.
ObjectReply AddBatchUsage(io::Json* data, size_t index,
                          const std::optional<BatchUsage>& usage);
}  // namespace q4t::server
