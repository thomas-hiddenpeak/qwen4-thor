#include "q4t/server/model_preparation.h"

#include <algorithm>
#include <limits>

#include "object_protocol.h"

namespace q4t::server {
bool ModelPreparation::ContinueReservation(uint64_t admitted_input,
                                           uint64_t completed_output) noexcept {
  if (reserved_ || pending_ || started_ || input_ || output_ ||
      admitted_input > UINT64_MAX / 2 || completed_output > UINT64_MAX / 2)
    return false;
  prior_input_ = admitted_input;
  prior_output_ = completed_output;
  reserved_ = true;
  return true;
}
bool ModelPreparation::ContinuePublicGeneration(uint64_t admitted_input,
                                               uint64_t completed_output) noexcept {
  if (!preparation_output_ || preparation_output_ > public_output_ ||
      !ContinueReservation(admitted_input, completed_output))
    return false;
  shared_public_output_ = true;
  return true;
}
ObjectReply ModelPreparation::ReservePrompt(uint64_t tokens) {
  using object_detail::Error;
  if (pending_ || !tokens || !preparation_output_ ||
      (!shared_public_output_ && preparation_output_ > kMaxPreparationOutputTokens) ||
      auxiliary_output_ > kMaxAuxiliaryOutputTokens ||
      output_ >= preparation_output_ || input_ > UINT64_MAX - prior_input_ ||
      tokens > UINT64_MAX - prior_input_ - input_)
    return Error(500, "file_preparation_state_invalid",
                 "Invalid internal model preparation state");
  if (const auto* limits = identity_.Limits();
      limits && prior_input_ + input_ + tokens > limits->max_prompt_tokens)
    return Error(400, "client_prompt_limit_exceeded",
                 "Model input exceeds the cumulative client prompt limit");
  const auto result =
      reserved_ ? rate_.ExtendPrompt(clients_, identity_, tokens)
                : rate_.Reserve(clients_, identity_, tokens, public_output_,
                                ClientClock::now(), auxiliary_output_,
                                preparation_output_);
  switch (result) {
    case ClientRateResult::kFrozen:
      return Error(503, "service_draining",
                   "Service is draining and cannot admit another model step");
    case ClientRateResult::kRpm:
      return Error(429, "client_rpm_exhausted",
                   "Client request rate limit reached");
    case ClientRateResult::kTpm:
      return Error(429, "client_tpm_exhausted",
                   "Client token rate limit reached");
    case ClientRateResult::kInvalid:
      return Error(500, "client_rate_invalid",
                   "Invalid model step rate reservation");
    case ClientRateResult::kAllowed:
      break;
  }
  reserved_ = pending_ = true;
  pending_input_ = tokens;
  return {};
}
bool ModelPreparation::StartStep() noexcept {
  if (!pending_ || started_) return false;
  input_ += pending_input_;
  rate_.StartPrompt(prior_input_ + input_);
  started_ = true;
  return true;
}
void ModelPreparation::ObserveOutput(uint64_t tokens) noexcept {
  if (!started_) return;
  step_output_ = std::max(step_output_, tokens);
  // An executor bug must not wrap or hide actual usage above its reservation.
  const auto current =
      step_output_ > UINT64_MAX - output_ ? UINT64_MAX : output_ + step_output_;
  rate_.Completion(current > UINT64_MAX - prior_output_
                       ? UINT64_MAX
                       : prior_output_ + current);
}
void ModelPreparation::CloseStep() noexcept {
  if (started_)
    output_ = step_output_ > UINT64_MAX - output_ ? UINT64_MAX
                                                  : output_ + step_output_;
  pending_ = started_ = false;
  pending_input_ = step_output_ = 0;
}
}  // namespace q4t::server
