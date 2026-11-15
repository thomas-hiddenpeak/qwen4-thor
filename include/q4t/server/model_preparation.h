// Request-owned accounting for actual model work used to prepare inputs.
#pragma once

#include "q4t/server/client_policy.h"
#include "q4t/server/object_store.h"

namespace q4t::server {

class ModelPreparation {
 public:
  ModelPreparation(ClientAdmission& clients, ClientIdentity identity,
                   ClientRateLease& rate, uint64_t public_output,
                   uint64_t auxiliary_output, uint64_t preparation_output)
      : clients_(clients),
        identity_(std::move(identity)),
        rate_(rate),
        public_output_(public_output),
        auxiliary_output_(auxiliary_output),
        preparation_output_(preparation_output) {}
  // Actual tokenized input, before any model admission. The first call reserves
  // all output and one RPM; later calls extend input on that same lease.
  ObjectReply ReservePrompt(uint64_t tokens);
  // A native tool may return a spreadsheet after primary generation. The
  // owning handler has already reserved this preparation output on the same
  // RPM/TPM lease. Extend only input, preserving all previously observed work.
  bool ContinueReservation(uint64_t admitted_input,
                           uint64_t completed_output) noexcept;
  bool StartStep() noexcept;
  // Cumulative tokens from the current step, including a failed/cancelled one.
  void ObserveOutput(uint64_t tokens) noexcept;
  void CloseStep() noexcept;
  bool Reserved() const { return reserved_; }
  uint64_t InputTokens() const { return input_; }
  uint64_t OutputTokens() const { return output_; }
  ClientRateLease& Rate() { return rate_; }

 private:
  ClientAdmission& clients_;
  ClientIdentity identity_;
  ClientRateLease& rate_;
  uint64_t public_output_, auxiliary_output_, preparation_output_;
  uint64_t input_ = 0, output_ = 0, pending_input_ = 0, step_output_ = 0;
  uint64_t prior_input_ = 0, prior_output_ = 0;
  bool reserved_ = false, pending_ = false, started_ = false;
};
}  // namespace q4t::server
