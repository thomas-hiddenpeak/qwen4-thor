// CPU-only: production workspace sizing + allocation estimate, no CUDA calls.
#include <algorithm>
#include <cstdio>

#include "q4t/model/decoder_layer.h"
#include "q4t/model/model_head.h"
#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"
#include "q4t/mtp/subset_head.h"
#include "q4t/mtp/suffix_drafter.h"
#include "q4t/runtime/memory_budget.h"

int main() {
  const q4t::model::ModelConfig cfg;
  q4t::model::FullAttentionWeights full;
  full.max_len = 208896;
  full.min_slot_capacity = 65536;
  q4t::runtime::BudgetRequest req;
  req.max_len = full.max_len;
  req.max_seq = 4;
  req.max_prefill = 4096;
  req.seq_capacities = {208896, 65536, 65536, 65536};
  req.main_workspace_bytes = q4t::model::ModelHeadWorkspaceBytes(4096, cfg.hs);
  for (int i = 0; i < cfg.num_layers; ++i)
    req.main_workspace_bytes =
        std::max(req.main_workspace_bytes,
                 q4t::model::DecoderLayerWorkspaceBytes(
                     4096, i % 4 == 3, i == 1, cfg.hs, cfg.E, cfg.moe_is,
                     cfg.shared_is, cfg.topk, &full, cfg.lowrank));
  q4t::mtp::MtpConfig mcfg;
  mcfg.max_prefill = 4096;
  mcfg.max_len = full.max_len;
  req.mtp_workspace_bytes = q4t::mtp::MtpWorkspaceBytes(mcfg, 4096, full);
  // Bound to the same read-only checkpoint headers as the prior capacity audit.
  req.mtp_weights_bytes = 5214301696;
  req.mtp_load_host_bytes = 3355443200;
  req.extra_fixed_bytes =
      4 * q4t::mtp::SuffixDrafter::kMaxMemoryBytes + (size_t{256} << 10);
  q4t::runtime::BudgetModelParams params;
  params.has_mtp = true;
  bool all_fit = true;
  for (int subset = 0; subset < 2; ++subset) {
    if (subset) {
      req.extra_fixed_bytes += q4t::mtp::kSubsetReadCacheBudget;
      req.mtp_weights_bytes +=
          q4t::mtp::kSubsetWeightBytes + q4t::mtp::kSubsetIdBytes;
    }
    const auto b = q4t::runtime::ComputeMemoryBudget(
        params, req, 83995036096, q4t::runtime::ReadMemTotal());
    const auto requested = q4t::runtime::ComputeMemoryBudget(
        params, req, 83995036096, size_t{1} << 50);
    std::printf(
        "{\"head\":\"%s\",\"main_workspace\":%zu,\"mtp_workspace\":%zu,"
        "\"mem_total\":%zu,\"budget\":%zu,\"feasible\":%s,"
        "\"effective_slots\":%d,\"requested_peak\":%zu,\"state\":%zu,"
        "\"transient\":%zu,\"fixed\":%zu}\n",
        subset ? "subset" : "full", req.main_workspace_bytes,
        req.mtp_workspace_bytes, b.mem_total, b.budget,
        b.feasible ? "true" : "false", b.max_seq, requested.estimated_total,
        requested.state_pool, requested.per_request, requested.fixed);
    all_fit &= b.feasible && b.max_seq == 4 && !b.capped;
  }
  return all_fit ? 0 : 1;
}
