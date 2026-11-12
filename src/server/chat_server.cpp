#include "q4t/mtp/subset_head.h"
#include "q4t/mtp/suffix_drafter.h"
// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"
#include "q4t/server/file_expansion.h"
#include "q4t/server/file_input_policy.h"
#include "q4t/server/object_store_options.h"
#include "cuda_memory_backend.h"

#include <arpa/inet.h>
#include <sys/socket.h>
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <ctime>
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
#include "q4t/server/cuda_quota_policy.h"
#include "q4t/server/embedding_assets.h"
#if defined(Q4T_STARTUP_RESOURCE_PLAN) || defined(Q4T_STARTUP_RESERVATION)
#include "q4t/runtime/startup_plan.h"
#endif
#include "q4t/vision/vision.h"
#include "q4t/vision/configuration.h"

namespace q4t::server {

ChatServer::~ChatServer() {
  RequestStop();
  // Stop the bounded policy worker before destroying queue/client state.
  client_policy_admin_.reset();
  state_key_admin_.reset();
  // Start may have succeeded without a subsequent Run. Stop accepting TCP
  // connections before releasing the model, including partial startup state.
  startup_listeners_.Close();
  batches_.RequestStop();
  vector_jobs_.RequestStop();
  background_.RequestStop();
  // Release any requests queued in AllocSeqId so their threads can exit.
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    seq_stopping_ = true;
  }
  seq_cv_.notify_all();
  // B2b: stop the scheduler thread FIRST (it touches model_.Get() +
  // d_sched_logits_ under model_mu_, so it must be joined before those are
  // freed).
  StopScheduler();
  batches_.Join();
  vector_jobs_.Join();
  background_.Join();
  model_.Get().router_trace = nullptr;
  router_trace_.reset();
  const auto release = [&](auto*& pointer, const char* site) {
    const auto result = detail::ReleaseCudaMemory(pointer, site);
    RecordDeviceMemoryFailure(result, site);
    // Run() may already have returned success. Do not turn an unknown release
    // into a clean process exit or retry the same pointer in another owner.
    if (!result.ok()) std::_Exit(1);
  };
  release(d_sched_logits_, "shutdown.scheduler_logits");
  release(d_sched_tokens_, "shutdown.scheduler_tokens");
  release(d_prefill_logits_, "shutdown.prefill_logits");
  release(d_embedding_weights_, "shutdown.native_embedding_weights");
  embedding_ready_.store(false);
  if (vision_tower_) {
    vision_tower_->Free();
    vision_tower_.reset();
  }
  // MTP draft model: free its device weights before model_.Get() (the borrowed
  // embed/lm_head point into model_.Get()'s memory). Then the rolling
  // draft-trunk buffers.
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
                 bytes, c.peak_bytes, static_cast<unsigned long long>(c.checks),
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
  explicit PhaseTimer(const char* p)
      : phase(p), t0(std::chrono::steady_clock::now()) {}
  ~PhaseTimer() {
    const double ms = std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - t0)
                          .count();
    std::fprintf(stderr, "[q4t][startup] %s %.0f ms\n", phase, ms);
  }
};
}  // namespace

