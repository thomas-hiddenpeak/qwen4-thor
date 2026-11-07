// Permanent model dependency faults survive request and scheduler boundaries.
#pragma once

#include <atomic>

namespace q4t::runtime {

enum class ModelFault { kNone, kPleRead, kPleUpload };

inline const char* ModelFaultName(ModelFault fault) noexcept {
  switch (fault) {
    case ModelFault::kNone:
      return "none";
    case ModelFault::kPleRead:
      return "ple_read_failed";
    case ModelFault::kPleUpload:
      return "ple_upload_failed";
  }
  return "unknown";
}

// No reset: only a new dependency instance starts healthy. Success, request
// cancellation, memory recovery and telemetry observations cannot clear this.
// The diagnostic string stays with the serialized owner; observers read only
// this atomic code and never acquire the model execution lock.
class ModelHealth {
 public:
  ModelFault Fault() const noexcept { return fault_.load(); }
  void Fail(ModelFault fault) noexcept {
    if (fault == ModelFault::kNone) return;
    ModelFault expected = ModelFault::kNone;
    fault_.compare_exchange_strong(expected, fault);
  }

 private:
  std::atomic<ModelFault> fault_{ModelFault::kNone};
};

}  // namespace q4t::runtime
