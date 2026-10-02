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
#include <string>
#include <thread>
#include <vector>
#include <cuda_runtime.h>
#include "q4t/io/json.h"
#include "q4t/io/weight_loader.h"
#include "q4t/quant/moe_residency.h"
#include "q4t/runtime/memory_budget.h"
#include "q4t/runtime/residency_config.h"
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
  // RAM limit: model cache, allocator retention and driver ownership remain
  // separately measured. An infeasible estimate must stop startup.
  {
    PhaseTimer pt("budget");
    if (!opts.no_budget) {
      const size_t mem_total = runtime::ReadMemTotal();
      if (mem_total == 0) {
        return Status::Fail("could not read MemTotal from /proc/meminfo");
      }
      // The index includes optional MTP/vision tensors, so this remains a
      // weight estimate rather than the enabled device-allocation total.
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
        mtp::MtpConfig mcfg;
        mcfg.max_prefill = cfg.max_prefill;
        mcfg.max_len = full.max_len;
        breq.mtp_workspace_bytes =
            mtp::MtpWorkspaceBytes(mcfg, cfg.max_prefill, full);
      }
      runtime::BudgetModelParams params;
      params.num_layers = cfg.num_layers;
      params.hs = cfg.hs;
      params.hc = cfg.hc;
      params.vocab = cfg.vocab;
      params.has_mtp = capabilities.mtp;
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

  // A valid budget is always feasible here. Never substitute the original
  // request for a zero-capacity result. --no-budget is an explicit bypass.
  const int eff_max_len = budget_valid_ ? budget_.max_len : opts.max_len;
  const int eff_max_seq = budget_valid_ ? budget_.max_seq : opts.max_seq;
  if (eff_max_len > 0) cfg.max_len = eff_max_len;
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
  // model. On failure the server falls back to plain decode (mirrors the CLI
  // --mtp behavior). Skipped entirely when opts.no_mtp is set.
  if (!capabilities.mtp) {
    std::fprintf(stderr, "[q4t] MTP disabled; plain decode\n");
  } else {
    PhaseTimer pt("mtp_load");
    mtp::MtpConfig mcfg;
    mcfg.mtp_dir = opts.model_dir + "/mtp";
    mcfg.max_prefill = cfg.max_prefill;  // MTP draft-extend runs over the whole
                                         // prompt; size its workspace to match.
    mcfg.max_len = cfg.max_len;  // draft full-attn KV/indexer tracks the main
                                 // sequence positions; size to the same length.
    // Stage 2c: pool the draft full-attention KV/indexer for max_seq sequences
    // so concurrent MTP requests own independent draft-state slices (Stage 1).
    mcfg.max_seq = max_seq_;
    s = mtp::LoadMtp(mcfg, model_.Get().head.embed_tokens, model_.Get().head.lm_head,
                     &mtp_, nullptr);
    if (!s.ok()) {
      std::fprintf(stderr, "[q4t] MTP load failed (%s); plain decode only\n",
                   s.message().c_str());
    } else {
      mtp_loaded_ = true;
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
