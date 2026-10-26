#include "q4t/runtime/startup_plan.h"

#include <algorithm>
#include <cstdlib>
#include <limits>

#include "q4t/io/model_config.h"
#include "q4t/model/gemv.h"
#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"
#include "q4t/runtime/memory_budget.h"
#include "q4t/runtime/residency_config.h"
#include "q4t/server/server_options.h"

namespace q4t::runtime {
namespace {
Status Product(std::initializer_list<int64_t> shape, size_t element_bytes,
               size_t* bytes) {
  if (element_bytes == 0) return Status::Fail("zero element size");
  size_t value = element_bytes;
  for (int64_t extent : shape) {
    if (extent <= 0 || static_cast<uint64_t>(extent) >
                           std::numeric_limits<size_t>::max() / value) {
      return Status::Fail("nonpositive or overflowing allocation shape");
    }
    value *= static_cast<size_t>(extent);
  }
  *bytes = value;
  return Status();
}

Status CheckArchitecture(const model::ModelConfig& cfg) {
  io::ModelConfig disk;
  Status status = io::ParseModelConfig(cfg.model_dir + "/config.json", &disk);
  if (!status.ok()) return status;
  if (disk.num_hidden_layers != cfg.num_layers || disk.hidden_size != cfg.hs ||
      disk.hc_count != cfg.hc || disk.hc_lowrank != cfg.lowrank ||
      disk.vocab_size != cfg.vocab || disk.num_experts != cfg.E ||
      disk.num_experts_per_tok != cfg.topk ||
      disk.moe_intermediate_size != cfg.moe_is ||
      disk.shared_expert_intermediate_size != cfg.shared_is ||
      disk.num_attention_heads != 24 || disk.num_key_value_heads != 2 ||
      disk.head_dim != 256 || disk.linear_num_key_heads != 16 ||
      disk.linear_num_value_heads != 48 || disk.linear_key_head_dim != 128 ||
      disk.linear_value_head_dim != 128 || disk.linear_conv_kernel_dim != 4 ||
      disk.indexer_head_dim != 128 || disk.indexer_kv_heads != 1 ||
      disk.indexer_n_heads != 4 || disk.indexer_budget != 2048 ||
      disk.indexer_compress_ratio != 4 || disk.ple_embed_dim != cfg.hs ||
      disk.ple_conv_kernel_size != 4 || disk.ngram_size != cfg.ple_ngram_size ||
      disk.heads_per_ngram != cfg.ple_heads_per_ngram ||
      // The exported config uses one-based PLE ids; ParseModelConfig retains
      // them verbatim, while LoadDecoderLayer selects zero-based layer 1.
      disk.ple_layer_ids != std::vector<int64_t>{2}) {
    return Status::Fail(
        "startup plan: checkpoint/runtime architecture mismatch");
  }
  for (int layer = 0; layer < cfg.num_layers; ++layer) {
    if (disk.IsFullAttention(layer) != (layer % 4 == 3)) {
      return Status::Fail("startup plan: unsupported attention layer layout");
    }
  }
  return Status();
}
}  // namespace

Status StartupPlan::AddBytes(const std::string& name, size_t bytes) {
  if (name.empty() || bytes == 0 ||
      bytes > std::numeric_limits<size_t>::max() - device_bytes_) {
    return Status::Fail("startup plan: invalid allocation " + name);
  }
  if (std::any_of(entries_.begin(), entries_.end(),
                  [&](const auto& entry) { return entry.name == name; })) {
    return Status::Fail("startup plan: duplicate allocation " + name);
  }
  entries_.push_back({name, bytes});
  device_bytes_ += bytes;
  return Status();
}

Status StartupPlan::Add(const std::string& name,
                        std::initializer_list<int64_t> shape,
                        size_t element_bytes) {
  size_t bytes = 0;
  Status status = Product(shape, element_bytes, &bytes);
  if (!status.ok()) return Status::Fail(name + ": " + status.message());
  return AddBytes(name, bytes);
}

Status StartupPlan::CheckTensor(const io::WeightLoader& loader,
                                const std::string& name, io::Dtype dtype,
                                std::initializer_list<int64_t> shape) const {
  size_t bytes = 0;
  Status status = Product(shape, io::DtypeSize(dtype), &bytes);
  if (!status.ok()) return Status::Fail(name + ": " + status.message());
  const io::TensorInfo* tensor = loader.FindTensor(name);
  if (!tensor || tensor->dtype != dtype ||
      tensor->shape != std::vector<int64_t>(shape) ||
      tensor->byte_size() != bytes || tensor->data_end < tensor->data_start ||
      tensor->data_end - tensor->data_start != bytes) {
    return Status::Fail("startup plan: missing or mismatched tensor " + name);
  }
  return Status();
}

Status StartupPlan::AddTensor(const io::WeightLoader& loader,
                              const std::string& name, io::Dtype dtype,
                              std::initializer_list<int64_t> shape) {
  Status status = CheckTensor(loader, name, dtype, shape);
  if (!status.ok()) return status;
  return Add(name, shape, io::DtypeSize(dtype));
}

Status CheckStartupPlanSupport(const model::ModelConfig& cfg,
                               const server::ServerOptions& opts) {
  // Bound dimensions before invoking existing workspace formulas, some of
  // which use int intermediates. A new architecture requires its own contract.
  if (cfg.num_layers != 48 || cfg.hs != 2560 || cfg.hc != 4 ||
      cfg.lowrank != 320 || cfg.E != 512 || cfg.moe_is != 640 ||
      cfg.shared_is != 640 || cfg.topk != 10 || cfg.vocab != 248320 ||
      cfg.max_len < 1 || cfg.max_len > 262144 || cfg.max_seq < 1 ||
      cfg.max_seq > 64 || cfg.max_prefill < 1 || cfg.max_prefill > 8192 ||
      cfg.max_seq > cfg.max_prefill || cfg.ple_ngram_size != 3 ||
      cfg.ple_heads_per_ngram != 8 ||
      cfg.ple_capacity_tokens != 8192 || cfg.ple_row_bytes != 160 ||
      cfg.ple_total_rows != 320001536) {
    return Status::Fail("startup plan: unsupported dimensions or capacity");
  }
  if (opts.allow_media || !opts.moe_trace_dir.empty()) {
    return Status::Fail("startup plan: media and router capture unsupported");
  }
  for (auto part :
       {model::Fp8Part::kAttn, model::Fp8Part::kGdn, model::Fp8Part::kMoeShared,
        model::Fp8Part::kHc, model::Fp8Part::kLmHead}) {
    if (model::Fp8ProjEnabled(part)) {
      return Status::Fail("startup plan: FP8 projection shadows unsupported");
    }
  }
  // Match the planner's raw environment contract as well as the runtime's
  // cached projection selection, so auto never starts an unsupported plan.
  for (const char* name : {"Q4T_FP8_PROJ", "Q4T_FP8_ATTN", "Q4T_FP8_GDN",
                           "Q4T_FP8_LMHEAD", "Q4T_FP8_SHARED", "Q4T_FP8_HC"}) {
    const char* value = std::getenv(name);
    if (value && *value && !(value[0] == '0' && value[1] == '\0')) {
      return Status::Fail("startup plan: FP8 projection shadows unsupported");
    }
  }
  if (!opts.no_mtp && (opts.mtp_k < 1 || opts.mtp_k > 16 ||
                       cfg.max_prefill < std::max(4, cfg.max_seq * (opts.mtp_k + 1)) ||
                       cfg.max_len < opts.mtp_k + 1)) {
    return Status::Fail("startup plan: unsupported MTP capacity");
  }
  return Status();
}

Status BuildStartupPlan(const model::ModelConfig& cfg,
                        const server::ServerOptions& opts,
                        StartupPlan* output) {
  if (!output) return Status::Fail("startup plan: null output");
  Status status = CheckStartupPlanSupport(cfg, opts);
  if (!status.ok()) return status;
  status = CheckArchitecture(cfg);
  if (!status.ok()) return status;
  ResidencyConfig residency;
  status = LoadResidencyConfig(cfg.moe_hot_list, cfg.num_layers, cfg.E,
                               cfg.moe_resident_slots, &residency);
  if (!status.ok()) return status;
  StartupPlan plan;
  status = PlanMainResources(cfg, residency, &plan);
  if (!status.ok()) return status;
  if (!opts.no_mtp) {
    mtp::MtpConfig mtp;
    mtp.mtp_dir = cfg.model_dir + "/mtp";
    mtp.subset_head_dir = opts.mtp_subset_head;
    mtp.max_len = cfg.max_len;
    mtp.max_seq = cfg.max_seq;
    mtp.max_prefill = cfg.max_prefill;
    mtp.initial_scratch_capacity = opts.mtp_k + 1;
    status = PlanMtpResources(mtp, &plan);
    if (!status.ok()) return status;
    status = PlanVerifyResources(cfg, opts.mtp_k, &plan);
    if (!status.ok()) return status;
  }
  for (const char* name :
       {"scheduler.logits", "scheduler.tokens", "scheduler.prefill_logits"}) {
    const bool tokens = std::string(name) == "scheduler.tokens";
    status = plan.Add(name, {cfg.max_seq, tokens ? 1 : cfg.vocab},
                      tokens ? sizeof(int32_t) : sizeof(uint16_t));
    if (!status.ok()) return status;
  }
  const size_t physical_ram = ReadMemTotal();
  if (physical_ram == 0 || plan.device_bytes() > physical_ram) {
    return Status::Fail("startup plan: device allocations exceed physical RAM");
  }
  *output = std::move(plan);
  return Status();
}
}  // namespace q4t::runtime
