#include "q4t/mtp/subset_head.h"
#include "q4t/mtp/suffix_drafter.h"
// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"

#include <arpa/inet.h>
#include <sys/socket.h>
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <limits>
#include <new>
#include <string>
#include <thread>
#include <vector>
#include <cuda_runtime.h>
#include "q4t/io/json.h"
#include "q4t/io/weight_loader.h"
#include "q4t/quant/moe_residency.h"
#include "q4t/runtime/memory_budget.h"
#include "q4t/runtime/residency_config.h"
#if defined(Q4T_STARTUP_RESOURCE_PLAN) || defined(Q4T_STARTUP_RESERVATION)
#include "q4t/runtime/startup_plan.h"
#endif
#include "q4t/vision/vision.h"

namespace q4t::server {

namespace {
constexpr const char* kDefaultModelName = "qwen3.8-flash-next";

}

ChatServer::~ChatServer() {
  // Release any requests queued in AllocSeqId so their threads can exit.
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    seq_stopping_ = true;
  }
  seq_cv_.notify_all();
  // B2b: stop the scheduler thread FIRST (it touches model_.Get() + d_sched_logits_
  // under model_mu_, so it must be joined before those are freed).
  StopScheduler();
  model_.Get().router_trace = nullptr;
  router_trace_.reset();
  if (d_sched_logits_) {
    cudaFree(d_sched_logits_);
    d_sched_logits_ = nullptr;
  }
  if (d_sched_tokens_) {
    cudaFree(d_sched_tokens_);
    d_sched_tokens_ = nullptr;
  }
  if (d_prefill_logits_) {
    cudaFree(d_prefill_logits_);
    d_prefill_logits_ = nullptr;
  }
  if (vision_tower_) {
    vision_tower_->Free();
    vision_tower_.reset();
  }
  // MTP draft model: free its device weights before model_.Get() (the borrowed
  // embed/lm_head point into model_.Get()'s memory). Then the rolling draft-trunk
  // buffers.
  if (mtp_loaded_) {
    mtp_.Free();
    mtp_loaded_ = false;
  }
  if (mtp_suffix_) {
    const size_t bytes = suffix_token_cache_.MemoryBytes();
    suffix_token_cache_.Reset();
    const auto& c = suffix_token_cache_.Counters();
    std::fprintf(stderr,
                 "[suffix-classes] event=release bytes=%zu peak_bytes=%zu "
                 "checks=%llu lookups=%llu decodes=%llu unknown=%llu "
                 "allocations=%llu allocation_ns=%llu release_ns=%llu\n",
                 bytes, c.peak_bytes,
                 static_cast<unsigned long long>(c.checks),
                 static_cast<unsigned long long>(c.lookups),
                 static_cast<unsigned long long>(c.decodes),
                 static_cast<unsigned long long>(c.unknown),
                 static_cast<unsigned long long>(c.allocations),
                 static_cast<unsigned long long>(c.allocation_ns),
                 static_cast<unsigned long long>(c.release_ns));
  }
}

namespace {
// Per-phase startup timer: prints "[q4t][startup] <phase> <ms> ms" on scope
// exit. Industrial-grade startup observability — makes it trivial to see which
// phase dominates and to track startup-time regressions.
struct PhaseTimer {
  const char* phase;
  std::chrono::steady_clock::time_point t0;
  explicit PhaseTimer(const char* p) : phase(p), t0(std::chrono::steady_clock::now()) {}
  ~PhaseTimer() {
    const double ms =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
            .count();
    std::fprintf(stderr, "[q4t][startup] %s %.0f ms\n", phase, ms);
  }
};
}  // namespace

