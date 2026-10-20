// Configuration/shape-derived research plan; never consumes a runtime trace.
#include "q4t/runtime/startup_plan.h"

#include <algorithm>
#include <cstdlib>
#include <memory>

#include "q4t/model/model.h"
#include "q4t/quant/moe_residency.h"
#include "q4t/quant/swizzle.h"
#include "q4t/runtime/residency_config.h"
#include "startup_weight_plan.h"

namespace q4t::runtime {
namespace {
#define Q4T_PLAN_TRY(expression)     \
  do {                               \
    Status status = (expression);    \
    if (!status.ok()) return status; \
  } while (false)

constexpr io::Dtype kBF16 = io::Dtype::kBF16;

bool EnvironmentEnabled(const char* name) {
  const char* value = std::getenv(name);
  return value && *value && !(value[0] == '0' && value[1] == '\0');
}

Status ValidateMainConfig(const model::ModelConfig& cfg,
                          const ResidencyConfig& residency) {
  // LoadDecoderLayer fixes these attention/PLE dimensions in its callsites.
  // Reject another architecture instead of inferring it from matching bytes.
  if (cfg.hs != 2560 || cfg.hc != 4 || cfg.lowrank != 320 || cfg.E != 512 ||
      cfg.moe_is != 640 || cfg.shared_is != 640 || cfg.topk != 10 ||
      cfg.vocab != 248320 || cfg.num_layers < 1 || cfg.num_layers > 48 ||
      cfg.ple_ngram_size != 3 || cfg.ple_heads_per_ngram != 8 ||
      cfg.ple_row_bytes != 160) {
    return Status::Fail("startup plan: unsupported main model architecture");
  }
  if (cfg.max_len < 1 || cfg.max_len > 262144 || cfg.max_prefill < 1 ||
      cfg.max_prefill > 8192 || cfg.max_seq < 1 ||
      cfg.max_seq > cfg.max_prefill || cfg.ple_capacity_tokens < 1 ||
      cfg.ple_capacity_tokens > 262144 || cfg.moe_resident_slots < 0 ||
      cfg.moe_resident_slots > cfg.E ||
      residency.layer_slots.size() != static_cast<size_t>(cfg.num_layers)) {
    return Status::Fail("startup plan: unsupported main capacity");
  }
  for (int slots : residency.layer_slots) {
    if ((cfg.moe_resident_slots == 0 && slots != 0) ||
        (cfg.moe_resident_slots > 0 &&
         (slots < 1 || slots > cfg.moe_resident_slots))) {
      return Status::Fail("startup plan: inconsistent layer residency");
    }
  }
  for (const char* name : {"Q4T_FP8_PROJ", "Q4T_FP8_ATTN", "Q4T_FP8_GDN",
                           "Q4T_FP8_LMHEAD", "Q4T_FP8_SHARED", "Q4T_FP8_HC"}) {
    if (EnvironmentEnabled(name)) {
      return Status::Fail(
          std::string("startup plan: FP8 shadow unsupported: ") + name);
    }
  }
  return {};
}

Status PlanLinear(const io::WeightLoader& loader, const std::string& prefix,
                  int hs, StartupPlan* plan) {
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".in_proj_qkv.weight", kBF16,
                               {10240, hs}));
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".in_proj_z.weight", kBF16, {6144, hs}));
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".in_proj_a.weight", kBF16, {48, hs}));
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".in_proj_b.weight", kBF16, {48, hs}));
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".conv1d.weight", kBF16, {10240, 1, 4}));
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".out_proj.weight", kBF16, {hs, 6144}));
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".norm.weight", kBF16, {128}));
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".A_log", kBF16, {48}));
  return plan->AddTensor(loader, prefix + ".dt_bias", kBF16, {48});
}

Status CheckExperts(const io::WeightLoader& loader, const std::string& prefix,
                    int experts, int hs, int moe_is, StartupPlan* plan) {
  // Validate every expert, including cold ones that can be requested later.
  // These calls read only shard headers and do not materialize tensor payloads.
  for (int expert = 0; expert < experts; ++expert) {
    for (const char* projection : {"gate_proj", "up_proj", "down_proj"}) {
      const bool down = std::string(projection) == "down_proj";
      const int rows = down ? hs : moe_is;
      const int columns = down ? moe_is : hs;
      const std::string base =
          prefix + ".experts." + std::to_string(expert) + "." + projection;
      Q4T_PLAN_TRY(plan->CheckTensor(loader, base + ".weight", io::Dtype::kU8,
                                     {rows, columns / 2}));
      Q4T_PLAN_TRY(plan->CheckTensor(loader, base + ".weight_scale",
                                     io::Dtype::kF8E4M3, {rows, columns / 16}));
      Q4T_PLAN_TRY(plan->CheckTensor(loader, base + ".weight_scale_2",
                                     io::Dtype::kF32, {}));
      Q4T_PLAN_TRY(plan->CheckTensor(loader, base + ".input_scale",
                                     io::Dtype::kF32, {}));
    }
  }
  return {};
}

