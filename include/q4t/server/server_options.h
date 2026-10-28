#pragma once

#include <vector>

#include "q4t/runtime/sequence_capacity.h"

#include <span>
#include <string>
#include <string_view>

#include "q4t/status.h"

namespace q4t::server {

// Parsing/queued requests have a separate bound from active GPU slots.
inline int TextAdmissionLimit(int slots) { return std::max(32, slots * 2); }

enum class StartupPlanMode { kAuto, kOff, kRequired };

struct ServerOptions {
  // Numeric IPv4 only. Remote access requires an explicit address.
  std::string host = "127.0.0.1";
  int port = 8000;
  std::string model_dir;
  int max_tokens = 256;  // default cap when the request omits max_tokens
  int max_prefill = 0;   // 0 = use ModelConfig default (2048); >0 overrides
  // Max sequence length (sizes the full-attention KV/indexer/rope caches).
  // 0 = derive capacity with the memory budget, or use ModelConfig's 8192
  // when --no-budget is set. Positive requests may be capped by the budget.
  int max_len = 0;
  // Max concurrent sequences (per-sequence recurrent-state pool size). Each
  // in-flight request owns one seq_id; the model's SSM/conv/PLE-conv/KV/indexer
  // state is pooled [max_seq, ...] so concurrent requests are isolated.
  int max_seq = 1;
  std::vector<int> seq_capacities;  // Empty keeps uniform slot capacity.
  // MTP speculative decode is the default (k=3, ~2x decode speedup, five-tier
  // E2E accepted 2026-10-14); --no-mtp opts back to plain greedy decode.
  bool no_mtp = false;
  bool mtp_suffix = false;  // Explicit request-local text, fixed-k3 suffix.
  std::string session_cache_dir;  // Explicit bounded same-process SSD cache.
  std::string prefix_cache_dir;  // Separate bounded, scope-keyed shared pool.
  std::vector<int> prefix_cache_checkpoints{8192, 32768, 65536};
  std::string mtp_subset_head;  // Explicit experimental BF16 artifact.
  // MTP speculative steps k (draft tokens per step); only meaningful when
  // MTP is enabled. Serve default 3 (measured optimum, see docs/LOG.md).
  int mtp_k = 3;
  // Experimental media path; default service contract is text-only.
  bool allow_media = false;
  // Startup memory budget (vllm-style gpu_memory_utilization). The server
  // reserves mem_fraction x MemTotal for the whole engine (weights + state
  // pools + per-request headroom) and caps max_len/max_seq to what fits, so
  // many oversized configurations can be capped before allocation. This is
  // not a bound on media decoding or other processes. The default is 0.90.
  // Set no_budget=true to disable the cap (legacy behavior, user's own risk).
  double mem_fraction = 0.90;
  bool no_budget = false;
  // Auto reserves supported text startup resources before pinned loading.
  // Off preserves the existing allocator; required rejects unsupported modes.
  StartupPlanMode startup_plan = StartupPlanMode::kAuto;
  // Controlled full-request observation; empty directory means zero capture.
  std::string moe_trace_dir;
  std::string moe_trace_workload;
  int moe_trace_max_mib = 1024;
  // Tiered expert residency: C resident slots per MoE layer. 0 (default) =
  // all 512 experts resident (baseline, bit-identical path). C > 0 loads C
  // slots per layer and streams missing experts from NVMe on demand.
  int moe_resident_slots = 0;
  // Optional per-layer static hot list: JSON object mapping layer id (string)
  // to an array of expert ids, used to fill the initial slots. Empty = none.
  std::string moe_hot_list;
  // Protect the hot-list experts from LRU eviction (the dynamic capacity
  // becomes C - hot). Requires moe_resident_slots > 0 and a hot list.
  bool moe_hot_protect = false;
  // Default per-request deadline in milliseconds (arrival -> cancellation).
  // 1200000 (20 min) is the historical default; raise it for workloads whose
  // first request carries long on-demand expert loads (tiered residency).
  int request_deadline_ms = 1200000;
};

// Derived once from validated options, never a second configuration source.
struct ServerCapabilities {
  bool mtp;
  bool media;
  bool multiple_sequences;
  bool Experimental() const { return mtp || media || multiple_sequences; }
};

ServerCapabilities CapabilitiesFor(const ServerOptions& options);
Status ValidateServerOptions(const ServerOptions& options);
// Parse transactionally; invalid arguments leave the supplied options intact.
Status ParseServerOptions(std::span<const std::string_view> args,
                          ServerOptions* options);

}  // namespace q4t::server
