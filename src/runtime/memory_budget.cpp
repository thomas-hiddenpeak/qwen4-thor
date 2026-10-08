#include "q4t/runtime/memory_budget.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <initializer_list>
#include <limits>
#include <sstream>
#include <string>

#include "q4t/ple/page_reader.h"

namespace q4t {
namespace runtime {
namespace {

// ---------------------------------------------------------------------------
// Reference workspace estimates at max_prefill=8192 (2026-09-19). The main
// value was measured. Serve supplies the real sizing function instead.
// Scaling this fallback value is only an estimate because workspace sizing
// includes alignment and fixed scratch.
// ---------------------------------------------------------------------------
constexpr size_t kMainWorkspaceBytes = 2683503000;  // d_ws (max layer, @8192)
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

size_t Sum(std::initializer_list<size_t> values) {
  size_t total = 0;
  for (size_t value : values) total = Add(total, value);
  return total;
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

// LoadMtp's persistent single-step buffers, independent of ReserveScratch.
size_t MtpBufferBytes(const BudgetModelParams& p) {
  return Mul(Sum({static_cast<size_t>(p.hs), static_cast<size_t>(p.hc_dim()),
                  static_cast<size_t>(p.vocab)}),
             2u);
}

// MtpReserveScratch allocates both sets even for B=1. k+1 is the verify and
// extend row capacity; the main recurrent checkpoints instead use k rows.
size_t MtpScratchBytes(const BudgetModelParams& p, int k) {
  const size_t rows = static_cast<size_t>(k) + 1u;
  const size_t hidden = Mul(p.hs, 2u);
  const size_t trunk = Mul(p.hc_dim(), 2u);
  const size_t logits = Mul(p.vocab, 2u);
  const size_t single =
      Add(Mul(rows, Sum({8u, logits, Mul(trunk, 2u), hidden})), trunk);
  // IDs/positions, sample, multi and rolling g; then gather/verify/extend
  // trunks, verify/extend logits, sample and four packed integer arrays.
  const size_t multi =
      Add(Sum({8u, hidden, Mul(trunk, 2u)}),
          Mul(rows, Sum({Mul(trunk, 3u), Mul(logits, 2u), hidden, 16u})));
  return Add(single, multi);
}

size_t MtpCheckpointBytes(const BudgetModelParams& p, int k) {
  return Mul(Add(Mul(p.linear_attn_layers(), LinearPerSeqBytes(p)),
                 PleConvPerSeqBytes(p)),
             k);
}

size_t MtpForwardTempBytes(const BudgetModelParams& p, int k) {
  // MoeBf16RoutedForward separately cudaMallocAsyncs token_list and counts.
  // Its workspace sizing reserves the same bytes but does not use them for
  // these allocations. Both the workspace and this extra allocation exist.
  const size_t moe_device =
      Mul(Mul(p.mtp_experts, static_cast<size_t>(p.max_prefill) + 1u), 4u);
  const size_t moe_host = Mul(p.mtp_experts, 4u);   // counts_h
  const size_t positions = Mul(p.max_prefill, 4u);  // full-attention hp
  const size_t rows = static_cast<size_t>(k) + 1u;
  const size_t gather_offsets = Mul(rows, 4u);  // MtpSpeculativeStepMulti
  // B=1 small decode metadata, plus two conservative int slots. The complete
  // vhist copy is length-dependent and covered by PerRequestBytes' host
  // envelope, not by this subtotal. Sum across sequential subphases.
  const size_t decode_host = Mul(7u + k + 6u * rows + 2u, 4u);
  return Sum({moe_device, moe_host, positions, gather_offsets, decode_host});
}

size_t MtpLoadingHostBytes(const BudgetModelParams& p, const BudgetRequest& req,
                           int max_len) {
  if (!p.has_mtp) return 0;
  // LoadMoeBf16 reuses one vector: down.resize retains gate_up capacity.
  // Other tensor loaders use one temporary host vector at a time. The RoPE
  // and page-table initialization vectors are also sequential, not additive.
  const size_t gate_up = Mul(Mul(Mul(p.mtp_experts, p.mtp_moe_is), p.hs), 4u);
  return std::max({req.mtp_load_host_bytes, gate_up, Mul(max_len, 12u)});
}

// One admitted sequence can own one prompt trunk. DraftExtend adds two int
// arrays, sample/multi/logits, and serve adds a rolling g. The 8*max_len host
// envelope covers init's shifted IDs/positions or decode's full 4*max_len
// vhist copy. They do not coexist. Decode's small verifier host arrays also
// fit within the charged prompt trunk/chunk that are then absent.
// The chunk charge bounds every prompt up to max_len,
// including one whose last chunk is full. It is monotone in capacity.
size_t PerRequestBytes(const BudgetModelParams& p, int max_len,
                       size_t forward_temp) {
  if (!p.has_mtp) return 0;
  const size_t rows = std::min(max_len, p.max_prefill);
  const size_t trunk = Mul(p.hc_dim(), 2u);
  const size_t chunk =
      Mul(rows, Sum({8u, Mul(p.hs, 2u), trunk, Mul(p.vocab, 2u)}));
  return Sum(
      {Mul(max_len, trunk), trunk, chunk, Mul(max_len, 8u), forward_temp});
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
      (!p.has_mtp || (p.mtp_experts > 0 && p.mtp_moe_is > 0)) &&
      (!p.has_ple ||
       (p.ple_conv_state_len >= 0 && p.ple_capacity_tokens > 0 &&
        p.ple_row_bytes > 0 && p.ple_heads > 0 &&
        p.ple_heads <= std::numeric_limits<int>::max() / p.ple_row_bytes));
  const bool supported_mtp =
      !p.has_mtp ||
      (req.max_seq == 1 && req.mtp_k == 3 && p.mtp_full_attn_layers == 1 &&
       p.max_prefill >= 4 && p.max_prefill <= 8192);
  const bool complete_mtp =
      !p.has_mtp || (req.mtp_weights_bytes > 0 && req.mtp_load_host_bytes > 0 &&
                     req.mtp_workspace_bytes > 0);
  if (valid && supported_mtp && complete_mtp) {
    b.budget = static_cast<size_t>(static_cast<long double>(mem_total) * frac);
    b.weights = weights_bytes;
    b.main_workspace =
        req.main_workspace_bytes > 0
            ? req.main_workspace_bytes
            : ReferenceWorkspace(kMainWorkspaceBytes, p.max_prefill);
    b.main_forward_extra = req.main_forward_extra_bytes;
    if (p.has_mtp) {
      b.mtp_workspace = req.mtp_workspace_bytes;
      b.mtp_weights = req.mtp_weights_bytes;
      b.mtp_buffers = MtpBufferBytes(p);
      b.mtp_scratch = MtpScratchBytes(p, req.mtp_k);
      b.mtp_checkpoints = MtpCheckpointBytes(p, req.mtp_k);
      b.mtp_forward_temp = MtpForwardTempBytes(p, req.mtp_k);
      b.loading_host_peak = MtpLoadingHostBytes(p, req, 0);
    }
    b.forward_buffers = ForwardBufferBytes(p);
    b.ple_working = PleWorkingBytes(p);
    b.fixed =
        Sum({weights_bytes, b.main_workspace, b.mtp_workspace, b.mtp_weights,
             b.mtp_buffers, b.mtp_scratch, b.mtp_checkpoints, b.forward_buffers,
             b.ple_working, b.main_forward_extra, kContextMarginBytes});
    const auto peak_extra = [&](int len) {
      return std::max(PerRequestBytes(p, len, b.mtp_forward_temp),
                      MtpLoadingHostBytes(p, req, len));
    };
    const auto available_for = [&](int len) -> size_t {
      const size_t fixed = Add(b.fixed, peak_extra(len));
      return b.budget > fixed ? b.budget - fixed : 0;
    };
    const auto fits = [&](int len, int seq) {
      return len > 0 && seq > 0 && b.fixed <= b.budget &&
             (!p.has_mtp || len >= req.mtp_k + 1) &&
             peak_extra(len) <= b.budget - b.fixed &&
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
      b.per_request = PerRequestBytes(p, len, b.mtp_forward_temp);
      b.loading_host_peak = MtpLoadingHostBytes(p, req, len);
      b.available_for_state = available_for(len);
    }
    b.runtime_peak = Sum({b.fixed, b.state_pool, b.per_request});
    // All persistent allocations are charged even if some are created only
    // after host staging has been freed. This is a conservative startup
    // envelope; it never adds the mutually exclusive request transient.
    b.startup_peak = Sum({b.fixed, b.state_pool, b.loading_host_peak});
    b.estimated_total = std::max(b.runtime_peak, b.startup_peak);
    b.reason = b.feasible ? (b.capped ? "capacity_reduced" : "estimate_fits")
                          : "no_supported_capacity_fits_estimate";
  } else {
    b.reason = !valid           ? "invalid_budget_request"
               : !supported_mtp ? "unsupported_mtp_configuration"
                                : "missing_mtp_budget_inputs";
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
     << " main_forward_extra=" << b.main_forward_extra
     << " mtp_workspace=" << b.mtp_workspace
     << " forward_buffers=" << b.forward_buffers
     << " ple_working=" << b.ple_working << " mtp=" << p.has_mtp << "\n"
     << "[q4t][budget]   per_request=" << b.per_request / 1e9
     << " GB (MTP request allocations)  state_pool=" << b.state_pool / 1e9
     << " GB\n"
     << "[q4t][budget]   => max_len=" << b.max_len << " max_seq=" << b.max_seq
     << (b.capped ? "  (CAPPED to fit estimate)" : "") << "\n";
  if (p.has_mtp) {
    os << "[q4t][budget]   MTP admission scope: text max_seq=1 k=3; "
          "allocation estimate, not a physical RAM guarantee\n"
       << "[q4t][budget]   mtp_weights=" << b.mtp_weights
       << " mtp_buffers=" << b.mtp_buffers << " mtp_scratch=" << b.mtp_scratch
       << " mtp_checkpoints=" << b.mtp_checkpoints
       << " mtp_forward_temp=" << b.mtp_forward_temp << "\n"
       << "[q4t][budget]   loading_host_peak=" << b.loading_host_peak
       << " startup_envelope=" << b.startup_peak
       << " runtime_peak=" << b.runtime_peak
       << " estimated_total=" << b.estimated_total << "\n";
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