Status PlanExperts(const io::WeightLoader& loader, const std::string& prefix,
                   int experts, int hs, int moe_is, int slots,
                   StartupPlan* plan) {
  Q4T_PLAN_TRY(CheckExperts(loader, prefix, experts, hs, moe_is, plan));
  const int count = slots > 0 ? slots : experts;
  const size_t before = plan->device_bytes();
  Q4T_PLAN_TRY(plan->Add(prefix + ".gu_packed", {2, count, moe_is, hs / 2}, 1));
  Q4T_PLAN_TRY(plan->Add(prefix + ".gu_sf",
                         {count, int64_t(quant::SfBufferSize(2 * moe_is, hs))},
                         1));
  Q4T_PLAN_TRY(plan->Add(prefix + ".dn_packed", {count, hs, moe_is / 2}, 1));
  Q4T_PLAN_TRY(plan->Add(prefix + ".dn_sf",
                         {count, int64_t(quant::SfBufferSize(hs, moe_is))}, 1));
  if (slots > 0) {
    Q4T_PLAN_TRY(plan->Add(prefix + ".scale_block", {4, count}, sizeof(float)));
  } else {
    for (const char* suffix :
         {"gu_w_scale2", "gu_input_scale", "dn_w_scale2", "dn_input_scale"}) {
      Q4T_PLAN_TRY(plan->Add(prefix + "." + suffix, {count}, sizeof(float)));
    }
  }
  if (plan->device_bytes() - before !=
      quant::MoEResidencyLayerBytes(hs, moe_is, count)) {
    return Status::Fail("startup plan: expert layout/accounting mismatch");
  }
  return {};
}

Status PlanPle(const io::WeightLoader& loader, const std::string& prefix,
               int hs, int hc, StartupPlan* plan) {
  const int64_t hyper = int64_t(hs) * hc;
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".key_proj.weight", kBF16, {hyper, hs}));
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".value_proj.weight", kBF16, {hs, hs}));
  for (const char* suffix :
       {"norm_key.weight", "norm_query.weight", "norm_conv.weight"}) {
    Q4T_PLAN_TRY(
        plan->AddTensor(loader, prefix + "." + suffix, kBF16, {hyper}));
  }
  return plan->AddTensor(loader, prefix + ".conv1d.weight", kBF16,
                         {hyper, 1, 4});
}

Status PlanPleEmbedding(const io::WeightLoader& loader,
                        const model::ModelConfig& cfg, StartupPlan* plan) {
  const std::string base = "model.language_model.layers.1.ple.ple_embedding.";
  Q4T_PLAN_TRY(plan->CheckTensor(loader, base + "layer_multipliers",
                                 io::Dtype::kI64, {cfg.ple_ngram_size}));
  const int64_t heads =
      (int64_t(cfg.ple_ngram_size) - 1) * cfg.ple_heads_per_ngram;
  Q4T_PLAN_TRY(plan->CheckTensor(loader, base + "ngram_heads_vocab_sizes",
                                 io::Dtype::kI64, {heads}));
  Q4T_PLAN_TRY(plan->CheckTensor(loader, base + "ngram_heads_offsets",
                                 io::Dtype::kI64, {heads}));
  Q4T_PLAN_TRY(plan->CheckTensor(loader, base + "ngram_embedding.weight_scale",
                                 kBF16, {1}));
  return plan->Add(
      "ple_embedding.gpu_fp8",
      {cfg.ple_capacity_tokens, heads, static_cast<int64_t>(cfg.ple_row_bytes)},
      1);
}
}  // namespace

namespace startup_detail {
Status PlanHC(const io::WeightLoader& loader, const std::string& prefix, int hc,
              int hs, int lowrank, bool combine, StartupPlan* plan) {
  const int64_t hyper = int64_t(hc) * hs;
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".hc_norm.weight", kBF16, {hyper}));
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".input_mix_weight_down.weight",
                               kBF16, {lowrank, hyper}));
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".input_mix_weight_up.weight",
                               kBF16, {hyper, lowrank}));
  if (combine) {
    Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".block_inject_weight.weight",
                                 kBF16, {hc, hyper}));
  }
  return {};
}

