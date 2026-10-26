#include "q4t/runtime/startup_plan.h"

#include <cstdint>
#include <memory>
#include <string>

#include "q4t/model/linear_attention.h"
#include "q4t/model/model.h"
#include "q4t/model/ple_layer.h"
#include "q4t/mtp/mtp.h"
#include "q4t/mtp/subset_head.h"
#include "startup_weight_plan.h"

namespace q4t::runtime {
namespace {

Status CheckMtpCapacity(const mtp::MtpConfig& cfg) {
  if (!ValidSequenceCapacities(cfg.seq_capacities, cfg.max_len, cfg.max_seq))
    return Status::Fail("startup MTP plan: invalid slot capacities");
  // The current kernels and LoadMtp implement this architecture. Capacities
  // vary, but a new architecture needs a matching allocation contract.
  const mtp::MtpConfig supported;
  if (cfg.hs != supported.hs || cfg.hc != supported.hc ||
      cfg.lowrank != supported.lowrank || cfg.E != supported.E ||
      cfg.moe_is != supported.moe_is || cfg.shared_is != supported.shared_is ||
      cfg.topk != supported.topk || cfg.vocab != supported.vocab ||
      cfg.nq != supported.nq || cfg.nkv != supported.nkv ||
      cfg.hd != supported.hd || cfg.rot_d != supported.rot_d ||
      cfg.idx_n_heads != supported.idx_n_heads ||
      cfg.idx_kv_heads != supported.idx_kv_heads ||
      cfg.idx_head_dim != supported.idx_head_dim ||
      cfg.idx_budget != supported.idx_budget ||
      cfg.idx_compress != supported.idx_compress || cfg.max_len < 1 ||
      cfg.max_len > 262144 || cfg.max_seq < 1 || cfg.max_seq > 64 ||
      cfg.max_prefill < 1 || cfg.max_prefill > 8192 ||
      cfg.initial_scratch_capacity < 0 || cfg.initial_scratch_capacity > 17 ||
      cfg.initial_scratch_capacity > cfg.max_prefill ||
      cfg.initial_scratch_capacity > cfg.max_len) {
    return Status::Fail("startup plan: unsupported MTP dimensions or capacity");
  }
  return Status();
}

Status PlanScratch(const mtp::MtpConfig& cfg, StartupPlan* plan) {
  const int k = cfg.initial_scratch_capacity;
  if (k == 0) return Status();
  const int64_t hc_dim = static_cast<int64_t>(cfg.hc) * cfg.hs;
  const int64_t rows = static_cast<int64_t>(cfg.max_seq) * k;
  auto add = [&](const char* name, std::initializer_list<int64_t> shape,
                 size_t bytes) {
    return plan->Add(std::string("mtp.scratch.") + name, shape, bytes);
  };
  // Exact MtpReserveScratch order, including batched scratch for max_seq=1.
  Status status;
  if (!(status = add("ids", {k}, sizeof(int32_t)))) return status;
  if (!(status = add("positions", {k}, sizeof(int)))) return status;
  if (!(status = add("spec_logits", {k, cfg.vocab}, sizeof(uint16_t))))
    return status;
  if (!(status = add("spec_trunk", {k, hc_dim}, sizeof(uint16_t))))
    return status;
  if (!(status = add("spec_multi", {k, hc_dim}, sizeof(uint16_t))))
    return status;
  if (!(status = add("spec_sample", {k, cfg.hs}, sizeof(uint16_t))))
    return status;
  if (!(status = add("g", {hc_dim}, sizeof(uint16_t)))) return status;
  if (!(status = add("ms_ids", {cfg.max_seq}, sizeof(int32_t)))) return status;
  if (!(status = add("ms_positions", {cfg.max_seq}, sizeof(int))))
    return status;
  if (!(status = add("ms_sample", {cfg.max_seq, cfg.hs}, sizeof(uint16_t))))
    return status;
  if (!(status = add("ms_multi", {cfg.max_seq, hc_dim}, sizeof(uint16_t))))
    return status;
  if (!(status = add("ms_gather", {rows, hc_dim}, sizeof(uint16_t))))
    return status;
  if (!(status = add("ms_g_pool", {cfg.max_seq, hc_dim}, sizeof(uint16_t))))
    return status;
  if (!(status = add("ms_ext_seq", {rows}, sizeof(int)))) return status;
  if (!(status = add("ms_vlogits", {rows, cfg.vocab}, sizeof(uint16_t))))
    return status;
  if (!(status = add("ms_vtrunk", {rows, hc_dim}, sizeof(uint16_t))))
    return status;
  if (!(status = add("ms_ext_logits", {rows, cfg.vocab}, sizeof(uint16_t))))
    return status;
  if (!(status = add("ms_ext_multi", {rows, hc_dim}, sizeof(uint16_t))))
    return status;
  if (!(status = add("ms_ext_sample", {rows, cfg.hs}, sizeof(uint16_t))))
    return status;
  if (!(status = add("ms_ext_ids", {rows}, sizeof(int32_t)))) return status;
  if (!(status = add("ms_ext_positions", {rows}, sizeof(int)))) return status;
  if (!(status = add("ms_drafts", {rows}, sizeof(int32_t)))) return status;
  return add("ms_gather_offsets", {rows}, sizeof(int));
}

}  // namespace

Status PlanMtpResources(const mtp::MtpConfig& cfg, StartupPlan* plan) {
  if (!plan) return Status::Fail("startup MTP plan: null output");
  Status status = CheckMtpCapacity(cfg);
  if (!status.ok()) return status;
  io::WeightIndex* raw_index = nullptr;
  status = io::WeightIndex::Open(cfg.mtp_dir + "/model.safetensors.index.json",
                                 &raw_index);
  if (!status.ok()) return status;
  std::unique_ptr<io::WeightIndex> index(raw_index);
  io::WeightLoader* raw_loader = nullptr;
  status = io::WeightLoader::Create(cfg.mtp_dir, *index, 8, &raw_loader);
  if (!status.ok()) return status;
  std::unique_ptr<io::WeightLoader> loader(raw_loader);
  // Header inspection must not evict warmed payload pages when its shard
  // handles leave the loader's LRU or when this metadata pass returns.
  loader->SetKeepPageCache(true, "");
  const int64_t hc_dim = static_cast<int64_t>(cfg.hc) * cfg.hs;
  auto tensor = [&](const std::string& name,
                    std::initializer_list<int64_t> shape) {
    return plan->AddTensor(*loader, name, io::Dtype::kBF16, shape);
  };
  if (!cfg.subset_head_dir.empty()) {
    status = plan->AddBytes("mtp.subset_head", mtp::kSubsetWeightBytes);
    if (!status.ok()) return status;
    status = plan->AddBytes("mtp.subset_ids", mtp::kSubsetIdBytes);
    if (!status.ok()) return status;
  }
  // LoadMtp allocates both projections and both norms before their uploads.
  if (!(status = tensor("mtp.fc_embedding.weight", {cfg.hs, cfg.hs})))
    return status;
  if (!(status = tensor("mtp.fc_hidden.weight", {cfg.hs, cfg.hs})))
    return status;
  if (!(status = tensor("mtp.pre_fc_norm_embedding.weight", {cfg.hs})))
    return status;
  if (!(status = tensor("mtp.pre_fc_norm_hidden.weight", {hc_dim})))
    return status;

  const std::string layer = "mtp.layers.0";
  for (const char* suffix :
       {".attn_hyper_connection", ".mlp_hyper_connection"}) {
    status = startup_detail::PlanHC(*loader, layer + suffix, cfg.hc, cfg.hs,
                                    cfg.lowrank, true, plan);
    if (!status.ok()) return status;
  }
  model::FullAttentionWeights full;
  full.hidden_size = cfg.hs;
  full.nq = cfg.nq;
  full.nkv = cfg.nkv;
  full.hd = cfg.hd;
  full.rot_d = cfg.rot_d;
  full.rope_theta = cfg.rope_theta;
  full.eps = cfg.eps;
  full.max_len = cfg.max_len;
  full.idx_n_heads = cfg.idx_n_heads;
  full.idx_kv_heads = cfg.idx_kv_heads;
  full.idx_head_dim = cfg.idx_head_dim;
  full.idx_budget = cfg.idx_budget;
  full.idx_compress = cfg.idx_compress;
  status = startup_detail::PlanFull(*loader, layer + ".self_attn", full, plan);
  if (!status.ok()) return status;
  if (!(status = tensor(layer + ".mlp.experts.gate_up_proj",
                        {cfg.E, 2 * cfg.moe_is, cfg.hs})))
    return status;
  if (!(status = tensor(layer + ".mlp.experts.down_proj",
                        {cfg.E, cfg.hs, cfg.moe_is})))
    return status;
  status = startup_detail::PlanMoEExtra(*loader, layer + ".mlp", cfg.E, cfg.hs,
                                        cfg.shared_is, plan);
  if (!status.ok()) return status;
  status = startup_detail::PlanHC(*loader, "mtp.hyper_connection_mixer", cfg.hc,
                                  cfg.hs, cfg.lowrank, false, plan);
  if (!status.ok()) return status;

  if (!cfg.seq_capacities.empty()) {
    status = plan->Add("mtp.state.idx_offsets", {cfg.max_seq}, sizeof(size_t));
    if (!status.ok()) return status;
  }
  const int64_t pages =
      (static_cast<int64_t>(cfg.max_len) + model::kKvPageSize - 1) /
      model::kKvPageSize;
  if (!(status =
            plan->Add("mtp.state.kv_cache",
                      {static_cast<int64_t>(
                           cfg.seq_capacities.empty()
                               ? cfg.max_seq * pages * model::kKvPageSize
                               : SequencePoolTokens(cfg.seq_capacities,
                                                    cfg.max_len, cfg.max_seq)),
                       cfg.nkv, 2, cfg.hd},
                      sizeof(uint16_t))))
    return status;
  if (!(status = plan->Add("mtp.state.page_table", {cfg.max_seq, cfg.max_len},
                           sizeof(int))))
    return status;
  for (const char* name : {"mtp.state.idx_raw", "mtp.state.idx_comp"}) {
    status = plan->Add(name,
                       {static_cast<int64_t>(SequencePoolTokens(
                            cfg.seq_capacities, cfg.max_len, cfg.max_seq)),
                        cfg.idx_head_dim},
                       sizeof(uint16_t));
    if (!status.ok()) return status;
  }
  if (!(status = plan->Add("mtp.state.rope_positions",
                           {cfg.max_seq, 3, cfg.max_len}, sizeof(int))))
    return status;
  status = plan->AddBytes("mtp.workspace",
                          mtp::MtpWorkspaceBytes(cfg, cfg.max_prefill, full));
  if (!status.ok()) return status;
  if (!(status = plan->Add("mtp.sample", {cfg.hs}, sizeof(uint16_t))))
    return status;
  if (!(status = plan->Add("mtp.trunk", {hc_dim}, sizeof(uint16_t))))
    return status;
  if (!(status = plan->Add("mtp.logits", {cfg.vocab}, sizeof(uint16_t))))
    return status;
  return PlanScratch(cfg, plan);
}

Status PlanVerifyResources(const model::ModelConfig& cfg, int k,
                           StartupPlan* plan) {
  if (!plan || cfg.num_layers < 0 || cfg.num_layers > 48 || cfg.max_seq < 1 ||
      cfg.max_seq > 64 || cfg.hs != 2560 || cfg.hc != 4 || k < 0 || k > 16) {
    return Status::Fail("startup verify plan: unsupported dimensions");
  }
  if (k == 0 || cfg.num_layers == 0) return Status();
  // LoadDecoderLayer uses these fixed linear/PLE dimensions and marks every
  // fourth layer full-attention; CollectLinCkptDims visits linear layers only.
  const model::LinearAttentionWeights linear;
  const model::PleLayerWeights ple;
  const int linear_layers = cfg.num_layers - cfg.num_layers / 4;
  Status status = plan->Add(
      "model.verify.ssm",
      {linear_layers, cfg.max_seq, k, linear.nv, linear.kd, linear.vd},
      sizeof(float));
  if (!status.ok()) return status;
  status = plan->Add(
      "model.verify.conv",
      {linear_layers, cfg.max_seq, k, linear.in_qkv(), linear.conv_k - 1},
      sizeof(uint16_t));
  if (!status.ok()) return status;
  // Zero-based layer 1 is the sole PLE layer and is a linear-attention layer.
  if (cfg.num_layers > 1) {
    return plan->Add("model.verify.ple_conv",
                     {cfg.max_seq, k, cfg.hc, cfg.hs, ple.conv_kernel - 1,
                      ple.conv_dilation},
                     sizeof(uint16_t));
  }
  return Status();
}

}  // namespace q4t::runtime