Status ChatServer::Start(const ServerOptions& opts) {
  // Validate before loading the tokenizer or allocating model resources.
  const Status validated = ValidateServerOptions(opts);
  if (!validated.ok()) return validated;
  const ServerCapabilities capabilities = CapabilitiesFor(opts);
  std::fprintf(stderr,
               "[q4t][capabilities] requested=%s mtp=%d media=%d max_seq=%d\n",
               capabilities.Experimental() ? "experimental" : "text-greedy",
               capabilities.mtp, capabilities.media, opts.max_seq);
  host_ = opts.host;
  allow_media_ = opts.allow_media;
  mtp_k_ = opts.mtp_k;
  mtp_suffix_ = opts.mtp_suffix;
  if (!opts.session_cache_dir.empty()) {
    const Status cache_status = session_cache_.Open(opts.session_cache_dir);
    if (!cache_status.ok()) return cache_status;
    std::fprintf(stderr,
                 "[session-cache] enabled=1 entries=8 disk_bytes=%zu "
                 "ram_budget=%zu staging_bytes=%zu ttl_seconds=1800\n",
                 SessionCache::kDiskBudget, SessionCache::kRamBudget,
                 SessionCache::kStagingBytes);
  }
  if (!opts.prefix_cache_dir.empty()) {
    const Status cache_status = prefix_cache_.Open(
        opts.prefix_cache_dir, 1024, size_t{8} << 30);
    if (!cache_status.ok()) return cache_status;
    prefix_cache_checkpoints_ = opts.prefix_cache_checkpoints;
    std::fprintf(stderr,
                 "[prefix-cache] enabled=1 entries=8 disk_bytes=8589934592 "
                 "ram_budget=%zu staging_bytes=%zu ttl_seconds=1800\n",
                 SessionCache::kRamBudget, SessionCache::kStagingBytes);
  }
  PhaseTimer total("startup_total");
  {
    PhaseTimer pt("tokenizer");
    text::TokenizerLimits limits;
    Status s = text::Tokenizer::Load(opts.model_dir + "/tokenizer.json", limits,
                                     &tok_);
    if (!s.ok()) {
      return Status::Fail("tokenizer load failed: " + s.message());
    }
  }
  Status s;

  model::ModelConfig cfg;
  cfg.model_dir = opts.model_dir;
  cfg.index_path = opts.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = opts.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  if (opts.max_prefill > 0) cfg.max_prefill = opts.max_prefill;
  cfg.moe_resident_slots = opts.moe_resident_slots;
  cfg.moe_hot_list = opts.moe_hot_list;
  cfg.moe_hot_protect = opts.moe_hot_protect;
  runtime::ResidencyConfig residency_config;
  s = runtime::LoadResidencyConfig(cfg.moe_hot_list, cfg.num_layers, cfg.E,
                                    cfg.moe_resident_slots, &residency_config);
  if (!s.ok()) return s;

  // Estimate allocation capacity before model load. This is not a physical
  // RAM limit: file cache, driver ownership and other processes need separate
  // accounting. An infeasible estimate must stop startup.
  {
    PhaseTimer pt("budget");
    if (!opts.no_budget) {
      const size_t mem_total = runtime::ReadMemTotal();
      if (mem_total == 0) {
        return Status::Fail("could not read MemTotal from /proc/meminfo");
      }
      // Keep the whole index estimate, including optional tensors. This is
      // not an exact count of the weights loaded by the enabled capabilities.
      size_t weights = 0;
      {
        io::WeightIndex* idx = nullptr;
        if (io::WeightIndex::Open(cfg.index_path, &idx).ok()) {
          weights = idx->total_size();
          delete idx;
        }
      }
      if (weights == 0) {
        return Status::Fail("could not read weight total_size (budget)");
      }
      runtime::BudgetRequest breq;
      breq.mem_fraction = opts.mem_fraction;
      breq.max_len = opts.max_len;
      breq.max_seq = opts.max_seq;
      breq.seq_capacities = opts.seq_capacities;
      breq.max_prefill = cfg.max_prefill;
      if (opts.moe_resident_slots > 0) {
        const size_t slot_bytes =
            quant::MoEResidencyLayerBytes(cfg.hs, cfg.moe_is, 1);
        const size_t offloaded =
            static_cast<size_t>(cfg.E) * cfg.num_layers -
            residency_config.total_slots;
        if (offloaded > weights / slot_bytes) {
          return Status::Fail("weight index smaller than offloaded estimate");
        }
        weights -= offloaded * slot_bytes;
        // Staging is part of L2, not an additional worker-sized allocation.
        breq.extra_fixed_bytes =
            static_cast<size_t>(cfg.num_layers) *
            (quant::MoEResidencyL2Slots() *
                 quant::MoEResidencyStagingBytes(cfg.hs, cfg.moe_is) +
             quant::MoEResidencyMirrorK() *
                 quant::MoEResidencyMirrorBytes(cfg.hs, cfg.moe_is));
      }
      // Reuse allocation sizing functions. For auto/capped length, use the
      // requested upper bound; small-T attention scratch can depend on it.
      model::FullAttentionWeights full;
      full.max_len = opts.max_len > 0 ? std::min(opts.max_len, 262144) : 262144;
      breq.main_workspace_bytes =
          model::ModelHeadWorkspaceBytes(cfg.max_prefill, cfg.hs);
      for (int layer = 0; layer < cfg.num_layers; ++layer) {
        breq.main_workspace_bytes = std::max(
            breq.main_workspace_bytes,
            model::DecoderLayerWorkspaceBytes(
                cfg.max_prefill, layer % 4 == 3, layer == 1, cfg.hs, cfg.E,
                cfg.moe_is, cfg.shared_is, cfg.topk, &full, cfg.lowrank));
      }
      if (capabilities.mtp) {
        if (prefix_cache_.Enabled())
          breq.extra_fixed_bytes += SessionCache::kRamBudget;
        if (session_cache_.Enabled())
          breq.extra_fixed_bytes += SessionCache::kRamBudget;
        if (mtp_suffix_)
          breq.extra_fixed_bytes +=
              size_t(opts.max_seq) * mtp::SuffixDrafter::kMaxMemoryBytes +
              SuffixTokenCache::kMaxBytes;
        mtp::MtpConfig mcfg;
        mcfg.mtp_dir = opts.model_dir + "/mtp";
        mcfg.max_prefill = cfg.max_prefill;
        mcfg.max_len = full.max_len;
        breq.mtp_workspace_bytes =
            mtp::MtpWorkspaceBytes(mcfg, cfg.max_prefill, full);
        breq.mtp_k = mtp_k_;
        if (!opts.mtp_subset_head.empty()) {
          breq.extra_fixed_bytes += mtp::kSubsetReadCacheBudget;
          breq.mtp_weights_bytes += mtp::kSubsetWeightBytes + mtp::kSubsetIdBytes;
          breq.mtp_load_host_bytes = mtp::kSubsetWeightBytes + mtp::kSubsetIdBytes;
        }
        io::WeightIndex* raw_index = nullptr;
        s = io::WeightIndex::Open(
            mcfg.mtp_dir + "/model.safetensors.index.json", &raw_index);
        if (!s.ok()) return Status::Fail("MTP budget index: " + s.message());
        const std::unique_ptr<io::WeightIndex> mtp_index(raw_index);
        io::WeightLoader* raw_loader = nullptr;
        s = io::WeightLoader::Create(mcfg.mtp_dir, *mtp_index, 1, &raw_loader);
        if (!s.ok()) return Status::Fail("MTP budget loader: " + s.message());
        const std::unique_ptr<io::WeightLoader> mtp_loader(raw_loader);
#ifdef Q4T_STARTUP_RESOURCE_PLAN
        // Metadata preflight must not evict the cache under investigation.
        mtp_loader->SetKeepPageCache(true, "");
#elif defined(Q4T_STARTUP_RESERVATION)
        // Header inspection must not clear an inherited hot startup state.
        if (opts.startup_plan != StartupPlanMode::kOff) {
          mtp_loader->SetKeepPageCache(true, "");
        }
#endif
        // Sum mapped tensor bytes from shard headers: the exported index's
        // total_size does not match the tensors listed in its MTP weight_map.
        // The loader reads each weight through one host vector; gate_up
        // retains its capacity while loading down_proj. No payload is read.
        for (const auto& group : mtp_index->ShardGroups()) {
          for (const auto& name : group.second) {
            const io::TensorInfo* tensor = mtp_loader->FindTensor(name);
            if (!tensor) return Status::Fail("MTP budget tensor: " + name);
            const size_t bytes = static_cast<size_t>(tensor->byte_size());
            if (bytes > std::numeric_limits<size_t>::max() -
                            breq.mtp_weights_bytes)
              return Status::Fail("MTP budget weight bytes overflow");
            breq.mtp_weights_bytes += bytes;
            breq.mtp_load_host_bytes =
                std::max(breq.mtp_load_host_bytes, bytes);
          }
        }
      }
      runtime::BudgetModelParams params;
      params.num_layers = cfg.num_layers;
      params.hs = cfg.hs;
      params.hc = cfg.hc;
      params.vocab = cfg.vocab;
      params.has_mtp = capabilities.mtp;
      params.mtp_experts = cfg.E;
      params.mtp_moe_is = cfg.moe_is;
      params.has_ple = cfg.num_layers > 1;
      params.ple_capacity_tokens = cfg.ple_capacity_tokens;
      params.ple_row_bytes = static_cast<int>(cfg.ple_row_bytes);
      params.ple_heads = (cfg.ple_ngram_size - 1) * cfg.ple_heads_per_ngram;
      budget_ = runtime::ComputeMemoryBudget(params, breq, weights, mem_total);
      budget_valid_ = true;
      std::fputs(budget_.report.c_str(), stderr);
      if (!budget_.feasible) {
        return Status::Fail("no feasible allocation budget: " + budget_.reason);
      }
    }
  }

  // A computed budget is always feasible here. Never substitute the original
  // request for a zero-capacity result. --no-budget is an explicit bypass.
  const int eff_max_len = budget_valid_ ? budget_.max_len : opts.max_len;
  const int eff_max_seq = budget_valid_ ? budget_.max_seq : opts.max_seq;
  if (eff_max_len > 0) cfg.max_len = eff_max_len;
  if (mtp_suffix_ &&
      cfg.max_len > static_cast<int>(mtp::SuffixDrafter::kMaxCapacity))
    return Status::Fail("mtp-suffix effective max-len exceeds 262144");
  if (mtp_suffix_) {
    const Status configured =
        suffix_token_cache_.Configure(tok_.get(), cfg.vocab);
    if (!configured.ok()) return configured;
    std::fprintf(stderr,
                 "[suffix-classes] event=configure bytes=0 vocab=%d "
                 "cap_bytes=%zu object_bytes=%zu lazy=1\n",
                 cfg.vocab, SuffixTokenCache::kMaxBytes,
                 sizeof(SuffixTokenCache));
    std::fprintf(stderr,
                 "[q4t] suffix enabled: k=3 match=3..64 candidates=4 "
                 "capacity=%d global_ids=1 numeric_gate=1 "
                 "min_admitted_match=%d lazy_classes=1\n", cfg.max_len,
                 mtp::SuffixDrafter::kMinAdmittedMatch);
  }
  std::fprintf(stderr,
               "[q4t][capacity] requested_max_len=%d requested_max_seq=%d "
               "requested_max_prefill=%d effective_max_len=%d "
               "effective_max_seq=%d effective_max_prefill=%d "
               "budget_enabled=%d budget_feasible=%s\n",
               opts.max_len, opts.max_seq, opts.max_prefill, cfg.max_len,
               eff_max_seq, cfg.max_prefill, budget_valid_,
               budget_valid_ ? "true" : "not_evaluated");
  residency_enabled_ = opts.moe_resident_slots > 0;
  request_deadline_ms_ = opts.request_deadline_ms;
  // B1: pool the per-sequence recurrent state for up to max_seq concurrent
  // requests. Each in-flight request owns one seq_id.
  max_seq_ = eff_max_seq;
  cfg.max_seq = max_seq_;
  cfg.seq_capacities = opts.seq_capacities;
  seq_capacities_ = opts.seq_capacities;
  if (!seq_capacities_.empty()) {
    std::fprintf(stderr, "[q4t][slots] capacities=");
    for (size_t i = 0; i < seq_capacities_.size(); ++i)
      std::fprintf(stderr, "%s%d", i ? "," : "", seq_capacities_[i]);
    std::fprintf(stderr, " stream_mtp_prefill=%d text_admission=32\n",
                 capabilities.mtp);
  }
  bool planned_startup = false;
#ifdef Q4T_STARTUP_RESOURCE_PLAN
  {
    PhaseTimer pt("startup_resource_plan");
    s = runtime::PrepareStartupReservation(cfg, opts);
    if (!s.ok()) return s;
    planned_startup = true;
  }
#elif defined(Q4T_STARTUP_RESERVATION)
  if (opts.startup_plan == StartupPlanMode::kOff) {
    std::fprintf(stderr, "[q4t][startup-plan] mode=off active=0\n");
  } else {
    const Status supported = runtime::CheckStartupPlanSupport(cfg, opts);
    if (!supported.ok()) {
      if (opts.startup_plan == StartupPlanMode::kRequired) return supported;
      std::fprintf(stderr,
                   "[q4t][startup-plan] mode=auto active=0 reason=%s\n",
                   supported.message().c_str());
    } else {
      PhaseTimer pt("startup_resource_plan");
      s = startup_reservation_.Prepare(cfg, opts);
      if (!s.ok()) return s;
      planned_startup = true;
      std::fprintf(stderr, "[q4t][startup-plan] mode=%s active=1\n",
                   opts.startup_plan == StartupPlanMode::kRequired
                       ? "required" : "auto");
    }
  }
#endif
  seq_free_.assign(static_cast<size_t>(max_seq_), true);
  conn_cap_ = std::max(max_seq_ * 8, 128);  // in-flight request-thread cap
  {
    PhaseTimer pt("model_load");
    s = model_.Load(cfg, nullptr);
    if (!s.ok()) {
      tok_.reset();
      return Status::Fail("model load failed: " + s.message());
    }
  }

  // Generation EOS may contain several IDs. Keep the model's primary EOS
  // unchanged: PLE uses that ID to pad the beginning of its n-gram history.
  stop_token_ids_ = {static_cast<int32_t>(model_.Get().cfg.eos_token_id)};
  {
    errno = 0;
    std::ifstream input(opts.model_dir + "/generation_config.json");
    if (!input.is_open()) {
      if (errno != ENOENT)
        return Status::Fail("cannot read generation_config.json");
    } else {
      const std::string text((std::istreambuf_iterator<char>(input)),
                             std::istreambuf_iterator<char>());
      if (input.bad())
        return Status::Fail("cannot read generation_config.json");
      io::Json generation;
      s = io::ParseJson(text, &generation);
      if (!s.ok() || !generation.IsObject())
        return Status::Fail("invalid generation_config.json");
      const io::Json* ids = generation.Find("eos_token_id");
      if (ids && !ids->IsNull()) {
        std::vector<int32_t> configured;
        const auto append = [&](const io::Json& value) {
          if (!value.IsNumber() || value.number < 0 ||
              value.number >= model_.Get().cfg.vocab ||
              value.number != static_cast<int32_t>(value.number))
            return false;
          const int32_t id = static_cast<int32_t>(value.number);
          if (std::find(configured.begin(), configured.end(), id) ==
              configured.end())
            configured.push_back(id);
          return true;
        };
        if (ids->IsArray()) {
          for (const io::Json& id : ids->array)
            if (!append(id))
              return Status::Fail("invalid generation eos_token_id");
        } else if (!append(*ids)) {
          return Status::Fail("invalid generation eos_token_id");
        }
        if (configured.empty())
          return Status::Fail("empty generation eos_token_id");
        stop_token_ids_ = std::move(configured);
      }
    }
  }
  std::fprintf(stderr, "[q4t] generation stop token ids:");
  for (int32_t id : stop_token_ids_) std::fprintf(stderr, " %d", id);
  std::fprintf(stderr, "\n");

  // Load the MTP draft model (optional). Borrowed embed/lm_head from the main
  // model. Legacy loading falls back to plain decode on failure; a selected
  // startup plan must fail because its resource contract includes MTP.
  // Skipped entirely when opts.no_mtp is set.
  if (!capabilities.mtp) {
    std::fprintf(stderr, "[q4t] MTP disabled; plain decode\n");
  } else {
    PhaseTimer pt("mtp_load");
    mtp::MtpConfig mcfg;
    mcfg.mtp_dir = opts.model_dir + "/mtp";
    mcfg.subset_head_dir = opts.mtp_subset_head;
    mcfg.max_prefill = cfg.max_prefill;  // MTP draft-extend runs over the whole
                                         // prompt; size its workspace to match.
    mcfg.max_len = cfg.max_len;  // draft full-attn KV/indexer tracks the main
                                 // sequence positions; size to the same length.
    // Stage 2c: pool the draft full-attention KV/indexer for max_seq sequences
    // so concurrent MTP requests own independent draft-state slices (Stage 1).
    mcfg.max_seq = max_seq_;
    mcfg.seq_capacities = cfg.seq_capacities;
    if (planned_startup) mcfg.initial_scratch_capacity = mtp_k_ + 1;
    s = mtp::LoadMtp(mcfg, model_.Get().head.embed_tokens, model_.Get().head.lm_head,
                     &mtp_, nullptr);
    if (!s.ok()) {
      if (planned_startup || !opts.mtp_subset_head.empty()) {
        return Status::Fail("planned MTP load failed: " + s.message());
      }
      std::fprintf(stderr, "[q4t] MTP load failed (%s); plain decode only\n",
                   s.message().c_str());
    } else {
      mtp_loaded_ = true;
      if (planned_startup) {
        s = model::ModelReserveVerifyCheckpoints(model_.Get(), mtp_k_);
        if (!s.ok()) return s;
      }
      std::fprintf(stderr, "[q4t] MTP loaded (k=%d max_seq=%d)\n", mtp_k_,
                   max_seq_);
    }
  }

  // Load the vision tower (multimodal). Optional: if the checkpoint has no
  // "model.visual." tensors, skip gracefully (the server then serves text
  // only and rejects image parts). The loader is read-only (mmap) and is
  // released after the weights are copied to device.
  if (capabilities.media) {
    PhaseTimer pt("vision_load");
    io::WeightIndex* index = nullptr;
    s = io::WeightIndex::Open(cfg.index_path, &index);
    if (s.ok()) {
      io::WeightLoader* loader = nullptr;
      s = io::WeightLoader::Create(opts.model_dir, *index, 8, &loader);
      if (s.ok()) {
        vision::VisionConfig vcfg;  // defaults match this model's vision_config
        auto tower = std::make_unique<vision::VisionTower>();
        std::string verr;
        if (vision::LoadVision(*loader, vcfg, tower.get(), &verr, nullptr)) {
          vision_tower_ = std::move(tower);
          std::fprintf(stderr, "[q4t] vision tower loaded (%d blocks)\n",
                       vcfg.depth);
        } else {
          std::fprintf(stderr,
                       "[q4t] vision tower unavailable (%s); text-only mode\n",
                       verr.c_str());
        }
        delete loader;
      }
      delete index;
    }
  } else {
    std::fprintf(stderr, "[q4t] media disabled; vision tower not loaded\n");
  }
  std::fprintf(stderr,
               "[q4t][capabilities] effective mtp=%d media_allowed=%d "
               "vision_loaded=%d max_seq=%d\n",
               mtp_loaded_, allow_media_, vision_tower_ != nullptr, max_seq_);

  port_ = opts.port;
  max_tokens_default_ = opts.max_tokens;
  max_prefill_ = cfg.max_prefill;
  max_len_ = cfg.max_len;
  model_name_ = kDefaultModelName;
  // Video processor budget comes from video_preprocessor_config.json
  // (4096 / 25165824), NOT the image budget. Same patch/merge/temporal dims.
  video_proc_cfg_ = proc_cfg_;
  video_proc_cfg_.min_pixels = 4096;
  video_proc_cfg_.max_pixels = 25165824;

  if (!opts.moe_trace_dir.empty()) {
    if (max_seq_ != 1 || !opts.no_mtp || opts.allow_media ||
        std::getenv("Q4T_MOE_DUMP")) {
      std::fprintf(stderr, "[q4t][trace] unsupported mode; capture disabled\n");
    } else {
      auto capture = std::make_unique<trace::RouterCollector>();
      trace::RouterTraceConfig tc;
      tc.layers = cfg.num_layers; tc.experts = cfg.E;
      tc.top_k = cfg.topk; tc.max_rows = cfg.max_prefill;
      Status capture_status = capture->Start(
          opts.moe_trace_dir, opts.moe_trace_workload, cfg.index_path, tc,
          cfg.max_len, uint64_t(opts.moe_trace_max_mib) * 1024 * 1024);
      if (capture_status.ok()) {
        router_trace_ = std::move(capture);
        model_.Get().router_trace = router_trace_.get();
      } else {
        if (capture->HasCudaFailure()) return capture_status;
        std::fprintf(stderr, "[q4t][trace] disabled: %s\n",
                     capture_status.message().c_str());
      }
    }
  }

  // B2b continuous batching: the scheduler's packed-logits buffer (device
  // [max_seq, vocab]) + a GPU-argmax token buffer (device [max_seq] + host
  // mirror) + the scheduler thread.
  if (cudaMalloc(reinterpret_cast<void**>(&d_sched_logits_),
                 static_cast<size_t>(max_seq_) * cfg.vocab * 2) != cudaSuccess) {
    std::fprintf(stderr, "[q4t] scheduler logits alloc failed; plain decode\n");
  } else if (cudaMalloc(reinterpret_cast<void**>(&d_sched_tokens_),
                        static_cast<size_t>(max_seq_) * sizeof(int32_t)) !=
             cudaSuccess) {
    std::fprintf(stderr, "[q4t] scheduler tokens alloc failed; plain decode\n");
    cudaFree(d_sched_logits_);
    d_sched_logits_ = nullptr;
  } else {
    h_sched_tokens_.assign(static_cast<size_t>(max_seq_), 0);
    scheduler_thread_ = std::thread([this] { SchedulerLoop(); });
    scheduler_active_ = true;
    std::fprintf(stderr, "[q4t] continuous-batching scheduler started "
                         "(max_seq=%d)\n",
                 max_seq_);
  }
  // Every serve prefill consumes one row per sequence. The shared buffer
  // holds at most max_seq rows, and all accesses are under model_mu_.
  if (cudaMalloc(reinterpret_cast<void**>(&d_prefill_logits_),
                 static_cast<size_t>(max_seq_) * cfg.vocab * 2) !=
      cudaSuccess) {
    return Status::Fail("prefill logits buffer alloc failed (out of memory)");
  }
#ifdef Q4T_STARTUP_RESOURCE_PLAN
  s = runtime::CompleteStartupReservation();
  if (!s.ok()) return s;
#elif defined(Q4T_STARTUP_RESERVATION)
  if (planned_startup) {
    s = startup_reservation_.Complete();
    if (!s.ok()) return s;
  }
#endif
  return Status();
}

void ChatServer::RequestStop() {
  stop_requested_.store(true, std::memory_order_relaxed);
  // shutdown(2) is async-signal-safe and wakes the blocked accept(2).
  static_assert(std::atomic<int>::is_always_lock_free);
  const int fd = listen_fd_.load(std::memory_order_relaxed);
  if (fd >= 0) ::shutdown(fd, SHUT_RDWR);
}

void ChatServer::StopScheduler() {
  {
    const std::lock_guard<std::mutex> lock(sched_mu_);
    scheduler_stop_ = true;
  }
  sched_cv_.notify_all();
  if (scheduler_thread_.joinable()) scheduler_thread_.join();
}

}  // namespace q4t::server
