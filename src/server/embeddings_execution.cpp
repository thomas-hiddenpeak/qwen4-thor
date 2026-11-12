#include "q4t/server/embeddings_execution.h"

#include "object_protocol.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
ObjectReply ExecuteEmbeddingInputs(
    const EmbeddingsRequestPlan& plan,
    const std::vector<std::vector<int32_t>>& inputs, ClientAdmission& clients,
    const ClientIdentity& client, ClientRateLease& rate,
    ActualUsageCounter& usage, const EmbeddingStepExecutor& execute,
    const std::function<bool()>& cancelled, std::string* response) {
  using object_detail::Error;
  response->clear();
  if (inputs.empty() || inputs.size() != plan.inputs.size() ||
      inputs.size() > kEmbeddingInputs || !client.Allowed() || !execute ||
      !cancelled)
    return Error(500, "embedding_state_invalid", "Invalid embedding owner");
  uint64_t reserved = 0;
  for (const auto& input : inputs) {
    if (input.empty() || input.size() > kEmbeddingInputTokens ||
        input.size() > kEmbeddingTotalTokens - reserved)
      return Error(500, "embedding_state_invalid", "Invalid prepared inputs");
    reserved += input.size();
  }
  if (const auto* limits = client.Limits();
      limits && reserved > limits->max_prompt_tokens)
    return Error(400, "client_prompt_limit_exceeded",
                 "Embedding exceeds cumulative client input limit");
  if (cancelled())
    return Error(409, "embedding_cancelled", "Embedding request was cancelled");
  const auto admitted = rate.Reserve(clients, client, reserved, 0);
  ScopeExit settle([&] { rate.Settle(); });
  if (admitted != ClientRateResult::kAllowed) {
    const bool rpm = admitted == ClientRateResult::kRpm;
    const bool tpm = admitted == ClientRateResult::kTpm;
    const bool frozen = admitted == ClientRateResult::kFrozen;
    return Error(rpm || tpm ? 429
                 : frozen   ? 503
                            : 500,
                 rpm      ? "client_rpm_exhausted"
                 : tpm    ? "client_tpm_exhausted"
                 : frozen ? "service_draining"
                          : "client_rate_invalid",
                 "Embedding minute quota unavailable");
  }
  uint64_t completed = 0;
  std::vector<std::vector<float>> vectors;
  vectors.reserve(inputs.size());
  for (const auto& input : inputs) {
    if (cancelled())
      return Error(409, "embedding_cancelled", "Embedding request cancelled");
    EmbeddingStepResult step;
    usage.BeginStep();
    ScopeExit count([&] {
      if (step.committed_input > input.size()) {
        usage.Merge(std::nullopt);
      } else {
        usage.Observe({step.committed_input});
        completed += step.committed_input;
        rate.StartPrompt(completed);
      }
      usage.EndStep();
    });
    auto reply = execute(input, &step);
    if (!reply.ok()) return reply;
    if (cancelled())
      return Error(409, "embedding_cancelled", "Embedding request cancelled");
    if (step.committed_input != input.size() ||
        step.hidden.size() != plan.hidden_size)
      return Error(502, "embedding_result_invalid",
                   "Native embedding step did not complete its input");
    std::vector<float> vector;
    const auto status =
        NormalizeEmbeddingHidden(step.hidden, plan.dimensions, &vector);
    if (!status)
      return Error(502, "embedding_result_invalid", status.message());
    vectors.push_back(std::move(vector));
  }
  const auto status =
      BuildEmbeddingsResponse(plan, vectors, completed, response);
  return status ? ObjectReply{}
                : Error(502, "embedding_result_invalid", status.message());
}
}  // namespace q4t::server
