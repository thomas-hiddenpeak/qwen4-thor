#pragma once

#include "q4t/server/actual_usage.h"
#include "q4t/server/embeddings_plan.h"
#include "q4t/server/object_store.h"

namespace q4t::server {
struct EmbeddingStepResult {
  std::vector<uint16_t> hidden;
  uint64_t committed_input = 0;
};
using EmbeddingStepExecutor = std::function<ObjectReply(
    const std::vector<int32_t>&, EmbeddingStepResult*)>;
// One external RPM, all input reserved before execution, and actual committed
// tokens on every unwind. Executor drains its model/slot owners before return.
ObjectReply ExecuteEmbeddingInputs(
    const EmbeddingsRequestPlan& plan,
    const std::vector<std::vector<int32_t>>& inputs, ClientAdmission& clients,
    const ClientIdentity& client, ClientRateLease& rate,
    ActualUsageCounter& usage, const EmbeddingStepExecutor& execute,
    const std::function<bool()>& cancelled, std::string* response);
}  // namespace q4t::server
