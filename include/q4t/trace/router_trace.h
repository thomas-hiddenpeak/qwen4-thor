// Host-only trace evidence. These types observe execution; they do not drive
// it.
#pragma once

#include <array>
#include <cstdint>
#include <iosfwd>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::trace {

using Digest = std::array<uint8_t, 32>;
struct RouterTraceConfig {
  uint32_t layers = 0;
  uint32_t experts = 0;
  uint32_t top_k = 0;
  uint32_t max_rows = 0;
  // Hash file bytes: executable, model manifest, workload manifest.
  Digest binary_sha256{};
  Digest model_sha256{};
  Digest workload_sha256{};
};

enum class RouteStage : uint32_t { kPrefill = 1, kDecode = 2 };
enum class RequestOutcome : uint32_t {
  kSuccess = 1,
  kCancelled = 2,
  kFailed = 3
};
enum class RouteRecord : uint32_t {
  kRequestBegin = 1,
  kForwardBegin = 2,
  kLayer = 3,
  kForwardEnd = 4,
  kRequestEnd = 5,
  kRunEnd = 6
};

// Fields used by each record are encoded explicitly, never as a native struct.
// RequestBegin: request_id, prompt_rows, prompt_sha256 (SHA256 of actual
// input token IDs encoded as contiguous little-endian uint32 values).
// ForwardBegin: forward_id, stage, position, rows (inside current request).
// Layer: layer, expert_ids in original [row, top-k] order.
// ForwardEnd: submission_ok, gpu_complete, committed (independent facts).
// RequestEnd: outcome, output_tokens (not inferred from decode rows).
struct RouteEvent {
  RouteRecord kind = RouteRecord::kRunEnd;
  uint64_t request_id = 0;
  uint64_t prompt_rows = 0;
  Digest prompt_sha256{};
  uint64_t forward_id = 0;
  RouteStage stage = RouteStage::kPrefill;
  uint64_t position = 0;
  uint32_t rows = 0;
  uint32_t layer = 0;
  std::span<const uint16_t> expert_ids;
  bool submission_ok = false;
  bool gpu_complete = false;
  bool committed = false;
  RequestOutcome outcome = RequestOutcome::kFailed;
  uint64_t output_tokens = 0;
};

struct TraceSummary {
  uint64_t records = 0;
  uint64_t requests = 0;
  uint64_t successful_requests = 0;
  uint64_t cancelled_requests = 0;
  uint64_t failed_requests = 0;
  uint64_t committed_forwards = 0;
  uint64_t committed_prefill_rows = 0;
  uint64_t committed_decode_rows = 0;
  uint64_t route_ids = 0;  // Includes diagnostic records of failed forwards.
  uint64_t output_tokens = 0;
};

// Sequential full-request capture only. No sampling, gaps or packed requests.
// Errors are sticky: a broken stream cannot be relabelled complete by retrying.
class RouterTraceValidator {
 public:
  explicit RouterTraceValidator(const RouterTraceConfig& config);
  Status Observe(const RouteEvent& event);
  Status Finish() const;
  const TraceSummary& summary() const { return summary_; }

 private:
  Status Fail(const char* message);
  RouterTraceConfig config_;
  Status status_;
  TraceSummary summary_;
  bool request_open_ = false;
  bool forward_open_ = false;
  bool ended_ = false;
  bool decode_started_ = false;
  bool request_broken_ = false;
  bool gpu_unresolved_ = false;
  uint64_t last_request_id_ = 0;
  uint64_t last_forward_id_ = 0;
  uint64_t prompt_rows_ = 0;
  uint64_t committed_position_ = 0;
  RouteStage stage_ = RouteStage::kPrefill;
  uint32_t rows_ = 0;
  uint32_t next_layer_ = 0;
};

// Offline/worker-only IO; never call on the inference thread. Version 1 is
// little-endian, framed, CRC32 checked, with bounded record allocation.
class RouterTraceWriter {
 public:
  RouterTraceWriter(std::ostream& output, const RouterTraceConfig& config);
  Status Write(const RouteEvent& event);
  Status Finish();  // Requires explicit RunEnd and checks flush errors.

 private:
  std::ostream& output_;
  RouterTraceValidator validator_;
  Status status_;
  uint64_t sequence_ = 0;
};

// Rejects empty/truncated/oversized/extra data and unfinished lifecycles.
// Does not prove that recorded GPU completion or model identity is truthful;
// that binding belongs to the future collector and its runtime regression.
Status CheckRouterTrace(std::istream& input, TraceSummary* summary);

}  // namespace q4t::trace