Status ChatServer::Start(const ServerOptions& opts) {
  if (start_attempted_) return Status::Fail("server startup already attempted");
  start_attempted_ = true;
  // Validate before loading the tokenizer or allocating model resources.
  const Status validated = ValidateServerOptions(opts);
  if (!validated.ok()) return validated;
  progress_publisher_ = StartProgressPublisher(
      progress_, metrics_.requests_success, metrics_.execution_steps_total);
  auto loading = progress_.Acquire(ProgressNowNs() + 300000000000ULL);
  ProgressContext loading_context(loading.Handle());
  ProgressExecution loading_execution(ProgressWork::kLoad);
  const char* state_directory = std::getenv("XDG_STATE_HOME");
  const char* home_directory = std::getenv("HOME");
  const auto object_options =
      ResolveObjectStoreOptions(opts, state_directory ? state_directory : "",
                                home_directory ? home_directory : "");
  FileInputPolicy file_policy;
  if (!opts.file_input_policy_file.empty()) {
    const auto status =
        LoadFileInputPolicy(opts.file_input_policy_file, &file_policy);
    if (!status) return status;
  }
  auto file_inputs =
      std::make_shared<FileExpansionOptions>(std::move(file_policy.options));
  // A sibling of the strict ObjectStore tree: never put codec scratch files
  // inside application-state records or use a client-provided pathname.
  file_inputs->extraction.legacy.work_directory =
      object_options.directory + "-input-codecs";
  file_inputs_ = std::move(file_inputs);
  file_input_policy_sha256_ = std::move(file_policy.sha256);
  objects_ = std::make_unique<ObjectStore>(object_options);
  state_key_admin_ = StateKeyAdmin::Start(*objects_, object_options.directory);
  if (opts.allow_media) {
    const auto status = vision::CheckVisionConfiguration(opts.model_dir);
    if (!status) return status;
  }
  if (!opts.client_policy_file.empty()) {
    GatewayClientPolicy policy;
    auto status = LoadGatewayClientPolicy(opts.client_policy_file, &policy);
    if (status) status = clients_.Configure(std::move(policy));
    if (!status) return status;
  }
  if (!opts.access_program_policy_file.empty()) {
    auto policy = std::make_shared<AccessProgramPolicy>();
    auto status = LoadAccessProgramPolicy(opts.access_program_policy_file,
                                          policy.get());
    if (status)
      status = BindAccessProgramPolicy(*policy, opts.served_model_name,
                                        clients_.Snapshot().get());
    if (!status) return status;
    access_program_policy_ = std::move(policy);
  }
  // Keep the actual listening descriptors across startup. A probe followed by
  // close would let another process take the port while model loading runs.
  HttpListeners pending_listeners;
  const auto listeners =
      pending_listeners.Open(opts.host, opts.port, opts.operations_port);
  if (!listeners) return listeners;
  memory_admission_.Configure(
      static_cast<uint64_t>(opts.min_available_memory_mib) * 1024 * 1024,
      static_cast<uint64_t>(opts.memory_recovery_margin_mib) * 1024 * 1024);
  const auto check_startup_memory = [&](const char* checkpoint,
                                        uint64_t estimate, bool full,
                                        StartupMemorySnapshot* snapshot) {
    *snapshot = ObserveStartupMemory(
        !opts.no_budget, estimate,
        static_cast<uint64_t>(opts.startup_memory_headroom_mib) * 1024 * 1024,
        full);
    std::fprintf(stderr,
                 "[q4t][startup-memory] {\"checkpoint\":\"%s\","
                 "\"admission\":%s}\n",
                 checkpoint, StartupMemorySnapshotJson(*snapshot).c_str());
    return snapshot->Allowed() ? Status()
                               : Status::Fail(std::string(snapshot->Reason()) +
                                              ": " + checkpoint);
  };
  Status s = check_startup_memory("before_metadata", 0, false,
                                  &startup_memory_.before_metadata);
  if (!s.ok()) return s;
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
    const char* incremental = std::getenv("Q4T_SESSION_INCREMENTAL");
    const Status cache_status =
        session_cache_.Open(opts.session_cache_dir, SessionCache::kMinPrefix,
                            SessionCache::kDiskBudget,
                            incremental && std::strcmp(incremental, "1") == 0);
    if (!cache_status.ok()) {
      std::fprintf(stderr, "[session-cache] unavailable; generation continues\n");
    }
    std::fprintf(stderr,
                 "[session-cache] enabled=1 entries=8 disk_bytes=%zu "
                 "ram_budget=%zu staging_bytes=%zu ttl_seconds=1800\n",
                 SessionCache::kDiskBudget, SessionCache::kRamBudget,
                 SessionCache::kStagingBytes);
  }
  {
    // Reuse the owned application-state filesystem for anonymous cache files
    // unless an operator selects another SSD directory. The isolated worker
    // performs all filesystem work; failure only disables cache reuse.
    const auto directory = opts.prefix_cache_dir.empty()
                               ? object_options.directory + "-prompt-cache"
                               : opts.prefix_cache_dir;
    const Status cache_status =
        prefix_cache_.Open(directory, 1024, size_t{8} << 30, false,
                            opts.prefix_cache_dir.empty());
    if (!cache_status.ok()) {
      std::fprintf(stderr, "[prefix-cache] unavailable; generation continues\n");
    }
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
        const size_t offloaded = static_cast<size_t>(cfg.E) * cfg.num_layers -
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
      if (!opts.embedding_model_dir.empty()) {
        model::EmbeddingWorkspaceLayout embedding;
        const auto planned = model::PlanEmbeddingWorkspace(
            model::EmbeddingConfig::kInputTokens, &embedding);
        if (!planned) return planned;
        // Host staging + device weights + possible file pages all use Thor
        // DRAM. Keep the optional model's peak visible before capacity choice.
        breq.extra_fixed_bytes += 3 * kNativeEmbeddingFileBytes +
            512ULL * 1024 * 1024 + size_t(opts.max_seq) * embedding.bytes;
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
          breq.mtp_weights_bytes +=
              mtp::kSubsetWeightBytes + mtp::kSubsetIdBytes;
          breq.mtp_load_host_bytes =
              mtp::kSubsetWeightBytes + mtp::kSubsetIdBytes;
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
            if (bytes >
                std::numeric_limits<size_t>::max() - breq.mtp_weights_bytes)
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
                 "min_admitted_match=%d lazy_classes=1\n",
                 cfg.max_len, mtp::SuffixDrafter::kMinAdmittedMatch);
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
  s = ResolveAdmissionLimits(opts, max_seq_, &admission_limits_);
  if (!s.ok()) return s;
  if (!clients_.FairCapacity(admission_limits_.inflight,
                             admission_limits_.queued))
    return Status::Fail(
        "fair queue requires queue >= clients and total client "
        "inflight <= global inflight");
  if (clients_.FairQueue()) seq_waiters_.ConfigureClients(clients_.Size());
  std::fprintf(stderr,
               "[q4t][admission] inflight=%d queued=%d "
               "policy=%s\n",
               admission_limits_.inflight, admission_limits_.queued,
               clients_.FairQueue() ? "client_round_robin_best_fit"
                                    : "capacity_fifo_best_fit");
  cfg.max_seq = max_seq_;
  cfg.seq_capacities = opts.seq_capacities;
  seq_capacities_ = opts.seq_capacities;
  if (!seq_capacities_.empty()) {
    std::fprintf(stderr, "[q4t][slots] capacities=");
    for (size_t i = 0; i < seq_capacities_.size(); ++i)
      std::fprintf(stderr, "%s%d", i ? "," : "", seq_capacities_[i]);
    std::fprintf(stderr, " stream_mtp_prefill=%d text_admission=%d\n",
                 capabilities.mtp, admission_limits_.inflight);
  }
  // The tokenizer and metadata are already loaded. Sample again here, before
  // either planned reservation or legacy model loading can allocate weights.
  s = check_startup_memory("before_allocations", budget_.estimated_total, true,
                           &startup_memory_.before_allocations);
  if (!s.ok()) return s;
  bool planned_startup = false;
#ifdef Q4T_STARTUP_RESERVATION
  s = ResolveCudaQuotaLimit(opts.cuda_allocation_limit_mib, opts.mem_fraction,
                            opts.cuda_allocation_limit_mib < 0
                                ? runtime::ReadSystemMemory()
                                : runtime::SystemMemory{},
                            &cuda_quota_limit_bytes_);
  if (!s.ok()) return s;
  s = runtime::ConfigureCudaAllocationQuota(cuda_quota_limit_bytes_);
  if (!s.ok()) return s;
#else
  if (opts.cuda_allocation_limit_mib != 0)
    return Status::Fail(
        "CUDA quota unavailable in experimental startup build; "
        "explicitly select --cuda-allocation-limit-mib 0");
#endif
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
      std::fprintf(stderr, "[q4t][startup-plan] mode=auto active=0 reason=%s\n",
                   supported.message().c_str());
    } else {
      PhaseTimer pt("startup_resource_plan");
      s = startup_reservation_.Prepare(cfg, opts);
      if (!s.ok()) return s;
      planned_startup = true;
      std::fprintf(stderr, "[q4t][startup-plan] mode=%s active=1\n",
                   opts.startup_plan == StartupPlanMode::kRequired ? "required"
                                                                   : "auto");
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

  // A requested MTP model must load successfully in either startup mode.
  // Untyped load errors cannot establish that plain decode remains healthy.
  // --no-mtp is the explicit opt-out; borrowed weights stay with the model.
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
    s = detail::DeviceMemoryFault("startup.mtp_load", nullptr) != cudaSuccess
            ? Status::Fail("injected MTP load failure")
            : mtp::LoadMtp(mcfg, model_.Get().head.embed_tokens,
                           model_.Get().head.lm_head, &mtp_, nullptr);
    if (!s.ok()) {
      return Status::Fail("requested MTP load failed: " + s.message());
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

  // Explicitly requested media must initialize completely before readiness.
  if (capabilities.media) {
    PhaseTimer pt("vision_load");
    io::WeightIndex* raw_index = nullptr;
    s = io::WeightIndex::Open(cfg.index_path, &raw_index);
    if (!s.ok()) return s;
    std::unique_ptr<io::WeightIndex> index(raw_index);
    io::WeightLoader* raw_loader = nullptr;
    s = io::WeightLoader::Create(opts.model_dir, *index, 8, &raw_loader);
    if (!s.ok()) return s;
    std::unique_ptr<io::WeightLoader> loader(raw_loader);
    vision::VisionConfig vcfg;
    if (vcfg.patch_size != proc_cfg_.patch_size ||
        vcfg.temporal_patch_size != proc_cfg_.temporal_patch_size ||
        vcfg.spatial_merge_size != proc_cfg_.merge_size ||
        vcfg.in_channels != 3)
      return Status::Fail("vision processor configuration mismatch");
    const auto attention = vision::VisionAttentionSharedBytes(
        &vision_device_budget_.attention_shared_bytes);
    if (attention != cudaSuccess)
      return Status::Fail("vision attention capability unavailable");
    vision_device_budget_.hidden_size = vcfg.hidden_size;
    vision_device_budget_.intermediate_size = vcfg.intermediate_size;
    vision_device_budget_.out_hidden_size = vcfg.out_hidden_size;
    auto tower = std::make_unique<vision::VisionTower>();
    std::string error;
    if (detail::DeviceMemoryFault("startup.vision_load", nullptr) !=
            cudaSuccess ||
        !vision::LoadVision(*loader, vcfg, tower.get(), &error, nullptr))
      return Status::Fail("requested vision load failed: " + error);
    vision_tower_ = std::move(tower);
    std::fprintf(stderr, "[q4t] vision tower loaded (%d blocks)\n", vcfg.depth);
  } else {
    std::fprintf(stderr, "[q4t] media disabled; vision tower not loaded\n");
  }
  std::fprintf(stderr,
               "[q4t][capabilities] effective mtp=%d media_allowed=%d "
               "vision_loaded=%d max_seq=%d\n",
               mtp_loaded_, allow_media_, vision_tower_ != nullptr, max_seq_);

  port_ = opts.port;
  operations_port_ = opts.operations_port;
  max_tokens_default_ = opts.max_tokens;
  max_prefill_ = cfg.max_prefill;
  max_len_ = cfg.max_len;
  model_name_ = opts.served_model_name;
  s = model_catalog_.Configure(model_name_, opts.model_aliases,
                                static_cast<int64_t>(std::time(nullptr)));
  if (!s) return s;
  embedding_model_dir_ = opts.embedding_model_dir;
  if (!embedding_model_dir_.empty()) {
    s = embedding_catalog_.Configure(model::EmbeddingConfig::kModelId, {},
                                      static_cast<int64_t>(std::time(nullptr)));
    if (!s) return s;
  }
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
      tc.layers = cfg.num_layers;
      tc.experts = cfg.E;
      tc.top_k = cfg.topk;
      tc.max_rows = cfg.max_prefill;
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
  const auto allocate = [&](auto*& pointer, size_t bytes, const char* site) {
    const auto result = detail::AllocateCudaMemory(pointer, bytes, site);
    RecordDeviceMemoryFailure(result, site);
    return result;
  };
  const auto logits =
      allocate(d_sched_logits_, static_cast<size_t>(max_seq_) * cfg.vocab * 2,
               "startup.scheduler_logits");
  if (!logits.ok() && !logits.exhausted())
    return Status::Fail("scheduler logits device memory failure");
  const auto tokens =
      logits.ok() ? allocate(d_sched_tokens_,
                             static_cast<size_t>(max_seq_) * sizeof(int32_t),
                             "startup.scheduler_tokens")
                  : logits;
  if (!tokens.ok()) {
    const auto released = detail::ReleaseCudaMemory(
        d_sched_logits_, "startup.scheduler_logits_rollback");
    RecordDeviceMemoryFailure(released, "startup.scheduler_logits_rollback");
    if (!tokens.exhausted() || !released.ok())
      return Status::Fail("scheduler device memory failure");
    std::fprintf(stderr,
                 "[q4t] scheduler allocation exhausted; plain decode\n");
  } else {
    h_sched_tokens_.assign(static_cast<size_t>(max_seq_), 0);
    try {
      scheduler_thread_ = std::thread([this] {
        try {
          SchedulerLoop();
        } catch (const std::exception& error) {
          std::fprintf(stderr, "[q4t] scheduler exception: %s\n", error.what());
          AbortScheduler();
        } catch (...) {
          std::fprintf(stderr, "[q4t] unknown scheduler exception\n");
          AbortScheduler();
        }
      });
    } catch (...) {
      return Status::Fail("thread failed");
    }
    scheduler_active_ = true;
    std::fprintf(stderr,
                 "[q4t] continuous-batching scheduler started "
                 "(max_seq=%d)\n",
                 max_seq_);
  }
  // Every serve prefill consumes one row per sequence. The shared buffer
  // holds at most max_seq rows, and all accesses are under model_mu_.
  const auto prefill =
      allocate(d_prefill_logits_, static_cast<size_t>(max_seq_) * cfg.vocab * 2,
               "startup.prefill_logits");
  if (!prefill.ok()) return Status::Fail(prefill.Reason());
#ifdef Q4T_STARTUP_RESOURCE_PLAN
  s = runtime::CompleteStartupReservation();
  if (!s.ok()) return s;
#elif defined(Q4T_STARTUP_RESERVATION)
  if (planned_startup) {
    s = startup_reservation_.Complete();
    if (!s.ok()) return s;
  }
#endif
  // A successful estimate is not a reservation against competing allocators.
  // Recheck residual headroom before readiness; sockets are already reserved.
  s = check_startup_memory("before_ready", 0, false,
                           &startup_memory_.before_ready);
  if (!s.ok()) return s;
  effective_config_ =
      EffectiveServeConfigJson(opts, PlanningConfig(), budget_valid_,
                               mtp_loaded_, conn_cap_, &startup_memory_);
  client_policy_admin_ = StartClientPolicyAdmin(
      clients_, admission_limits_.inflight, admission_limits_.queued,
      [this](GatewayClientPolicy candidate, uint64_t expected) {
        return PublishClientPolicy(clients_, seq_waiters_, seq_mu_,
                                   std::move(candidate), expected,
                                   admission_limits_.inflight,
                                   admission_limits_.queued);
      });
  background_options_.max_input_bytes =
      static_cast<size_t>(opts.background_max_input_mib) << 20;
  background_options_.max_jobs =
      std::clamp<size_t>(admission_limits_.inflight, 1, 256);
  background_options_.max_client_jobs = background_options_.max_jobs;
  background_options_.workers = std::min<size_t>(
      {background_options_.max_jobs, static_cast<size_t>(max_seq_), 8});
  background_options_.timeout = std::chrono::milliseconds(request_deadline_ms_);
  batch_options_.workers = opts.batch_workers;
  batch_options_.max_jobs = opts.batch_max_jobs;
  batch_options_.max_client_jobs = opts.batch_max_client_jobs;
  batch_options_.max_result_bytes =
      static_cast<size_t>(opts.batch_max_result_mib) << 20;
  batch_options_.turn_timeout = std::chrono::milliseconds(request_deadline_ms_);
  startup_listeners_ = std::move(pending_listeners);
  ProgressExecution::Completed();
  model_ready_.store(true);
  RecoverApplicationJobs();
  return Status();
}

void ChatServer::RecoverApplicationJobs() noexcept {
  if (stop_requested_.load() || !model_ready_.load() || !objects_) return;
  if (!embedding_model_dir_.empty()) {
    vector_jobs_.TryStart([this] {
      return std::make_unique<VectorJobs>(
          *objects_, clients_, VectorJobsOptions{},
          [this](auto text, const auto& control, auto* output) {
            return EncodeVectorDocument(text, control, output);
          },
          [this](const auto& tokens, const auto& client, auto& permit,
                 const auto& control, auto* result) {
            return RunVectorEmbedding(tokens, client, permit, control, result);
          });
    });
  }
  background_.TryStart([this] {
    return std::make_unique<ResponsesJobs>(
        *objects_, background_options_,
        [this](auto& job) { RunBackgroundResponse(job); },
        [this](auto& job) { return AdmitBackgroundBatch(job); });
  });
  batches_.TryStart([this] {
    return std::make_unique<BatchJobs>(
        *objects_, clients_, PlanningConfig(), batch_options_,
        [this](const auto& endpoint, const auto& row, const auto& frozen,
               const auto& client, auto& permit, const auto& control,
               auto rate) {
          return RunBatchRequest(endpoint, row, frozen, client, permit, control,
                                 std::move(rate));
        },
        [this](auto endpoint, const auto& row, const auto& frozen,
               const auto& config, const auto& access, auto* budget) {
          return MeasureBatchRequest(endpoint, row, frozen, config, access,
                                     budget);
        },
        [this](auto endpoint, const auto& row, const auto& frozen,
               const auto& config, const auto& access, auto* rate) {
          BatchInputBudget input;
          return MeasureBatchRequest(endpoint, row, frozen, config, access,
                                     &input, rate);
        },
        [this](const auto& link, const auto& access, bool cancel) {
          if (auto* background = background_.Get())
            return background->ObserveBatch(link, access, cancel);
          ObjectReply reply;
          reply.http_status = 503;
          reply.code = "batch_response_observer_unavailable";
          reply.message = "Background observer unavailable";
          return reply;
        });
  });
  if (auto* background = background_.Get()) {
    background_scanner_.Try(
        [&] { background->StartRecovery(clients_, PlanningConfig()); });
  }
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
