#include "q4t/server/vector_embedding_execution.h"

#include "object_protocol.h"
#include "q4t/model/embedding_config.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
ObjectReply ExecuteVectorIngestionWithQuota(
    ObjectStore& objects, const BinaryAccess& access,
    const VectorIngestionPrepared& prepared, ClientAdmission& clients,
    const ClientIdentity& client, const ClientLease& permit,
    ActualUsageCounter& usage, const EmbeddingStepExecutor& execute,
    VectorIngestionResult* result) {
  using object_detail::Error;
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  if (!execute || !clients.OwnsIdentity(client) ||
      !permit.Holds(client, ClientLane::kData) || scope != access.scope)
    return Error(403, "vector_execution_owner_invalid",
                 "Ingestion client execution ownership changed");
  if (const auto* limits = client.Limits();
      limits && prepared.input_tokens > limits->max_prompt_tokens)
    return Error(400, "client_prompt_limit_exceeded",
                 "Ingestion exceeds cumulative client input limit");
  ClientRateLease rate;
  uint64_t completed = 0;
  bool unknown_usage = false;
  usage.Enable();
  return ExecuteVectorIngestion(
      objects, access, prepared,
      [&](uint64_t tokens) {
        const auto admitted =
            rate.ReserveInternalInput(clients, client, tokens);
        if (admitted == ClientRateResult::kAllowed) return ObjectReply{};
        const bool quota = admitted == ClientRateResult::kTpm;
        const bool frozen = admitted == ClientRateResult::kFrozen;
        return Error(quota    ? 429
                     : frozen ? 503
                              : 500,
                     quota    ? "client_tpm_exhausted"
                     : frozen ? "service_draining"
                              : "client_rate_invalid",
                     "Ingestion token admission unavailable");
      },
      [&](std::span<const uint32_t> input, std::vector<float>* vector,
          uint64_t* used) {
        // BuildVectorIndex has already validated the model vocabulary.
        const std::vector<int32_t> tokens(input.begin(), input.end());
        EmbeddingStepResult step;
        usage.BeginStep();
        ScopeExit account([&] {
          *used = step.committed_input;
          if (unknown_usage || step.committed_input > input.size() ||
              step.committed_input > prepared.input_tokens - completed) {
            unknown_usage = true;
            result->usage_unknown = true;
            usage.Merge(std::nullopt);
            // Preserve the entire admitted envelope on an invalid producer
            // receipt. An unknown observation must not become a false zero.
            rate.Start();
          } else {
            completed += step.committed_input;
            usage.Observe({step.committed_input});
            rate.StartPrompt(completed);
          }
          usage.EndStep();
        });
        auto reply = execute(tokens, &step);
        if (!reply.ok()) return reply;
        if (step.committed_input != input.size() ||
            step.hidden.size() != model::EmbeddingConfig::kHidden)
          return Error(502, "embedding_result_invalid",
                       "Native embedding did not complete its input");
        const auto normalized = NormalizeEmbeddingHidden(
            step.hidden, prepared.identity.dimensions, vector);
        return normalized
                   ? ObjectReply{}
                   : Error(502, "embedding_result_invalid",
                           "Native embedding returned invalid hidden states");
      },
      result);
}
}  // namespace q4t::server
