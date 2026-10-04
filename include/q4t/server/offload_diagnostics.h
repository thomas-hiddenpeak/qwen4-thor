// Bounded, opt-in host snapshots at actual single-request stage boundaries.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "q4t/model/model.h"

namespace q4t::server {

// No GPU calls, dispatch changes, payload reads or counter resets. Call
// Capture only under model_mu_, after existing GPU completion boundaries.
// One request owns the sole sequence slot until Emit has finished.
class OffloadDiagnostics {
 public:
  explicit OffloadDiagnostics(std::string request_id);
  void SetInputTokens(int count) { input_tokens_ = count; }
  void Capture(const model::Model& model, const char* event,
               uint64_t decode_forwards, bool full_cache,
               bool gpu_work_complete);
  void Emit(const char* outcome, uint64_t output_tokens,
            uint64_t decode_forwards) const;

 private:
  struct Snapshot {
    std::string event;
    uint64_t decode_forwards = 0;
    uint64_t monotonic_ns = 0, realtime_ns = 0;
    uint64_t end_monotonic_ns = 0, end_realtime_ns = 0;
    std::optional<uint64_t> pid_read_bytes;
    bool gpu_work_complete = false;
    bool full_cache = false;
    quant::MoEResidency::Stats stats;
    model::ResidencyTimingSnapshot timing;
    uint64_t merge_count = 0, merge_ns = 0, merge_max_ns = 0;
    std::vector<quant::MoEResidency::DiagnosticState> layers;
  };
  std::string request_id_;
  std::optional<int> input_tokens_;
  std::vector<Snapshot> snapshots_;
};

}  // namespace q4t::server
