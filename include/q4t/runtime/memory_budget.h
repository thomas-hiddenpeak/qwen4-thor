// Allocation estimate used to choose a startup capacity.
//
// A feasible result fits the modeled allocations in mem_fraction x MemTotal.
// It is not a physical RAM limit or an OOM guarantee: file cache, allocator
// retention, driver ownership and other processes require separate accounting.
// Callers must reject an infeasible result; zero capacity is never a fallback.
// MTP admission covers single-sequence text with k=3. It includes the supplied
// independent draft weights, loading host peak, scratch, checkpoints and the
// request's draft-extend allocations. Other MTP configurations fail closed.
//
// The model accounts for:
//   - A supplied weight estimate (an index total may include unused tensors)
//   - Fixed forward workspace + buffers (sized for max_prefill)
//   - State pools that scale with max_len x max_seq (KV, indexer, rope, SSM,
//     conv, PLE conv)
//   - MTP loading and request peaks, evaluated as separate phases
#pragma once

#include <cstddef>
#include <string>

namespace q4t {
namespace runtime {

// Model architecture parameters for memory budget computation.
// Defaults match qwen4_exp (Qwen3.8-Flash-Next-NVFP4-SSD-Stream).
struct BudgetModelParams {
  int num_layers = 48;
  int hs = 2560;
  int hc = 4;
  int vocab = 248320;
  int max_prefill = 8192;
  // Full-attention (QSA) architecture.
  int full_attn_every = 4;  // every Nth layer is full-attention
  int nkv = 2;              // GQA key/value heads
  int hd = 256;             // head dimension
  int idx_hd = 128;         // indexer head dimension
  // Linear-attention (GDN) architecture.
  int nv = 48;         // value heads
  int kd = 128;        // key dimension
  int vd = 128;        // value dimension
  int in_qkv = 10240;  // QKV input dimension
  int conv_k = 4;      // causal conv kernel size
  // PLE (n-gram embedding) architecture.
  int ple_conv_state_len = 9;  // (conv_kernel-1) * dilation
  bool has_ple = true;
  int ple_capacity_tokens = 8192;  // PLE working-memory capacity
  int ple_row_bytes = 160;         // FP8 row size
  int ple_heads = 16;              // (ngram_size - 1) * heads_per_ngram
  // MTP draft model.
  bool has_mtp = false;
  int mtp_full_attn_layers = 1;  // MTP has 1 full-attention layer
  int mtp_experts = 512;
  int mtp_moe_is = 640;

  int full_attn_layers() const { return num_layers / full_attn_every; }
  int linear_attn_layers() const { return num_layers - full_attn_layers(); }
  int hc_dim() const { return hc * hs; }
  int ple_embed_dim() const { return ple_heads * ple_row_bytes; }
};

// User-requested configuration (from CLI flags).
struct BudgetRequest {
  double mem_fraction = 0.0;  // 0 = default 0.90, not a disabled budget
  int max_len = 0;            // 0 = auto-derive from budget
  int max_seq = 8;            // max concurrent sequences
  int max_prefill = 8192;     // max tokens per forward pass
  // Existing runtime sizing functions can supply these without allocation.
  // Main zero uses a scaled reference estimate. MTP requires its exact
  // workspace and independent index/staging sizes. MTP fields are ignored
  // when has_mtp is false.
  size_t main_workspace_bytes = 0;
  size_t mtp_workspace_bytes = 0;
  size_t mtp_weights_bytes =
      0;  // independent MTP index, excluding main weights
  size_t mtp_load_host_bytes = 0;  // largest simultaneous loader host staging
  int mtp_k = 3;  // scratch rows = k+1; main recurrent checkpoints = k
};

// Computed memory budget (result of ComputeMemoryBudget).
struct MemoryBudget {
  size_t mem_total = 0;  // total system memory (bytes)
  size_t budget = 0;     // mem_fraction x mem_total (bytes)
  size_t weights = 0;    // supplied weight estimate
  size_t fixed = 0;      // weights + workspace + buffers + margin
  size_t state_pool =
      0;  // KV/indexer/SSM/conv/rope (scales with max_len x max_seq)
  size_t per_request = 0;  // MTP prompt trunk, extend buffers and host scratch
  size_t available_for_state = 0;  // budget - fixed - max(request, loading)
  size_t main_workspace = 0;
  size_t mtp_workspace = 0;
  size_t mtp_weights = 0;
  size_t mtp_buffers = 0;  // LoadMtp single-step sample/trunk/logits
  size_t mtp_scratch = 0;  // MtpReserveScratch, including its B=1 multi buffers
  size_t mtp_checkpoints = 0;   // main SSM/conv/PLE rollback checkpoints
  size_t mtp_forward_temp = 0;  // allocations outside the MTP workspace
  size_t loading_host_peak = 0;
  size_t forward_buffers = 0;
  size_t ple_working = 0;
  size_t runtime_peak = 0;
  size_t startup_peak = 0;
  size_t estimated_total = 0;  // max(runtime_peak, startup_peak)
  int requested_max_len = 0;
  int requested_max_seq = 0;
  int requested_max_prefill = 0;
  int max_prefill = 0;
  int max_len = 0;        // derived (or capped) max sequence length
  int max_seq = 0;        // derived (or capped) max concurrent sequences
  bool capped = false;    // true if a user value was reduced to fit
  bool feasible = false;  // false => max_len=max_seq=0; do not load
  std::string reason;
  std::string report;  // human-readable budget report (for stderr logging)
};

// Compute the memory budget.
//
// `params` describes the model architecture. `req` is the user's request
// (mem_fraction, max_len, max_seq). `weights_bytes` is the supplied weight
// estimate (e.g. WeightIndex::total_size()). `mem_total` is the system's total
// memory in bytes (from /proc/meminfo MemTotal or sysconf).
//
// The function:
//  1. Computes the estimate ceiling (fraction defaults to 0.90 if zero).
//  2. Subtracts fixed costs (weights + workspace + buffers).
//  3. Derives the maximum (max_len, max_seq) that fits in the remaining budget,
//     including the larger of MTP loading and request allocation peaks.
//  4. If the user specified max_len and/or max_seq, caps them to what fits
//     (setting `capped = true` if any value was reduced).
//  5. Returns feasible=false if no supported capacity fits, with a report
//     that separates requested capacity from the effective estimate.
MemoryBudget ComputeMemoryBudget(const BudgetModelParams& params,
                                 const BudgetRequest& req, size_t weights_bytes,
                                 size_t mem_total);

// Read MemAvailable from /proc/meminfo (bytes). Returns 0 on failure.
// Used for runtime OOM preflight checks.
size_t ReadMemAvailable();

// Read MemFree from /proc/meminfo (bytes). Returns 0 on failure. Unlike
// MemAvailable this excludes the reclaimable page cache: it is the memory the
// kernel can hand to a fresh cudaMalloc WITHOUT first evicting cache, so it is
// the right gauge for the OOM preflight (the OOM killer reacts to MemFree).
size_t ReadMemFree();

// Read MemTotal from /proc/meminfo (bytes). Returns 0 on failure.
size_t ReadMemTotal();

}  // namespace runtime
}  // namespace q4t
