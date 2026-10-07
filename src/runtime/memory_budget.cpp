#include "q4t/runtime/memory_budget.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <limits>
#include <sstream>
#include <string>

#include "q4t/ple/page_reader.h"

namespace q4t {
namespace runtime {
namespace {

// ---------------------------------------------------------------------------
// Reference workspace estimates at max_prefill=8192 (2026-09-19). The main
// value was measured; the MTP value is approximate. Serve supplies the real
// sizing functions instead. Scaling these fallback values is only an
// estimate because workspace sizing includes alignment and fixed scratch.
// ---------------------------------------------------------------------------
constexpr size_t kMainWorkspaceBytes = 2683503000;  // d_ws (max layer, @8192)
constexpr size_t kMtpWorkspaceBytes =
    5000000000;  // MTP draft forward ws (est @8192)
constexpr size_t kContextMarginBytes =
    2000000000;  // CUDA context + system headroom

size_t Add(size_t a, size_t b) {
  return b > std::numeric_limits<size_t>::max() - a
             ? std::numeric_limits<size_t>::max()
             : a + b;
}

size_t Mul(size_t a, size_t b) {
  return a && b > std::numeric_limits<size_t>::max() / a
             ? std::numeric_limits<size_t>::max()
             : a * b;
}
// d_rope_pos is [max_seq, 3, max_len] int32 -> 3 ints per token per seq.
size_t RopePerTokenBytes() { return 3u * sizeof(int); }
// Per linear-attention layer, per sequence (O(1), independent of max_len).
size_t LinearPerSeqBytes(const BudgetModelParams& p) {
  return Add(Mul(Mul(Mul(p.nv, p.kd), p.vd), 4u),
             Mul(Mul(p.in_qkv, p.conv_k - 1), 2u));
}
// PLE short-conv state (single PLE layer): hc_dim * state_len * 2 (BF16).
size_t PleConvPerSeqBytes(const BudgetModelParams& p) {
  return p.has_ple ? Mul(Mul(p.hc_dim(), p.ple_conv_state_len), 2u) : 0;
}

// Total pooled state for (max_len, max_seq). The full-attention KV/indexer and
// rope scale with max_len x max_seq; the linear SSM/conv and PLE-conv state
// scale with max_seq only.
size_t StatePoolBytes(const BudgetModelParams& p, int max_len, int max_seq) {
  const size_t full_layers =
      static_cast<size_t>(p.full_attn_layers()) +
      (p.has_mtp ? static_cast<size_t>(p.mtp_full_attn_layers) : 0u);
  // KV pages round up to 16 tokens; indexer/page tables use max_len itself.
  const size_t len = static_cast<size_t>(max_len);
  const size_t kv_len = (len + 15u) / 16u * 16u;
  const size_t full_state =
      Add(Mul(kv_len, Mul(Mul(p.nkv, p.hd), 4u)),
          Mul(len, 4u + static_cast<size_t>(p.idx_hd) * 4u));
  const size_t rope = Mul(len, RopePerTokenBytes() * (p.has_mtp ? 2u : 1u));
  const size_t per_seq_fixed = Add(
      Add(Mul(p.linear_attn_layers(), LinearPerSeqBytes(p)),
          PleConvPerSeqBytes(p)),
      // Scheduler and selected prefill logits, plus the device argmax token.
      static_cast<size_t>(p.vocab) * 2u * 2u + sizeof(int32_t) +
          // d_seq_id and the per-sequence part of d_ragged_seq_offset.
          2u * sizeof(int32_t));
  return Mul(static_cast<size_t>(max_seq),
             Add(Add(Mul(full_layers, full_state), rope), per_seq_fixed));
}

// One-request reference subtotal: full-prompt trunk plus draft chunk logits.
// Excludes concurrent overlap, independent draft weights and lazy scratch.
// Trunks can be allocated before model_mu_; this is not the full MTP peak.
size_t PerRequestBytes(const BudgetModelParams& p, int max_len) {
  if (!p.has_mtp) return 0;
  return Add(Mul(Mul(max_len, p.hc_dim()), 2u),
             Mul(Mul(p.max_prefill, p.vocab), 2u));
}

// max_prefill-proportional forward buffers (d_ids/d_positions/d_emb/d_trunk/
// d_trunk2/d_ple_emb/...) from LoadModel, including all PLE heads.
size_t ForwardBufferBytes(const BudgetModelParams& p) {
  const size_t per_token =
      4u + 4u + 4u + 4u +               // ids, positions, token_seq_id, ragged
      static_cast<size_t>(p.hs) * 2u +  // d_emb
      4u +                              // d_img_pos
      static_cast<size_t>(p.hc_dim()) * 2u +  // d_trunk
      static_cast<size_t>(p.hc_dim()) * 2u +  // d_trunk2
      (p.has_ple ? static_cast<size_t>(p.ple_embed_dim()) * 2u : 0u);
  // The final cu_seqlens entry is not part of the per-sequence state charge.
  return Add(Mul(static_cast<size_t>(p.max_prefill), per_token),
             sizeof(int32_t));
}

size_t PleWorkingBytes(const BudgetModelParams& p) {
  if (!p.has_ple) return 0;
  const size_t rows = Mul(p.ple_capacity_tokens, p.ple_heads);
  // Pinned rows + GPU rows + pinned row IDs + registered page pool/ring.
  // Dynamic piece/group vectors remain part of the uncalibrated margin.
  return Add(Add(Mul(rows, 2u * p.ple_row_bytes + sizeof(int64_t)),
                 ple::kDefaultPagePoolMiB * 1024u * 1024u),
             ple::kRingBytesEstimate);
}

size_t ReferenceWorkspace(size_t bytes_at_8192, int max_prefill) {
  return Add(Mul(bytes_at_8192, max_prefill), 8191u) / 8192u;
}

}  // namespace

MemoryBudget ComputeMemoryBudget(const BudgetModelParams& params,
                                 const BudgetRequest& req, size_t weights_bytes,
                                 size_t mem_total) {
  MemoryBudget b;
  b.mem_total = mem_total;
  b.requested_max_len = req.max_len;
  b.requested_max_seq = req.max_seq;
  b.requested_max_prefill = req.max_prefill;
  const double frac = req.mem_fraction == 0.0 ? 0.90 : req.mem_fraction;
  BudgetModelParams p = params;
  p.max_prefill = req.max_prefill > 0 ? req.max_prefill : params.max_prefill;
  b.max_prefill = p.max_prefill;
  const bool valid =
      std::isfinite(frac) && frac > 0.0 && frac <= 1.0 && mem_total > 0 &&
      req.max_len >= 0 && req.max_seq >= 0 && req.max_prefill >= 0 &&
      p.max_prefill > 0 && p.num_layers >= 0 && p.full_attn_every > 0 &&
      p.hs > 0 && p.hc > 0 && p.vocab > 0 &&
      p.hc <= std::numeric_limits<int>::max() / p.hs && p.nkv > 0 && p.hd > 0 &&
      p.idx_hd > 0 && p.nv > 0 && p.kd > 0 && p.vd > 0 && p.in_qkv > 0 &&
      p.conv_k > 0 && p.mtp_full_attn_layers >= 0 &&
      (!p.has_ple ||
       (p.ple_conv_state_len >= 0 && p.ple_capacity_tokens > 0 &&
        p.ple_row_bytes > 0 && p.ple_heads > 0 &&
        p.ple_heads <= std::numeric_limits<int>::max() / p.ple_row_bytes));
  if (valid) {
    b.budget = static_cast<size_t>(static_cast<long double>(mem_total) * frac);
    b.weights = weights_bytes;
    b.main_workspace =
        req.main_workspace_bytes > 0
            ? req.main_workspace_bytes
            : ReferenceWorkspace(kMainWorkspaceBytes, p.max_prefill);
    b.mtp_workspace =
        !p.has_mtp ? 0
        : req.mtp_workspace_bytes > 0
            ? req.mtp_workspace_bytes
            : ReferenceWorkspace(kMtpWorkspaceBytes, p.max_prefill);
    b.forward_buffers = ForwardBufferBytes(p);
    b.ple_working = PleWorkingBytes(p);
    b.fixed =
        Add(Add(Add(Add(Add(weights_bytes, b.main_workspace), b.mtp_workspace),
                    b.forward_buffers),
                b.ple_working),
            kContextMarginBytes);
    const auto available_for = [&](int len) -> size_t {
      const size_t fixed = Add(b.fixed, PerRequestBytes(p, len));
      return b.budget > fixed ? b.budget - fixed : 0;
    };
    const auto fits = [&](int len, int seq) {
      return len > 0 && seq > 0 && b.fixed <= b.budget &&
             PerRequestBytes(p, len) <= b.budget - b.fixed &&
             StatePoolBytes(p, len, seq) <= available_for(len);
    };
    constexpr int kHardMaxLen = 262144;
    const int requested_seq = req.max_seq > 0 ? req.max_seq : 8;
    int len = 0;
    int seq = requested_seq;
    if (req.max_len > 0) {
      const int bound = std::min(req.max_len, kHardMaxLen);
      if (fits(bound, 1)) {
        len = bound;
        const size_t per_seq = StatePoolBytes(p, len, 1);
        seq = static_cast<int>(
            std::min<size_t>(requested_seq, available_for(len) / per_seq));
      } else {
        // A pinned length may shrink to >=2048; shorter explicit requests
        // must fit in full. Zero means there was no supported solution.
        int lo = std::min(2048, bound), hi = bound;
        seq = 1;
        while (lo <= hi) {
          const int mid = lo + (hi - lo) / 2;
          if (fits(mid, seq)) {
            len = mid;
            lo = mid + 1;
          } else {
            hi = mid - 1;
          }
        }
      }
    } else {
      // Search whole 1024-token units. Unlike the old fixed-point iteration,
      // this monotone search never manufactures a minimum when none fits.
      int lo = 1, hi = kHardMaxLen / 1024;
      while (lo <= hi) {
        const int mid = lo + (hi - lo) / 2;
        if (fits(mid * 1024, seq)) {
          len = mid * 1024;
          lo = mid + 1;
        } else {
          hi = mid - 1;
        }
      }
    }
    b.feasible = fits(len, seq);
    if (b.feasible) {
      b.max_len = len;
      b.max_seq = seq;
      b.capped = (req.max_len > 0 && len < req.max_len) || seq < requested_seq;
      b.state_pool = StatePoolBytes(p, len, seq);
      b.per_request = PerRequestBytes(p, len);
      b.available_for_state = available_for(len);
    }
    b.estimated_total = Add(Add(b.fixed, b.state_pool), b.per_request);
    b.reason = b.feasible ? (b.capped ? "capacity_reduced" : "estimate_fits")
                          : "no_supported_capacity_fits_estimate";
  } else {
    b.reason = "invalid_budget_request";
  }

  std::ostringstream os;
  os.precision(2);
  os << std::fixed << "[q4t][budget] mem_total=" << b.mem_total / 1e9
     << " GB fraction=" << frac << " budget=" << b.budget / 1e9 << " GB\n"
     << "[q4t][budget]   allocation estimate only; excludes file cache, "
        "driver overlap and other processes; not a physical RAM limit\n"
     << "[q4t][budget]   requested max_len=" << req.max_len
     << " max_seq=" << req.max_seq << " max_prefill=" << req.max_prefill
     << " feasible=" << b.feasible << " reason=" << b.reason << "\n"
     << "[q4t][budget]   weights=" << b.weights / 1e9
     << " GB  fixed=" << b.fixed / 1e9 << " GB (estimated allocations+margin)\n"
     << "[q4t][budget]   main_workspace=" << b.main_workspace
     << " mtp_workspace=" << b.mtp_workspace
     << " forward_buffers=" << b.forward_buffers
     << " ple_working=" << b.ple_working << " mtp=" << p.has_mtp << "\n"
     << "[q4t][budget]   per_request=" << b.per_request / 1e9
     << " GB (MTP trunk+draft_logits)  state_pool=" << b.state_pool / 1e9
     << " GB\n"
     << "[q4t][budget]   => max_len=" << b.max_len << " max_seq=" << b.max_seq
     << (b.capped ? "  (CAPPED to fit estimate)" : "") << "\n";
  if (p.has_mtp) {
    os << "[q4t][budget]   MTP subset only: excludes independent draft "
          "weights, concurrent request overlap and lazy scratch/checkpoints\n";
  }
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
