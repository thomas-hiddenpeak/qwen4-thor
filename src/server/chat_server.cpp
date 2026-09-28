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
#include "q4t/runtime/memory_budget.h"
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
  in_addr address{};
  if (::inet_pton(AF_INET, opts.host.c_str(), &address) != 1) {
    return Status::Fail("host must be a numeric IPv4 address");
  }
  if (opts.port < 1 || opts.port > 65535) {
    return Status::Fail("port must be in [1,65535]");
  }
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

  // OOM-safe memory budget (vllm-style gpu_memory_utilization). Compute the
  // maximum (max_len, max_seq) that fits within mem_fraction x MemTotal BEFORE
  // any allocation, and cap the user's request to it. This is what makes an
  // over-aggressive config (e.g. --max-len 262144 --max-seq 8) safe instead of
  // OOM-rebooting the unified-memory box. Skipped when opts.no_budget is set.
  {
    PhaseTimer pt("budget");
    if (!opts.no_budget) {
    const size_t mem_total = runtime::ReadMemTotal();
    if (mem_total == 0) {
      return Status::Fail("could not read MemTotal from /proc/meminfo");
    }
    // Weights: the index's total_size is the exact GPU weight byte count
    // (main + MTP + vision). Open the index read-only just to read it.
    size_t weights = 0;
    {
      io::WeightIndex* idx = nullptr;
      if (io::WeightIndex::Open(
               opts.model_dir + "/model.safetensors.index.json", &idx)
              .ok()) {
        weights = idx->total_size();
        delete idx;
      }
    }
    if (weights == 0) {
      return Status::Fail(
          "could not read weight total_size from model index (budget)");
    }
    runtime::BudgetRequest breq;
    breq.mem_fraction = opts.mem_fraction;
    breq.max_len = opts.max_len;
    breq.max_seq = opts.max_seq > 0 ? opts.max_seq : 8;
    breq.max_prefill =
        opts.max_prefill > 0 ? opts.max_prefill : 8192;
    budget_ = runtime::ComputeMemoryBudget(runtime::BudgetModelParams{},
                                           breq, weights, mem_total);
    budget_valid_ = true;
    std::fputs(budget_.report.c_str(), stderr);
    }
  }  // budget phase

  // Effective (possibly budget-capped) max_len / max_seq.
  const int eff_max_len =
      budget_valid_ && budget_.max_len > 0 ? budget_.max_len : opts.max_len;
  const int eff_max_seq =
      budget_valid_ && budget_.max_seq > 0 ? budget_.max_seq : opts.max_seq;

  model::ModelConfig cfg;
  cfg.model_dir = opts.model_dir;
  cfg.index_path = opts.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = opts.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  if (opts.max_prefill > 0) cfg.max_prefill = opts.max_prefill;
  if (eff_max_len > 0) cfg.max_len = eff_max_len;
  // B1: pool the per-sequence recurrent state for up to max_seq concurrent
  // requests. Each in-flight request owns one seq_id.
  max_seq_ = eff_max_seq > 0 ? eff_max_seq : 8;
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
  if (opts.no_mtp) {
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
  {
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
  }

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
