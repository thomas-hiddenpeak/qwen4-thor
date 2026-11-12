#pragma once

#include <cstdint>
#include <vector>

#include "q4t/runtime/sequence_capacity.h"

#include <span>
#include <string>
#include <string_view>

#include "q4t/status.h"

namespace q4t::server {

// Parsing/queued requests have a separate bound from active GPU slots.
inline int TextAdmissionLimit(int slots) { return std::max(32, slots * 2); }

struct AdmissionLimits {
  int inflight = 32;
  int queued = 31;
};

enum class StartupPlanMode { kAuto, kOff, kRequired };

struct ServerOptions {
  // Option names only, retained transactionally by the CLI parser. Values
  // such as model/cache paths are never included in configuration discovery.
  std::vector<std::string> supplied_options;
  // Numeric IPv4 only. Remote access requires an explicit address.
  std::string host = "127.0.0.1";
  std::string
      client_policy_file;  // Private immutable gateway policy, optional.
  std::string access_program_policy_file;  // Optional native policy snapshot.
  std::string file_input_policy_file;  // Optional trusted preparation policy.
  int port = 8000;
  // Optional independent local operations listener; 0 disables. Always
  // 127.0.0.1, with its own bounded handlers; never serves generation.
  int operations_port = 0;
  std::string model_dir;
  // Explicit optional trained embedding assets; never inferred/downloaded.
  std::string embedding_model_dir;
  std::string served_model_name = "qwen3.8-flash-next";
  std::vector<std::string> model_aliases;
  // Durable application state, independent of model assets and KV caches.
  // Empty resolves to XDG_STATE_HOME or HOME in the startup owner, without I/O.
  std::string object_store_dir;
  int object_max_object_mib = 16;
  int object_max_client_mib = 64;
  int object_max_total_mib = 256;
  int object_max_client_objects = 1000;
  int object_max_total_objects = 10000;
  int object_io_timeout_ms = 2000;
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
  // Internal auto sentinels; explicit CLI values are 1..4096 and 0..4096.
  // Inflight includes preflight/preparation/output; queued only slot waiters.
  int max_inflight_requests = 0;
  int max_queued_requests = -1;
  std::vector<int> seq_capacities;  // Empty keeps uniform slot capacity.
  // MTP speculative decode is the default (k=3, ~2x decode speedup, five-tier
  // E2E accepted 2026-10-14); --no-mtp opts back to plain greedy decode.
  bool no_mtp = false;
  bool mtp_suffix = false;  // Explicit request-local text, fixed-k3 suffix.
  std::string session_cache_dir;  // Explicit bounded isolated SSD cache.
  std::string prefix_cache_dir;   // Separate bounded, scope-keyed shared pool.
  std::vector<int> prefix_cache_checkpoints{8192, 32768, 65536};
  std::string mtp_subset_head;  // Explicit experimental BF16 artifact.
  // MTP speculative steps k (draft tokens per step); only meaningful when
  // MTP is enabled. Serve default 3 (measured optimum, see docs/LOG.md).
  int mtp_k = 3;
  // Experimental media path; default service contract is text-only.
  bool allow_media = false;
  // Startup allocation estimate: caps max_len/max_seq to fit the modeled
  // costs within mem_fraction x MemTotal. It reserves no physical memory.
  // File cache, driver ownership and other processes need separate accounting.
  // no_budget bypasses both sizing and the estimate-based startup check.
  double mem_fraction = 0.90;
  bool no_budget = false;
  // Process-lived quota for project-linked CUDA requested bytes. -1 = auto
  // mem_fraction x MemTotal; 0 explicitly disables; independent of no_budget.
  int cuda_allocation_limit_mib = -1;
  // Before large allocations require MemAvailable >= estimated_total + this
  // headroom. 0 disables the check; ignored with no_budget. Not a hard limit.
  int startup_memory_headroom_mib = 2048;
  // Runtime global MemAvailable admission, independent of startup sizing.
  // 0 disables the gate; reopening needs floor + recovery margin. MiB.
  int min_available_memory_mib = 2048;
  int memory_recovery_margin_mib = 512;
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
  // Logical retained payloads across background jobs, not an RSS estimate.
  int background_max_input_mib = 512;
  // Native Batch task/input capacity, independent of per-minute client rates.
  int batch_workers = 1;
  int batch_max_jobs = 8;
  int batch_max_client_jobs = 4;
  int batch_max_result_mib = 256;
  uint64_t batch_model_queue_tokens = 1ULL << 30;
  uint64_t batch_client_model_queue_tokens = 1ULL << 28;
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
// Resolve only after the startup memory budget has fixed effective slots.
// Failure leaves output unchanged. Does not reserve RAM or connections.
Status ResolveAdmissionLimits(const ServerOptions& options, int effective_slots,
                              AdmissionLimits* output);
// Parse transactionally; invalid arguments leave the supplied options intact.
Status ParseServerOptions(std::span<const std::string_view> args,
                          ServerOptions* options);

}  // namespace q4t::server