Status PlanFull(const io::WeightLoader& loader, const std::string& prefix,
                const model::FullAttentionWeights& dims, StartupPlan* plan) {
  const int64_t hs = dims.hidden_size;
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".q_proj.weight", kBF16,
                               {int64_t(dims.nq) * 2 * dims.hd, hs}));
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".k_proj.weight", kBF16,
                               {int64_t(dims.nkv) * dims.hd, hs}));
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".v_proj.weight", kBF16,
                               {int64_t(dims.nkv) * dims.hd, hs}));
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".o_proj.weight", kBF16,
                               {hs, int64_t(dims.nq) * dims.hd}));
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".q_norm.weight", kBF16, {dims.hd}));
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".k_norm.weight", kBF16, {dims.hd}));
  Q4T_PLAN_TRY(plan->AddTensor(
      loader, prefix + ".indexer.index_qk_proj.weight", kBF16,
      {(int64_t(dims.idx_n_heads) + dims.idx_kv_heads) * dims.idx_head_dim,
       hs}));
  Q4T_PLAN_TRY(plan->AddTensor(loader, prefix + ".indexer.q_layernorm.weight",
                               kBF16, {dims.idx_head_dim}));
  return plan->AddTensor(loader, prefix + ".indexer.k_layernorm.weight", kBF16,
                         {dims.idx_head_dim});
}

Status PlanMoEExtra(const io::WeightLoader& loader, const std::string& prefix,
                    int experts, int hs, int shared_is, StartupPlan* plan) {
  Q4T_PLAN_TRY(
      plan->AddTensor(loader, prefix + ".gate.weight", kBF16, {experts, hs}));
  for (const char* projection : {"gate_proj", "up_proj"}) {
    Q4T_PLAN_TRY(plan->CheckTensor(
        loader, prefix + ".shared_expert." + projection + ".weight", kBF16,
        {shared_is, hs}));
  }
  Q4T_PLAN_TRY(plan->Add(prefix + ".shared_gu", {2, shared_is, hs}, 2));
  Q4T_PLAN_TRY(plan->AddTensor(loader,
                               prefix + ".shared_expert.down_proj.weight",
                               kBF16, {hs, shared_is}));
  return plan->AddTensor(loader, prefix + ".shared_expert_gate.weight", kBF16,
                         {1, hs});
}
}  // namespace startup_detail

