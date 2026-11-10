#include "q4t/server/model_preparation.h"

#include <algorithm>
#include <limits>

#include "object_protocol.h"

namespace q4t::server {
ObjectReply ModelPreparation::ReservePrompt(uint64_t tokens) {
  using object_detail::Error;
  if (pending_ || !tokens || !preparation_output_ ||
      preparation_output_ > kMaxPreparationOutputTokens ||
      auxiliary_output_ > kMaxAuxiliaryOutputTokens ||
      output_ >= preparation_output_ || tokens > UINT64_MAX - input_)
    return Error(500, "file_preparation_state_invalid",
                 "Invalid internal model preparation state");
  if (const auto* limits = identity_.Limits();
      limits && input_ + tokens > limits->max_prompt_tokens)
    return Error(400, "client_prompt_limit_exceeded",
                 "File preparation exceeds cumulative client input limit");
  const auto result =
      reserved_ ? rate_.ExtendPrompt(clients_, identity_, tokens)
                : rate_.Reserve(clients_, identity_, tokens, public_output_,
                                ClientClock::now(), auxiliary_output_,
                                preparation_output_);
  if (result != ClientRateResult::kAllowed) {
    const bool rpm = result == ClientRateResult::kRpm;
    const bool tpm = result == ClientRateResult::kTpm;
    const bool frozen = result == ClientRateResult::kFrozen;
    return Error(rpm || tpm ? 429
                 : frozen   ? 503
                            : 500,
                 rpm      ? "client_rpm_exhausted"
                 : tpm    ? "client_tpm_exhausted"
                 : frozen ? "service_draining"
                          : "client_rate_invalid",
                 "File preparation minute quota unavailable");
  }
  reserved_ = pending_ = true;
  pending_input_ = tokens;
  return {};
}
bool ModelPreparation::StartStep() noexcept {
  if (!pending_ || started_) return false;
  input_ += pending_input_;
  rate_.StartPrompt(input_);
  started_ = true;
  return true;
}
void ModelPreparation::ObserveOutput(uint64_t tokens) noexcept {
  if (!started_) return;
  step_output_ = std::max(step_output_, tokens);
  // An executor bug must not wrap or hide actual usage above its reservation.
  rate_.Completion(step_output_ > UINT64_MAX - output_
                       ? UINT64_MAX
                       : output_ + step_output_);
}
void ModelPreparation::CloseStep() noexcept {
  if (started_)
    output_ = step_output_ > UINT64_MAX - output_ ? UINT64_MAX
                                                  : output_ + step_output_;
  pending_ = started_ = false;
  pending_input_ = step_output_ = 0;
}
}  // namespace q4t::server
