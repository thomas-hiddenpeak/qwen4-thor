#include "q4t/runtime/memory_budget.h"

#include <algorithm>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>

namespace q4t {
namespace runtime {
namespace {

// ---------------------------------------------------------------------------
// Measured fixed costs (Jetson AGX Thor, max_prefill=8192), 2026-09-19.
// Methodology: linked the real workspace-sizing functions
// (ModelHeadWorkspaceBytes / DecoderLayerWorkspaceBytes) and summed the
// persistent forward buffers allocated in LoadModel. See
// docs/log/2026-09-19.md. The max_prefill-proportional parts are expressed as
// formulas below so they track a changed max_prefill; the two workspace
// constants are measured at 8192 (workspace grows ~linearly in max_prefill).
// ---------------------------------------------------------------------------
constexpr size_t kMainWorkspaceBytes = 2683503000;  // d_ws (max layer, @8192)
constexpr size_t kMtpWorkspaceBytes = 5000000000;   // MTP draft forward ws (est @8192)
constexpr size_t kPleWorkingBytes = 75170000;       // PLE staging + gpu_fp8 + ring
constexpr size_t kContextMarginBytes = 2000000000;  // CUDA context + system headroom
constexpr size_t kSchedulerPerSeqBytes = 500000;    // d_sched_logits [1, vocab]/seq

// Per full-attention layer, per sequence, per token (BF16 KV + int page_table
// + BF16 indexer raw/comp). Mirrors the allocation in decoder_layer.cu.
size_t FullAttnPerTokenBytes(const BudgetModelParams& p) {
  return static_cast<size_t>(p.nkv) * 2u * static_cast<size_t>(p.hd) * 2u +
         4u + static_cast<size_t>(p.idx_hd) * 2u +
         static_cast<size_t>(p.idx_hd) * 2u;
}
// d_rope_pos is [max_seq, 3, max_len] int32 -> 3 ints per token per seq.
size_t RopePerTokenBytes() { return 3u * sizeof(int); }
// Per linear-attention layer, per sequence (O(1), independent of max_len).
size_t LinearPerSeqBytes(const BudgetModelParams& p) {
  return static_cast<size_t>(p.nv) * static_cast<size_t>(p.kd) *
             static_cast<size_t>(p.vd) * 4u +
         static_cast<size_t>(p.in_qkv) * static_cast<size_t>(p.conv_k - 1) * 2u;
}
// PLE short-conv state (single PLE layer): hc_dim * state_len * 2 (BF16).
size_t PleConvPerSeqBytes(const BudgetModelParams& p) {
  return static_cast<size_t>(p.hc_dim()) *
         static_cast<size_t>(p.ple_conv_state_len) * 2u;
}

// Total pooled state for (max_len, max_seq). The full-attention KV/indexer and
// rope scale with max_len x max_seq; the linear SSM/conv and PLE-conv state
// scale with max_seq only.
size_t StatePoolBytes(const BudgetModelParams& p, int max_len, int max_seq) {
  const size_t full_layers =
      static_cast<size_t>(p.full_attn_layers()) +
      (p.has_mtp ? static_cast<size_t>(p.mtp_full_attn_layers) : 0u);
  const size_t per_token = full_layers * FullAttnPerTokenBytes(p) +
                           RopePerTokenBytes();
  const size_t per_seq_fixed =
      static_cast<size_t>(p.linear_attn_layers()) * LinearPerSeqBytes(p) +
      PleConvPerSeqBytes(p) + kSchedulerPerSeqBytes;
  return static_cast<size_t>(max_seq) *
         (static_cast<size_t>(max_len) * per_token + per_seq_fixed);
}

// Per-request transient peak. Prefills serialize under model_mu_, so at most
// one request holds this at a time: the full-prompt trunk buffer (freed right
// after the MTP draft-extend) + the MTP draft logits (one max_prefill chunk).
size_t PerRequestBytes(const BudgetModelParams& p, int max_len) {
  return static_cast<size_t>(max_len) * static_cast<size_t>(p.hc_dim()) * 2u +
         static_cast<size_t>(p.max_prefill) * static_cast<size_t>(p.vocab) *
             2u;
}

// max_prefill-proportional forward buffers (d_ids/d_positions/d_emb/d_trunk/
// d_trunk2/d_ple_emb/...), measured 398.7 MB @ max_prefill=8192.
size_t ForwardBufferBytes(const BudgetModelParams& p) {
  const size_t per_token =
      4u + 4u + 4u + 4u +            // ids, positions, token_seq_id, ragged
      static_cast<size_t>(p.hs) * 2u +  // d_emb
      4u +                            // d_img_pos
      static_cast<size_t>(p.hc_dim()) * 2u +  // d_trunk
      static_cast<size_t>(p.hc_dim()) * 2u +  // d_trunk2
      static_cast<size_t>(p.ple_embed_dim) * 2u;  // d_ple_emb
  return static_cast<size_t>(p.max_prefill) * per_token;
}

size_t FixedBytes(const BudgetModelParams& p, size_t weights) {
  return weights + kMainWorkspaceBytes + ForwardBufferBytes(p) +
         static_cast<size_t>(p.max_prefill) * static_cast<size_t>(p.vocab) *
             2u +  // d_prefill_logits [max_prefill, vocab]
         kMtpWorkspaceBytes + kPleWorkingBytes + kContextMarginBytes;
}

}  // namespace

MemoryBudget ComputeMemoryBudget(const BudgetModelParams& params,
                                  const BudgetRequest& req,
                                  size_t weights_bytes, size_t mem_total) {
  MemoryBudget b;
  b.mem_total = mem_total;
  const double frac = req.mem_fraction > 0.0 ? req.mem_fraction : 0.90;
  b.budget = static_cast<size_t>(static_cast<double>(mem_total) * frac);

  BudgetModelParams p = params;
  p.max_prefill = req.max_prefill > 0 ? req.max_prefill : params.max_prefill;

  b.weights = weights_bytes;
  b.fixed = FixedBytes(p, weights_bytes);

  const int kHardMaxLen = 262144;  // model context ceiling
  const int kMinLen = 2048;        // below this the server is not useful
  int max_seq = req.max_seq > 0 ? req.max_seq : 8;
  int max_len = 0;

  // Available for the state pool at a candidate max_len (trunk shrinks as
  // max_len shrinks, so this is len-dependent).
  auto available_for = [&](int len) -> size_t {
    const size_t fixed_total = b.fixed + PerRequestBytes(p, len);
    return b.budget > fixed_total ? b.budget - fixed_total : 0u;
  };

  if (req.max_len > 0) {
    // User pinned max_len: derive the max_seq that fits at that length.
    const size_t per_seq = StatePoolBytes(p, req.max_len, 1);
    const int derived = per_seq > 0
                            ? static_cast<int>(available_for(req.max_len) /
                                                per_seq)
                            : 0;
    if (derived >= 1) {
      max_len = req.max_len;
      max_seq = std::min(max_seq, derived);
      b.capped = max_seq < req.max_seq;
    } else {
      // Even one sequence overflows at this max_len: binary-search the largest
      // max_len that fits a single sequence (keep max_seq=1).
      int lo = kMinLen, hi = std::min(req.max_len, kHardMaxLen), best = 0;
      while (lo <= hi) {
        const int mid = lo + (hi - lo) / 2;
        if (StatePoolBytes(p, mid, 1) <= available_for(mid)) {
          best = mid;
          lo = mid + 1;
        } else {
          hi = mid - 1;
        }
      }
      max_len = best;
      max_seq = 1;
      b.capped = best < req.max_len;
    }
  } else {
    // Auto max_len (vllm-style): derive the length that fits the given max_seq.
    const size_t per_seq_fixed = StatePoolBytes(p, 0, 1);
    const size_t per_token = StatePoolBytes(p, 1, 1) - per_seq_fixed;
    int len = 0;
    for (int iter = 0; iter < 16; ++iter) {
      const size_t avail = available_for(len);
      if (avail <= static_cast<size_t>(max_seq) * per_seq_fixed) {
        len = 0;
        break;
      }
      len = static_cast<int>((avail / static_cast<size_t>(max_seq) -
                              per_seq_fixed) /
                             per_token);
    }
    len = std::min(len, kHardMaxLen);
    len = (len / 1024) * 1024;
    max_len = std::max(len, 1024);
  }

  b.max_len = max_len;
  b.max_seq = max_seq;
  b.state_pool = StatePoolBytes(p, max_len, max_seq);
  b.per_request = PerRequestBytes(p, max_len);
  b.available_for_state =
      b.budget > b.fixed + b.per_request
          ? b.budget - b.fixed - b.per_request
          : 0u;

  std::ostringstream os;
  os.precision(2);
  os << std::fixed
     << "[q4t][budget] mem_total=" << b.mem_total / 1e9 << " GB fraction="
     << frac << " budget=" << b.budget / 1e9 << " GB\n"
     << "[q4t][budget]   weights=" << b.weights / 1e9
     << " GB  fixed=" << b.fixed / 1e9
     << " GB (ws+buf+logits+mtp+ple+margin)\n"
     << "[q4t][budget]   per_request=" << b.per_request / 1e9
     << " GB (trunk+draft_logits)  state_pool=" << b.state_pool / 1e9
     << " GB\n"
     << "[q4t][budget]   => max_len=" << b.max_len << " max_seq=" << b.max_seq
     << (b.capped ? "  (CAPPED to fit budget)" : "") << "\n";
  b.report = os.str();
  return b;
}

size_t ReadMemAvailable() {
  std::ifstream f("/proc/meminfo");
  std::string line;
  while (std::getline(f, line)) {
    if (line.rfind("MemAvailable:", 0) == 0) {
      long kb = 0;
      std::sscanf(line.c_str(), "MemAvailable: %ld", &kb);
      return static_cast<size_t>(kb) * 1024u;
    }
  }
  return 0;
}

size_t ReadMemTotal() {
  std::ifstream f("/proc/meminfo");
  std::string line;
  while (std::getline(f, line)) {
    if (line.rfind("MemTotal:", 0) == 0) {
      long kb = 0;
      std::sscanf(line.c_str(), "MemTotal: %ld", &kb);
      return static_cast<size_t>(kb) * 1024u;
    }
  }
  return 0;
}

size_t ReadMemFree() {
  std::ifstream f("/proc/meminfo");
  std::string line;
  while (std::getline(f, line)) {
    if (line.rfind("MemFree:", 0) == 0) {
      long kb = 0;
      std::sscanf(line.c_str(), "MemFree: %ld", &kb);
      return static_cast<size_t>(kb) * 1024u;
    }
  }
  return 0;
}

}  // namespace runtime
}  // namespace q4t