Status PlanMainResources(const model::ModelConfig& cfg,
                         const ResidencyConfig& residency, StartupPlan* plan) {
  if (!plan) return Status::Fail("startup plan: null output");
  Q4T_PLAN_TRY(ValidateMainConfig(cfg, residency));
  io::WeightIndex* raw_index = nullptr;
  Q4T_PLAN_TRY(io::WeightIndex::Open(cfg.index_path, &raw_index));
  std::unique_ptr<io::WeightIndex> index(raw_index);
  io::WeightLoader* raw_loader = nullptr;
  Q4T_PLAN_TRY(io::WeightLoader::Create(cfg.model_dir, *index,
                                        index->num_shards() + 1, &raw_loader));
  std::unique_ptr<io::WeightLoader> loader(raw_loader);
  // A metadata pass must not evict the warm payload when handles close.
  loader->SetKeepPageCache(true, "");
  const std::string language = "model.language_model.";
  Q4T_PLAN_TRY(plan->AddTensor(*loader, language + "embed_tokens.weight", kBF16,
                               {cfg.vocab, cfg.hs}));
  Q4T_PLAN_TRY(
      plan->AddTensor(*loader, "lm_head.weight", kBF16, {cfg.vocab, cfg.hs}));
  Q4T_PLAN_TRY(
      startup_detail::PlanHC(*loader, language + "hyper_connection_mixer",
                             cfg.hc, cfg.hs, cfg.lowrank, false, plan));

  model::FullAttentionWeights full;
  full.hidden_size = cfg.hs;
  full.max_len = cfg.max_len;
  size_t workspace = model::ModelHeadWorkspaceBytes(cfg.max_prefill, cfg.hs);
  const int64_t hyper = int64_t(cfg.hc) * cfg.hs;
  for (int layer = 0; layer < cfg.num_layers; ++layer) {
    const std::string base = language + "layers." + std::to_string(layer);
    const bool is_full = layer % 4 == 3;
    const bool has_ple = layer == 1;
    Q4T_PLAN_TRY(startup_detail::PlanHC(*loader,
                                        base + ".attn_hyper_connection", cfg.hc,
                                        cfg.hs, cfg.lowrank, true, plan));
    Q4T_PLAN_TRY(startup_detail::PlanHC(*loader, base + ".mlp_hyper_connection",
                                        cfg.hc, cfg.hs, cfg.lowrank, true,
                                        plan));
    if (is_full) {
      Q4T_PLAN_TRY(
          startup_detail::PlanFull(*loader, base + ".self_attn", full, plan));
      const int64_t pages =
          (int64_t(cfg.max_len) + model::kKvPageSize - 1) / model::kKvPageSize;
      Q4T_PLAN_TRY(plan->Add(
          base + ".state.kv_cache",
          {cfg.max_seq, pages, model::kKvPageSize, full.nkv, 2, full.hd}, 2));
      Q4T_PLAN_TRY(plan->Add(base + ".state.page_table",
                             {cfg.max_seq, cfg.max_len}, sizeof(int)));
      for (const char* name : {"idx_raw", "idx_comp"}) {
        Q4T_PLAN_TRY(plan->Add(base + ".state." + name,
                               {cfg.max_seq, cfg.max_len, full.idx_head_dim},
                               2));
      }
    } else {
      Q4T_PLAN_TRY(PlanLinear(*loader, base + ".linear_attn", cfg.hs, plan));
      Q4T_PLAN_TRY(plan->Add(base + ".state.ssm", {cfg.max_seq, 48, 128, 128},
                             sizeof(float)));
      Q4T_PLAN_TRY(plan->Add(base + ".state.conv", {cfg.max_seq, 10240, 3}, 2));
    }
    if (has_ple) {
      Q4T_PLAN_TRY(
          plan->Add(base + ".state.ple_conv", {cfg.max_seq, hyper, 9}, 2));
    }
    Q4T_PLAN_TRY(PlanExperts(*loader, base + ".mlp", cfg.E, cfg.hs, cfg.moe_is,
                             residency.layer_slots[layer], plan));
    Q4T_PLAN_TRY(startup_detail::PlanMoEExtra(*loader, base + ".mlp", cfg.E,
                                              cfg.hs, cfg.shared_is, plan));
    if (has_ple) {
      Q4T_PLAN_TRY(PlanPle(*loader, base + ".ple", cfg.hs, cfg.hc, plan));
    }
    workspace =
        std::max(workspace,
                 model::DecoderLayerWorkspaceBytes(
                     cfg.max_prefill, is_full, has_ple, cfg.hs, cfg.E,
                     cfg.moe_is, cfg.shared_is, cfg.topk, &full, cfg.lowrank));
  }
  const bool has_ple = cfg.num_layers > 1;
  if (has_ple) Q4T_PLAN_TRY(PlanPleEmbedding(*loader, cfg, plan));
  const int64_t tokens = cfg.max_prefill;
  Q4T_PLAN_TRY(plan->Add("model.d_ids", {tokens}, sizeof(int32_t)));
  Q4T_PLAN_TRY(plan->Add("model.d_positions", {tokens}, sizeof(int)));
  Q4T_PLAN_TRY(plan->Add("model.d_seq_id", {cfg.max_seq}, sizeof(int)));
  Q4T_PLAN_TRY(plan->Add("model.d_token_seq_id", {tokens}, sizeof(int)));
  Q4T_PLAN_TRY(plan->Add("model.d_ragged_seq_offset",
                         {int64_t(cfg.max_seq) + 1}, sizeof(int)));
  Q4T_PLAN_TRY(plan->Add("model.d_ragged_token_local", {tokens}, sizeof(int)));
  Q4T_PLAN_TRY(plan->Add("model.d_rope_pos", {cfg.max_seq, 3, cfg.max_len},
                         sizeof(int)));
  Q4T_PLAN_TRY(plan->Add("model.d_emb", {tokens, cfg.hs}, 2));
  Q4T_PLAN_TRY(plan->Add("model.d_img_pos", {tokens}, sizeof(int)));
  Q4T_PLAN_TRY(plan->Add("model.d_trunk", {tokens, hyper}, 2));
  Q4T_PLAN_TRY(plan->Add("model.d_trunk2", {tokens, hyper}, 2));
  if (has_ple) Q4T_PLAN_TRY(plan->Add("model.d_ple_emb", {tokens, cfg.hs}, 2));
  return plan->AddBytes("model.workspace", workspace);
}

#undef Q4T_PLAN_TRY
}  // namespace q4t::runtime
