// Memory budget model for OOM-safe startup configuration.
//
// Computes the maximum (max_len, max_seq) that fits within a memory budget
// (mem_fraction x MemTotal), given the model's weight size and architecture.
// This is the vllm-style "gpu_memory_utilization" equivalent for Jetson Thor's
// unified memory: even if the user configures an aggressive max_len/max_seq,
// the server caps it to what actually fits, preventing OOM crashes.
//
// The model accounts for:
//   - GPU weights (from WeightIndex::total_size)
//   - Fixed forward workspace + buffers (sized for max_prefill)
//   - State pools that scale with max_len x max_seq (KV, indexer, rope, SSM,
//     conv, PLE conv)
//   - Per-request headroom (trunk buffer + MTP draft logits)
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
  int nv = 48;              // value heads
  int kd = 128;             // key dimension
  int vd = 128;             // value dimension
  int in_qkv = 10240;       // QKV input dimension
  int conv_k = 4;           // causal conv kernel size
  // PLE (n-gram embedding) architecture.
  int ple_conv_state_len = 9;     // (conv_kernel-1) * dilation
  int ple_embed_dim = 1280;       // heads_per_ngram * row_bytes
  int ple_capacity_tokens = 8192; // PLE working-memory capacity
  int ple_row_bytes = 160;        // FP8 row size
  int ple_heads = 8;              // ngram heads
  // MTP draft model.
  bool has_mtp = true;
  int mtp_full_attn_layers = 1;   // MTP has 1 full-attention layer

  int full_attn_layers() const { return num_layers / full_attn_every; }
  int linear_attn_layers() const { return num_layers - full_attn_layers(); }
  int hc_dim() const { return hc * hs; }
};

// User-requested configuration (from CLI flags).
struct BudgetRequest {
  double mem_fraction = 0.0;  // 0 = disabled (use all available memory)
  int max_len = 0;            // 0 = auto-derive from budget
  int max_seq = 8;            // max concurrent sequences
  int max_prefill = 8192;     // max tokens per forward pass
};

// Computed memory budget (result of ComputeMemoryBudget).
struct MemoryBudget {
  size_t mem_total = 0;           // total system memory (bytes)
  size_t budget = 0;              // mem_fraction x mem_total (bytes)
  size_t weights = 0;             // GPU weights (WeightIndex::total_size)
  size_t fixed = 0;               // workspace + buffers (max_len/max_seq independent)
  size_t state_pool = 0;          // KV/indexer/SSM/conv/rope (scales with max_len x max_seq)
  size_t per_request = 0;         // trunk buffer + draft logits (per-request peak)
  size_t available_for_state = 0; // budget - weights - fixed
  int max_len = 0;                // derived (or capped) max sequence length
  int max_seq = 0;                // derived (or capped) max concurrent sequences
  bool capped = false;            // true if a user value was reduced to fit
  std::string report;             // human-readable budget report (for stderr logging)
};

// Compute the memory budget.
//
// `params` describes the model architecture. `req` is the user's request
// (mem_fraction, max_len, max_seq). `weights_bytes` is the total GPU weight
// size (from WeightIndex::total_size()). `mem_total` is the system's total
// memory in bytes (from /proc/meminfo MemTotal or sysconf).
//
// The function:
//  1. Computes the memory budget (mem_fraction x mem_total, or mem_total if 0).
//  2. Subtracts fixed costs (weights + workspace + buffers).
//  3. Derives the maximum (max_len, max_seq) that fits in the remaining budget,
//     accounting for per-request headroom (trunk buffer + MTP draft logits).
//  4. If the user specified max_len and/or max_seq, caps them to what fits
//     (setting `capped = true` if any value was reduced).
//  5. Produces a human-readable report for startup logging.
MemoryBudget ComputeMemoryBudget(const BudgetModelParams& params,
                                  const BudgetRequest& req,
                                  size_t weights_bytes,
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
