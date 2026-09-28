#pragma once

#include <span>
#include <string>
#include <string_view>

#include "q4t/status.h"

namespace q4t::server {

struct ServerOptions {
  // Numeric IPv4 only. Remote access requires an explicit address.
  std::string host = "127.0.0.1";
  int port = 8000;
  std::string model_dir;
  int max_tokens = 256;  // default cap when the request omits max_tokens
  int max_prefill = 0;   // 0 = use ModelConfig default (2048); >0 overrides
  // Max sequence length (sizes the full-attention KV/indexer/rope caches).
  // 0 = use ModelConfig default (8192); >0 overrides (e.g. 262144 for the
  // model's full context; see PHASES.md 262K memory budget — use max_seq=1).
  int max_len = 0;
  // Max concurrent sequences (per-sequence recurrent-state pool size). Each
  // in-flight request owns one seq_id; the model's SSM/conv/PLE-conv/KV/indexer
  // state is pooled [max_seq, ...] so concurrent requests are isolated.
  int max_seq = 1;
  // Plain greedy decode is the default; MTP requires explicit opt-in.
  bool no_mtp = true;
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
  // Controlled full-request observation; empty directory means zero capture.
  std::string moe_trace_dir;
  std::string moe_trace_workload;
  int moe_trace_max_mib = 1024;
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
