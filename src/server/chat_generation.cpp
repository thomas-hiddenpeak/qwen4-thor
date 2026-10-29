// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <span>
#include <string>
#include <vector>
#include <cuda_runtime.h>
#include "q4t/diagnostic/execution_observer.h"
#include "q4t/mtp/suffix_drafter.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/prefix_cache_policy.h"
#include "q4t/server/request_json.h"
#include "q4t/server/mtp_policy.h"
#include "q4t/server/scheduler_submission.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/server/session_state.h"

namespace q4t::server {
namespace {

// Diagnostic-only memory snapshot (device + host + page cache) for the
// acceptance memory ledger. Never touches the compute path.
void LogMemorySnapshot(const char* phase, const std::string& id) {
  size_t free_bytes = 0, total_bytes = 0;
  if (cudaMemGetInfo(&free_bytes, &total_bytes) != cudaSuccess) return;
  long rss_kb = 0;
  if (FILE* st = std::fopen("/proc/self/status", "r")) {
    char line[256];
    while (std::fgets(line, sizeof line, st)) {
      if (std::strncmp(line, "VmRSS:", 6) == 0) {
        rss_kb = std::atol(line + 6);
        break;
      }
    }
    std::fclose(st);
  }
  long mem_free_kb = 0, cached_kb = 0, mem_avail_kb = 0;
  if (FILE* mi = std::fopen("/proc/meminfo", "r")) {
    char line[256];
    while (std::fgets(line, sizeof line, mi)) {
      if (std::strncmp(line, "MemFree:", 8) == 0) mem_free_kb = std::atol(line + 8);
      if (std::strncmp(line, "Cached:", 7) == 0) cached_kb = std::atol(line + 7);
      if (std::strncmp(line, "MemAvailable:", 13) == 0)
        mem_avail_kb = std::atol(line + 13);
      if (mem_free_kb != 0 && cached_kb != 0 && mem_avail_kb != 0) break;
    }
    std::fclose(mi);
  }
  std::fprintf(stderr,
               "[q4t][mem] phase=%s id=%s dev_used_gb=%.3f dev_total_gb=%.3f "
               "rss_mb=%.1f mem_free_gb=%.3f mem_avail_gb=%.3f cached_gb=%.3f\n",
               phase, id.c_str(), (total_bytes - free_bytes) / 1e9,
               total_bytes / 1e9, rss_kb / 1024.0,
               mem_free_kb * 1024.0 / 1e9, mem_avail_kb * 1024.0 / 1e9,
               cached_kb * 1024.0 / 1e9);
}

}  // namespace
using detail::RequestCancelled;
using detail::ValidRequestKey;
using detail::FinishHostReadback;
using detail::JsonEscape;
using detail::WriteAll;
using detail::SendSimple;
using detail::SendError;
using detail::SseChunk;
using detail::PrepareChatInput;

void ChatServer::HandleChat(int fd, const std::string& body) {
  RequestDiagnostics diagnostic;
  using Wait = RequestDiagnostics::Wait;
  metrics_.requests_total.fetch_add(1, std::memory_order_relaxed);
  const auto t_arrive = std::chrono::steady_clock::now();
  // Any return before generation starts (validation / alloc / encode / prefill
  // failure) counts as a pre-generation error; cleared once a token is produced.
  struct ErrGuard {
    std::atomic<uint64_t>& err;
    bool ok = false;
    ~ErrGuard() {
      if (!ok) err.fetch_add(1, std::memory_order_relaxed);
    }
  } err_guard{metrics_.requests_error};
  if (!gpu_healthy_.load(std::memory_order_relaxed)) {
    SendError(fd, 503, "GPU unhealthy after a device error; restart the server");
    return;
  }
  const int chat_limit = TextAdmissionLimit(max_seq_);
  if (active_chats_.fetch_add(1) >= chat_limit) {
    active_chats_.fetch_sub(1);
    SendError(fd, 503, "text request capacity exhausted");
    return;
  }
  struct ChatGuard {
    std::atomic<int>& count;
    ~ChatGuard() { count.fetch_sub(1); }
  } chat_guard{active_chats_};
  io::Json req;
  Status s = io::ParseJson(body, &req, true, kRequestJsonLimits);
  if (!s.ok()) {
    SendError(fd, 400, "invalid JSON: " + s.message());
    return;
  }

  s = ValidateChatContract(req, model_name_, allow_media_);
  if (!s.ok()) {
    SendError(fd, 400, s.message());
    return;
  }

  const std::string prefix_scope = req.GetString("prefix_cache_scope");
  const int prefix_write_tokens =
      static_cast<int>(req.GetInt("prefix_cache_write_tokens", 0));
  if (!prefix_scope.empty() && !prefix_cache_.Enabled()) {
    SendError(fd, 400, "prefix cache is not enabled");
    return;
  }
  std::vector<std::unique_ptr<SessionLease>> shared_writes;
  SessionLease session;
  session.key = req.GetString("session_id");
  if (!session.key.empty()) {
    if (!session_cache_.Enabled()) {
      SendError(fd, 400, "session cache is not enabled");
      return;
    }
    if (!session_cache_.Acquire(session.key)) {
      SendError(fd, 409, "session is busy or lease capacity exhausted");
      return;
    }
    session.cache = &session_cache_;
  }

  const io::Json* external_id = req.Find("request_id");
  const io::Json* cancel_key = req.Find("cancel_token");
  const std::string key = req.GetString("cancel_token");
  std::string request_id = req.GetString("request_id");
  if ((external_id && (!external_id->IsString() || request_id.empty() ||
                       request_id.size() > 128 ||
                       request_id.starts_with("chatcmpl-auto-") ||
                       !std::all_of(request_id.begin(), request_id.end(),
                                    [](unsigned char c) {
                                      return std::isalnum(c) || c == '-' ||
                                             c == '_';
                                    }))) ||
      (external_id && (!cancel_key || !cancel_key->IsString() ||
                       !ValidRequestKey(key))) ||
      (!external_id && cancel_key)) {
    SendError(fd, 400, "request_id requires a fresh 64-hex cancel_token");
    return;
  }
  if (!external_id) {
    request_id = "chatcmpl-auto-" +
                 std::to_string(next_request_id_.fetch_add(1));
  }
  auto deadline = t_arrive +
                  std::chrono::milliseconds(request_deadline_ms_);
  if (const io::Json* timeout = req.Find("request_timeout_ms")) {
    const double value = timeout->AsDouble(-1);
    if (!timeout->IsNumber() || !(value >= 1 && value <= 10800000) ||
        value != static_cast<double>(static_cast<int64_t>(value))) {
      SendError(fd, 400,
                "request_timeout_ms must be an integer in [1,10800000]");
      return;
    }
    deadline = t_arrive +
               std::chrono::milliseconds(static_cast<int64_t>(value));
  }
  auto control = requests_.Register(request_id, key, deadline);
  if (!control) {
    SendError(fd, 409, "request_id already active");
    return;
  }
  diagnostic.Bind(request_id);
  struct Registration {
    RequestRegistry& registry;
    std::shared_ptr<RequestControl> control;
    RequestDiagnostics& diagnostic;
    ~Registration() {
      if (control->Cancelled())
        diagnostic.Outcome("cancelled", control->Reason());
      registry.Remove(control);
    }
  } registration{requests_, control, diagnostic};
  const auto cancelled = [&] { return RequestCancelled(control.get(), fd); };
  bool abort_counted = false;
  const auto count_abort = [&] {
    if (!abort_counted) {
      metrics_.requests_aborted.fetch_add(1, std::memory_order_relaxed);
      abort_counted = true;
      std::fprintf(stderr, "[q4t] request cancelled id=%s reason=%s\n",
                   request_id.c_str(), control->Reason());
    }
    err_guard.ok = true;
  };

  // ValidateChatContract checked type and range before registration.
  int max_tokens = static_cast<int>(
      req.GetInt("max_tokens", max_tokens_default_));
  // Upper bound: decode stops at max_len_ anyway (KV cache), so a huge
  // max_tokens only pins a seq slot and grows host buffers; cap it so one
  // request cannot starve the pool.
  if (max_tokens > max_len_) max_tokens = max_len_;

  // stream flag.
  const bool stream = req.GetBool("stream", false);
  const io::Json* stream_options = req.Find("stream_options");
  const bool include_usage =
      stream_options && stream_options->GetBool("include_usage", false);

  std::string prompt;
  std::vector<VisionItem> items;
  if (!PrepareChatInput(fd, req, &prompt, &items)) return;

  std::vector<std::uint32_t> prompt_u32;
  std::vector<int32_t> ids;
  const bool explicit_slots = !seq_capacities_.empty();
  size_t required_capacity = 0;
  if (explicit_slots) {
    diagnostic.Stage("encode");
    {
      auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer);
      s = tok_->Encode(prompt, &prompt_u32);
    }
    if (!s.ok()) {
      SendError(fd, 400, "encode failed: " + s.message());
      return;
    }
    required_capacity = prompt_u32.size() + static_cast<size_t>(max_tokens);
    if (prompt_u32.empty() ||
        required_capacity > static_cast<size_t>(max_len_)) {
      SendError(fd, 400, "input plus output budget exceeds slot capacity");
      return;
    }
    ids.assign(prompt_u32.begin(), prompt_u32.end());
    s = ValidatePrefixSeedBoundary(prefix_write_tokens,
                                     prefix_cache_checkpoints_, ids.size());
    if (!s.ok()) {
      SendError(fd, 400, s.message());
      return;
    }
  }

  // B1 multi-request scheduling: each request runs on its own thread. All
  // GPU work uses the DEFAULT stream (the model's per-forward scratch is a
  // single shared allocation, and per-sequence state is pooled + isolated by
  // seq_id), so model forwards are serialized behind model_mu_ for both
  // scratch safety and GPU-stream ordering. GPU readback completes under
  // the lock; tokenization and SSE output use the completed host result.
  diagnostic.Stage("slot_admission");
  int seq_id;
  {
    RequestDiagnostics::Scope wait(diagnostic, Wait::kSlot);
    seq_id = AllocSeqId(control.get(), fd, required_capacity);
  }
  if (seq_id == -3) {
    SendError(fd, 503, "GPU unhealthy; restart server");
    return;
  }
  if (seq_id == -2) {
    count_abort();
    SendError(fd, 409, "request cancelled before admission");
    return;
  }
  if (seq_id < 0) {
    SendError(fd, 503, "server shutting down");
    return;
  }
  diagnostic.Admitted(seq_id);
  const int request_capacity = model_.Get().cfg.Capacity(seq_id);
  if (explicit_slots)
    std::fprintf(stderr,
                 "[q4t][slot-admission] request=%s slot=%d capacity=%d "
                 "required=%zu\n",
                 request_id.c_str(), seq_id, request_capacity,
                 required_capacity);
  diagnostic.Stage("encode");
  metrics_.queue_seconds.Observe(
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t_arrive)
          .count());
  // Per-request device buffers (freed by `cleanup` on any exit path). Prefill
  // logits use the shared d_prefill_logits_ (under model_mu_), not a per-
  // request buffer.
  uint16_t* d_trunk_full = nullptr;
  uint16_t* d_vfeats = nullptr;
  uint16_t* d_session_trunk = nullptr;
  uint16_t* d_mtp_g = nullptr;  // MTP rolling draft trunk [hc*hs] (Stage 2c:
                                // per-request, so concurrent MTP requests don't
                                // share the legacy d_g_/d_g_next_ double buffer)
  trace::RequestOutcome trace_outcome = trace::RequestOutcome::kFailed;
  uint64_t trace_output_tokens = 0;
  auto cleanup = [&]() {
    if (d_session_trunk) {
      cudaFree(d_session_trunk);
      d_session_trunk = nullptr;
    }
    if (router_trace_) router_trace_->EndRequest(
        control->Cancelled() ? trace::RequestOutcome::kCancelled : trace_outcome,
        trace_output_tokens);
    if (d_trunk_full) {
      cudaFree(d_trunk_full);
      d_trunk_full = nullptr;
    }
    if (d_vfeats) {
      cudaFree(d_vfeats);
      d_vfeats = nullptr;
    }
    if (d_mtp_g) {
      cudaFree(d_mtp_g);
      d_mtp_g = nullptr;
    }
    FreeSeqId(seq_id);
    diagnostic.Released();
  };

  if (cancelled()) {
    count_abort();
    cleanup();
    SendError(fd, 409, "request cancelled before encoding");
    return;
  }

  // 1. Encode prompt (CPU, outside model_mu_). The tokenizer's ICU regex
  // engine is not thread-safe, so Encode is serialized behind tok_mu_.
  if (!explicit_slots) {
    {
      auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer);
      s = tok_->Encode(prompt, &prompt_u32);
    }
    if (!s.ok()) {
      SendError(fd, 400, "encode failed: " + s.message());
      cleanup();
      return;
    }
    ids.assign(prompt_u32.begin(), prompt_u32.end());
  }

  // 1b. Multimodal: each <|image_pad|> (image_token_id) / <|video_pad|>
  // (video_token_id) placeholder expands to the number of merged visual tokens
  // for its item, and the vision tower's features are injected in place of
  // those token embeddings (in prompt position order).
  const int img_id = model_.Get().cfg.image_token_id;
  const int vid_id = model_.Get().cfg.video_token_id;
  model::VisionFeatures vfeats;
  if (!items.empty()) {
    std::vector<int> counts;
    std::vector<std::array<int, 3>> grids;
    std::string verr;
    // The vision tower uses a shared workspace, so its forward is serialized
    // behind model_mu_ (the same lock that serializes the main model). The
    // pipeline runs on the default stream and self-synchronizes internally.
    {
      auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
      if (!gpu_healthy_.load(std::memory_order_relaxed)) {
        SendError(fd, 503, "GPU unhealthy; restart server");
        cleanup();
        return;
      }
      if (!RunVisionPipeline(items, &d_vfeats, &vfeats.num_tokens, &counts,
                             &grids, &verr)) {
        SendError(fd, 400, verr);
        cleanup();
        return;
      }
    }
    vfeats.grids = std::move(grids);
    // Split the per-item counts (in item order) into image / video counts,
    // matching the placeholder order in the encoded prompt.
    std::vector<int> img_counts, vid_counts;
    for (size_t i = 0; i < items.size(); ++i) {
      if (items[i].kind == VisionItem::kImage) img_counts.push_back(counts[i]);
      else vid_counts.push_back(counts[i]);
    }
    std::vector<int32_t> expanded;
    if (!model::ExpandMultimodalTokens(ids.data(), static_cast<int>(ids.size()),
                                       img_id, vid_id, img_counts, vid_counts,
                                       &expanded)) {
      SendError(fd, 400, "multimodal count mismatch in prompt");
      cleanup();
      return;
    }
    ids = std::move(expanded);
    vfeats.device = d_vfeats;
  }
  const int T = static_cast<int>(ids.size());
  if (T >= request_capacity) {
    SendError(fd, 400,
              "prompt too long for context: " + std::to_string(T) +
                  " tokens >= " + std::to_string(request_capacity) +
                  " max_len");
    cleanup();
    return;
  }

  const int vocab = model_.Get().cfg.vocab;
  const auto is_stop_token = [this](int32_t token) {
    return std::find(stop_token_ids_.begin(), stop_token_ids_.end(), token) !=
           stop_token_ids_.end();
  };
  // Chunked prefill (262K context, see PHASES.md 262K memory budget): the
  // forward workspace (d_ws, d_trunk, ...) is sized for max_prefill tokens,
  // so a prompt longer than max_prefill is run in chunks of max_prefill.
  // max_prefill is now the CHUNK size, not a prompt cap (the prompt cap is
  // max_len). Text-only: a vision prompt's special (t,h,w) rope is not
  // reproducible by the continuation chunks' text rope, so vision + chunked
  // is rejected (vision prompts are far shorter than 262K in practice).
  const int chunk = max_prefill_;
  const bool chunked = T > chunk;
  if (chunked && vfeats.num_tokens > 0) {
    SendError(fd, 400,
              "chunked prefill (prompt > max_prefill) does not support "
              "vision input");
    cleanup();
    return;
  }
  if (router_trace_) router_trace_->BeginRequest(ids, request_id);
  // Chunk 0 is a true prefill (state reset + rope table + PLE history);
  // chunks 1.. are ModelDecodeBatch calls that CONTINUE from the existing
  // per-layer state (linear SSM/conv recurrence, full-attention KV/indexer
  // written at absolute positions) — bit-identical to one big prefill. Only
  // the LAST chunk's final logits row is needed (first decode token), so
  // all serve paths explicitly select that row before the output head.
  // Shared d_prefill_logits_ holds [max_seq, vocab], including inline
  // fallback, vision and MTP main-model prefill (one row per request).
  // MTP: prefill trunk_out buffer (pre-final-mixer multi stream [T, hc*hs])
  // for the draft-extend. Allocated when MTP is loaded, for BOTH one-shot and
  // chunked prefill. The chunked path accumulates the trunk across chunks
  // (each chunk's trunk_out writes its [base, base+c) rows); the draft-extend
  // then runs over the full prompt in chunks (MtpDraftExtend's chunked path).
  // The draft KV/indexer is allocated at MTP load time (pooled over max_seq,
  // max_len), so it is NOT a per-request cost -- the only per-request extra is
  // this trunk buffer (T * hc_dim * 2, ~0.9 GB at 44K, ~5.4 GB at 262K). If
  // the allocation fails (not enough headroom), fall back to plain decode.
  const size_t trunk_hc_dim =
      mtp_loaded_
          ? static_cast<size_t>(mtp_.cfg.hc) * static_cast<size_t>(mtp_.cfg.hs)
          : 0;
  const bool stream_mtp = mtp_loaded_ && explicit_slots;
  int32_t stream_d0 = -1;
  bool stream_draft_ready = false;
  if (mtp_loaded_) {
    // OOM preflight (runtime). On unified memory a large cudaMalloc does not
    // merely fail — it can push MemFree to zero and trip the global OOM
    // killer (the 2026-09-19 reboot). So before attempting the trunk
    // allocation, gauge MemFree (which excludes reclaimable page cache) and
    // fall back to plain decode if there is not enough headroom. The trunk
    // buffer (T * hc_dim * 2) plus the MTP draft logits (max_prefill * vocab *
    // 2, allocated inside MtpDraftExtend) plus a 2 GB safety margin must fit.
    const size_t trunk_bytes =
        static_cast<size_t>(stream_mtp ? std::min(T, chunk) : T) *
        trunk_hc_dim * 2;
    const size_t draft_logits_bytes =
        static_cast<size_t>(max_prefill_) *
        static_cast<size_t>(model_.Get().cfg.vocab) * 2;
    const size_t kPreflightMargin = 2u * 1024u * 1024u * 1024u;  // 2 GB
    const size_t need = trunk_bytes + draft_logits_bytes + kPreflightMargin;
    // MemAvailable (NOT MemFree): it includes reclaimable page cache, which is
    // exactly what the kernel evicts to satisfy a fresh cudaMalloc. In steady
    // state MemFree is only a few GB (weights + model-file cache fill it) but
    // MemAvailable is ~120 GB, so MemFree would wrongly fall back on every
    // request. MemAvailable < need means even cache eviction cannot satisfy
    // the allocation -> the OOM killer would trip, so degrade to plain decode.
    const size_t mem_avail = runtime::ReadMemAvailable();
    if (mem_avail > 0 && mem_avail < need) {
      std::fprintf(
          stderr,
          "[q4t] OOM preflight: MemAvailable=%.2f GB < need=%.2f GB (trunk "
          "T=%d); plain decode\n",
          mem_avail / 1e9, need / 1e9, T);
      d_trunk_full = nullptr;
    } else if (cudaMalloc(reinterpret_cast<void**>(&d_trunk_full),
                          trunk_bytes) != cudaSuccess) {
      std::fprintf(stderr,
                   "[q4t] trunk alloc failed for T=%d; plain decode\n", T);
      d_trunk_full = nullptr;
    }
  }
  if (stream_mtp &&
      (!d_trunk_full || cudaMalloc(reinterpret_cast<void**>(&d_mtp_g),
                                   trunk_hc_dim * 2) != cudaSuccess)) {
    cleanup();
    SendError(fd, 503, "streamed MTP prefill reservation failed");
    return;
  }
  std::vector<uint16_t> h_logits(static_cast<size_t>(vocab));

  auto argmax = [&](const uint16_t* h) {
    int best = 0;
    float best_v = -1e30f;
    for (int v = 0; v < vocab; ++v) {
      const uint32_t bits = static_cast<uint32_t>(h[v]) << 16;
      float f;
      std::memcpy(&f, &bits, sizeof(f));
      if (f > best_v) {
        best_v = f;
        best = v;
      }
    }
    return best;
  };

  // Capture only the selected request's execution, after tokenization and
  // allocation. The observer is disabled unless explicitly requested.
  q4t::diagnostic::ProfileCapture execution_capture(T);
  diagnostic.Stage("prefill");
  // 2. Prefill (PD-ready 阶段边界 API: Begin -> Prefill). The forward uses the
  // shared per-forward scratch, so it is serialized behind model_mu_; the
  // per-sequence recurrent state is isolated by seq_id. All GPU work uses the
  // default stream, so the forward is ordered with the D2H below.
  model::ModelSequence seq;
  const model::VisionFeatures* vptr =
      (vfeats.num_tokens > 0) ? &vfeats : nullptr;
  // Batched prefill (plain path): non-vision, non-chunked, non-MTP requests
  // pack their prefill with other concurrent requests into ONE
  // ModelPrefillBatch (the dense weights read once for the whole batch instead
  // of per request). MTP needs the prefill trunk_out (draft-extend) and
  // vision/chunked need the per-request path, so those still prefill inline.
  const bool batched_prefill =
      scheduler_active_ && !mtp_loaded_ && !chunked && vptr == nullptr &&
      getenv("Q4T_NO_BATCH_PREFILL") == nullptr;
  const bool scheduled_chunk_prefill =
      scheduler_active_ && !mtp_loaded_ && chunked && vptr == nullptr;
  bool prefill_cancelled = false;
  if ((session.cache || !prefix_scope.empty()) &&
      cudaMalloc(&d_session_trunk,
                          static_cast<size_t>(trunk_hc_dim) * 2) != cudaSuccess) {
    cleanup();
    SendError(fd, 500, "session trunk allocation failed");
    return;
  }
  int restored_prefix = 0;
  bool session_gpu_error = false;
  const auto session_copy = [&](void* host, void* state, size_t bytes,
                                 bool restore) -> Status {
    const auto error = restore
        ? cudaMemcpy(state, host, bytes, cudaMemcpyHostToDevice)
        : cudaMemcpy(host, state, bytes, cudaMemcpyDeviceToHost);
    if (error == cudaSuccess) return Status();
    session_gpu_error = true;
    gpu_healthy_.store(false, std::memory_order_relaxed);
    return Status::Fail(std::string("session CUDA copy: ") +
                        cudaGetErrorString(error));
  };
  if (stream_mtp) {
    q4t::diagnostic::Batch execution_batch(request_id.c_str(), seq_id, 0, T);
    q4t::diagnostic::Scope execution_prefill("handler_stream_mtp_prefill", 0,
                                             T);
    auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
    s = !gpu_healthy_.load(std::memory_order_relaxed)
            ? Status::Fail("GPU unhealthy; restart server")
            : model::ModelBeginSequence(model_.Get(), &seq, nullptr, seq_id);
    if (s.ok()) s = mtp::MtpResetState(mtp_, nullptr, seq_id);
    if (s.ok() && (session.cache || !prefix_scope.empty())) {
      const auto begin = std::chrono::steady_clock::now();
      int prefix = session.cache ? session_cache_.Match(session.key, ids) : 0;
      SessionCache* selected_cache = &session_cache_;
      std::string selected_key = session.key;
      SessionLease shared_read;
      if (!prefix_scope.empty()) {
        const int shared = prefix_cache_.MatchShared(prefix_scope, ids,
                                                       &shared_read.key);
        if (shared > 0) shared_read.cache = &prefix_cache_;
        if (shared > prefix) {
          prefix = shared;
          selected_cache = &prefix_cache_;
          selected_key = shared_read.key;
        }
      }
      Status restored;
      if (prefix > 0) {
        std::vector<SessionRange> ranges;
        restored = SessionStateRanges(model_.Get(), mtp_, seq_id, prefix,
                                        d_mtp_g, d_session_trunk, &ranges);
        int32_t cached_pending = -1;
        if (restored.ok())
          restored = selected_cache->Restore(selected_key, ranges, session_copy,
                                               cancelled, &stream_d0,
                                               &cached_pending);
        if (restored.ok() && cached_pending != ids[prefix]) {
          // The draft is shifted by one token. A tool/message boundary may
          // replace its final predicted token; target state remains valid.
          const int position = prefix - 1;
          restored = mtp::MtpDraftExtend(mtp_, &ids[prefix], d_session_trunk,
                                          &position, 1, nullptr, nullptr,
                                          nullptr, seq_id, false);
          restored = FinishHostReadback(cudaSuccess, &gpu_healthy_, {},
                                          restored, nullptr);
          if (!restored.ok()) session_gpu_error = true;
        }
        if (restored.ok()) {
          seq.history.assign(ids.begin(), ids.begin() + prefix);
          seq.position = prefix;
          model_.Get().rope_delta[seq_id] = 0;
          restored_prefix = prefix;
        } else if (session_gpu_error) {
          s = restored;
        } else {
          // An incomplete read may have written part of the destination.
          // Reset both models before a miss is allowed to fall back.
          s = model::ModelBeginSequence(model_.Get(), &seq, nullptr, seq_id);
          if (s.ok()) s = mtp::MtpResetState(mtp_, nullptr, seq_id);
        }
      }
      std::fprintf(stderr,
                   "[session-cache] request=%s event=restore prefix=%d "
                   "ms=%.3f error=%s source=%s\n", request_id.c_str(), restored_prefix,
                   std::chrono::duration<double, std::milli>(
                       std::chrono::steady_clock::now() - begin).count(),
                   restored.ok() ? "none" : restored.message().c_str(),
                   selected_cache == &prefix_cache_ ? "shared" : "session");
    }
    std::vector<int32_t> shifted;
    std::vector<int> positions;
    int chunks = 0;
    while (s.ok() && seq.position < T) {
      if (cancelled()) {
        prefill_cancelled = true;
        break;
      }
      const int base = seq.position;
      int count = std::min(chunk, T - base);
      if (prefix_write_tokens > base)
        count = std::min(count, prefix_write_tokens - base);
      const int boundary = base + count;
      const bool last = boundary == T;
      SessionLease* shared_write = nullptr;
      if (!last && prefix_write_tokens > 0 &&
          boundary == prefix_write_tokens) {
        const auto digest = trace::Sha256({
            reinterpret_cast<const uint8_t*>(ids.data()),
            static_cast<size_t>(boundary) * sizeof(int32_t)});
        const std::string snapshot_key = prefix_scope + ':' +
                                        trace::HexDigest(digest);
        if (prefix_cache_.AcquireNew(snapshot_key)) {
          auto lease = std::make_unique<SessionLease>();
          lease->cache = &prefix_cache_;
          lease->key = snapshot_key;
          shared_write = lease.get();
          shared_writes.push_back(std::move(lease));
        }
      }
      if (router_trace_)
        router_trace_->BeginForward(trace::RouteStage::kPrefill, base, count);
      s = model::ModelPrefillTextChunk(
          model_.Get(), &seq, ids.data(), T, count,
          last ? d_prefill_logits_ : nullptr, nullptr, d_trunk_full,
          model::LogitsRows::kLastRow, model::SequenceCompletion::kDeferred);
      cudaError_t copy_error = cudaSuccess;
      if (s.ok() && last)
        copy_error = cudaMemcpyAsync(h_logits.data(), d_prefill_logits_,
                                     static_cast<size_t>(vocab) * 2,
                                     cudaMemcpyDeviceToHost, nullptr);
      model::ModelSequence* sequence = &seq;
      s = FinishHostReadback(copy_error, &gpu_healthy_, {&sequence, 1}, s,
                             router_trace_.get());
      if (!s.ok()) break;
      shifted.resize(count);
      positions.resize(count);
      for (int i = 0; i < count; ++i) {
        positions[i] = base + i;
        shifted[i] =
            base + i + 1 < T ? ids[base + i + 1] : argmax(h_logits.data());
      }
      s = mtp::MtpDraftExtend(mtp_, shifted.data(), d_trunk_full,
                              positions.data(), count, &stream_d0, d_mtp_g,
                              nullptr, seq_id, last, shared_write != nullptr);
      // The target chunk was committed above. Drain a failed draft submission
      // before releasing this request's storage or giving scratch to a peer.
      s = FinishHostReadback(cudaSuccess, &gpu_healthy_, {}, s, nullptr);
      if (s.ok() && (last || shared_write) && d_session_trunk) {
        const auto error = cudaMemcpy(
            d_session_trunk,
            d_trunk_full + static_cast<size_t>(count - 1) * trunk_hc_dim,
            static_cast<size_t>(trunk_hc_dim) * 2, cudaMemcpyDeviceToDevice);
        if (error != cudaSuccess) {
          gpu_healthy_.store(false, std::memory_order_relaxed);
          s = Status::Fail("session prefill final trunk copy failed");
        }
      }
      if (s.ok() && shared_write) {
        const auto save_begin = std::chrono::steady_clock::now();
        SessionSaveTiming timing(request_id.c_str(), "shared");
        std::vector<SessionRange> ranges;
        Status saved;
        {
          SessionSaveTiming::Scope span(&timing, SessionSaveTiming::kRanges);
          saved = SessionStateRanges(model_.Get(), mtp_, seq_id, boundary,
                                      d_mtp_g, d_session_trunk, &ranges);
        }
        if (saved.ok())
          saved = prefix_cache_.Save(shared_write->key,
              {ids.data(), static_cast<size_t>(boundary + 1)}, ranges,
              session_copy, cancelled, -1, &timing);
        timing.Report(saved.ok());
        if (session_gpu_error) s = saved;
        std::fprintf(stderr,
                     "[prefix-cache] request=%s event=save prefix=%d "
                     "bytes=%zu ms=%.3f error=%s\n", request_id.c_str(),
                     boundary, prefix_cache_.Bytes(),
                     std::chrono::duration<double, std::milli>(
                         std::chrono::steady_clock::now() - save_begin).count(),
                     saved.ok() ? "none" : saved.message().c_str());
      }
      ++chunks;
    }
    if (s.ok() && !prefill_cancelled) {
      s = model::ModelReserveVerifyCheckpoints(model_.Get(), mtp_k_);
      if (s.ok()) s = mtp::MtpReserveScratch(mtp_, mtp_k_ + 1);
      stream_draft_ready = s.ok();
    }
    if (!s.ok()) {
      s = FinishHostReadback(cudaSuccess, &gpu_healthy_, {}, s, nullptr);
      seq.Fail();
    }
    std::fprintf(stderr,
                 "[mtp-prefill] request=%s streamed=1 chunks=%d "
                 "trunk_bytes=%zu ready=%d\n",
                 request_id.c_str(), chunks,
                 static_cast<size_t>(std::min(T, chunk)) * trunk_hc_dim * 2,
                 stream_draft_ready);
  } else if (scheduled_chunk_prefill) {
    ChunkPrefillReq pr;
    pr.fd = fd;
    pr.control = control.get();
    pr.seq = &seq;
    pr.seq_id = seq_id;
    pr.ids = ids.data();
    pr.len = T;
    pr.h_logits = h_logits.data();
    RequestDiagnostics::Scope wait(diagnostic, Wait::kScheduler);
    SubmitSchedulerRequestAndWait(
        sched_mu_, scheduler_stop_, sched_cv_, pr.cv,
        [&] { chunk_prefill_pending_.push_back(&pr); },
        [&] { return pr.done; });
    prefill_cancelled = pr.cancelled;
    s = pr.ok ? Status()
              : Status::Fail("scheduled chunk prefill failed" +
                             (pr.err.empty()
                                  ? std::string()
                                  : std::string(": ") + pr.err));
  } else if (batched_prefill) {
    PrefillReq pr;
    pr.control = control.get();
    pr.fd = fd;
    pr.seq = &seq;
    pr.seq_id = seq_id;
    pr.ids = ids.data();
    pr.len = T;
    pr.h_logits = h_logits.data();
    RequestDiagnostics::Scope wait(diagnostic, Wait::kScheduler);
    SubmitSchedulerRequestAndWait(
        sched_mu_, scheduler_stop_, sched_cv_, pr.cv,
        [&] {
          pr.pending = true;
          prefill_pending_.push_back(&pr);
        },
        [&] { return pr.done; });
    s = pr.ok ? Status() : Status::Fail("batched prefill failed");
  } else {
    // The request ID outlives this batch. Flush only after the existing final
    // readback boundary; do not log between asynchronous chunk submissions.
    q4t::diagnostic::Batch execution_batch(request_id.c_str(), seq_id, 0, T);
    q4t::diagnostic::Scope execution_prefill("handler_prefill", 0, T);
    auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
    {
      q4t::diagnostic::Scope execution_begin("target_begin_sequence", 0, T);
      s = !gpu_healthy_.load(std::memory_order_relaxed)
              ? Status::Fail("GPU unhealthy; restart server")
              : cancelled()
                    ? Status::Fail("request cancelled")
                    : model::ModelBeginSequence(model_.Get(), &seq, nullptr,
                                                seq_id);
    }
    int prefill_base = 0;
    int prefill_tokens = std::min(chunk, T);
    if (s.ok()) {
      if (!chunked) {
        if (router_trace_) router_trace_->BeginForward(
            trace::RouteStage::kPrefill, 0, T);
        // Keep full trunk output for MTP, but only the final logits row.
        // Deferred completion does not make this a pure CPU submission span:
        // model internals may still wait for metadata, PLE or MoE readbacks.
        q4t::diagnostic::Scope execution_submit("target_prefill_call", 0, T);
        s = model::ModelPrefill(model_.Get(), &seq, ids.data(), T, d_prefill_logits_,
                                nullptr, mtp_loaded_ ? d_trunk_full : nullptr,
                                vptr, seq_id, model::LogitsRows::kLastRow,
                                model::SequenceCompletion::kDeferred);
      } else {
        // Text chunks share the sequence's slot, position and PLE history.
        // Intermediate chunks only produce trunk/state; the final chunk
        // writes one logits row at the start of the shared buffer.
        while (s.ok() && seq.position < T) {
          if (cancelled()) {
            prefill_cancelled = true;
            s = Status::Fail("client disconnected during prefill");
            break;
          }
          const int base = seq.position;
          const int c = std::min(chunk, T - base);
          const bool last = (base + c == T);
          prefill_base = base;
          prefill_tokens = c;
          q4t::diagnostic::Scope execution_chunk("target_prefill_chunk", base,
                                                c);
          if (router_trace_) router_trace_->BeginForward(
              trace::RouteStage::kPrefill, base, c);
          {
            q4t::diagnostic::Scope execution_submit("target_prefill_call", base,
                                                   c);
            s = model::ModelPrefillTextChunk(
                model_.Get(), &seq, ids.data(), T, c,
                last ? d_prefill_logits_ : nullptr, nullptr,
                d_trunk_full
                    ? d_trunk_full + static_cast<size_t>(base) * trunk_hc_dim
                    : nullptr, model::LogitsRows::kLastRow,
                model::SequenceCompletion::kDeferred);
          }
          if (!last) {
            model::ModelSequence* sequence = &seq;
            {
              // This includes the existing stream wait and sequence commit.
              q4t::diagnostic::Scope execution_wait(
                  "prefill_chunk_readback_wait_commit", base, c);
              s = FinishHostReadback(cudaSuccess, &gpu_healthy_, {&sequence, 1},
                                     s, router_trace_.get());
            }
            // Diagnostic: memory trajectory across chunked prefill (the
            // 204800-tier OOM investigation). Never touches compute.
            {
              q4t::diagnostic::Scope execution_memory(
                  "prefill_chunk_memory_snapshot", base, c);
              LogMemorySnapshot("prefill_chunk",
                                "seq" + std::to_string(seq_id) +
                                    "@pos" + std::to_string(seq.position));
            }
          }
          if (last || !s.ok()) break;
        }
      }
    }
    cudaError_t copy_error = cudaSuccess;
    if (s.ok()) {
      q4t::diagnostic::Scope execution_copy(
          "prefill_final_d2h_submit", prefill_base, prefill_tokens);
      copy_error = cudaMemcpyAsync(
          h_logits.data(), d_prefill_logits_, static_cast<size_t>(vocab) * 2,
          cudaMemcpyDeviceToHost, nullptr);
    }
    model::ModelSequence* sequence = &seq;
    // Drain and commit while holding model_mu_, also on failed submission.
    const auto pending = seq.HasPending()
        ? std::span<model::ModelSequence* const>(&sequence, 1)
        : std::span<model::ModelSequence* const>();
    {
      q4t::diagnostic::Scope execution_wait(
          "prefill_final_readback_wait_commit", prefill_base, prefill_tokens);
      s = FinishHostReadback(copy_error, &gpu_healthy_, pending, s,
                             router_trace_.get());
    }
    if (!s.ok()) seq.Fail();
  }
  if ((prefill_cancelled || cancelled()) &&
      gpu_healthy_.load(std::memory_order_relaxed)) {
    // Scheduler chunks have drained before notification; the inline path
    // drains above. The request thread still owns fd and all request buffers.
    std::fprintf(stderr, "[q4t] prefill cancelled seq=%d position=%d total=%d\n",
                 seq_id, seq.position, T);
    count_abort();
    model::ModelEndSequence(&seq);
    cleanup();
    SendError(fd, 409, "request cancelled during prefill");
    return;
  }
  if (!s.ok()) {
    model::ModelEndSequence(&seq);
    cleanup();
    SendError(fd, 500, "prefill failed: " + s.message());
    return;
  }

  diagnostic.Stage("decode");
  // 3. Decode loop (greedy).
  const std::string id = request_id;
  const std::string model_field = model_name_;
  const int created = static_cast<int>(std::time(nullptr));
  bool client_disconnected = false;
  const auto write_stream = [&](const std::string& data) {
    if (client_disconnected) return false;
    RequestDiagnostics::Scope wait(diagnostic, Wait::kWrite);
    if (!cancelled() && WriteAll(fd, data)) return true;
    client_disconnected = true;
    control->Cancel(RequestControl::State::kDisconnect);
    count_abort();
    return false;
  };
  detail::Utf8Stream utf8;
  bool first_content_sent = false;
  const auto write_content = [&](const std::string& piece) {
    if (piece.empty()) return true;
    if (!write_stream(SseChunk(id, model_field, "", piece, "", 0))) return false;
    if (!first_content_sent) {
      first_content_sent = true;
      metrics_.ttft_seconds.Observe(std::chrono::duration<double>(
          std::chrono::steady_clock::now() - t_arrive).count());
    }
    return true;
  };

  // Streaming header (flushed immediately).
  if (stream) {
    const std::string head =
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\nConnection: close\r\n\r\n";
    if (!write_stream(head) ||
        !write_stream(SseChunk(id, model_field, "assistant", "", "", 0))) {
      err_guard.ok = true;  // Count this as an abort, not a prefill error.
      model::ModelEndSequence(&seq);
      cleanup();
      return;
    }
  }

  std::vector<int32_t> generated;
  int next_token = -1;
  std::string finish_reason = "stop";
  bool generation_failed = false;

  // Every prefill path reads its selected final row under model_mu_ and
  // checks stream completion before publishing h_logits to this thread.
  next_token = argmax(h_logits.data());
  metrics_.target_ready_seconds.Observe(
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t_arrive)
          .count());
  err_guard.ok = true;  // generation has started; not a pre-generation error

  // MTP init (mirrors the CLI --mtp path): fresh draft KV, bonus token b =
  // t_P (the first decode token), draft-extend over the prompt to build the
  // draft KV[0..P-1] and seed the first speculative step (d0 + g). On any
  // failure fall back to plain decode (the main seq is still usable).
  //
  // Stage 2c (4b): the MTP path is per-sequence safe AND batched. The draft
  // KV is pooled per seq_id (mcfg.max_seq, Stage 1) and each request owns a
  // per-request rolling trunk (d_mtp_g). MTP init (per-seq reset + draft
  // extend) runs on this thread under model_mu_; the speculative STEPS are
  // registered with the central scheduler, which batches all concurrent MTP
  // requests into ONE MtpSpeculativeStepMulti (batched draft loop +
  // ModelVerifyMulti + batched extend, weights read once) — the same code path
  // a single MTP request takes (B=1). This thread advances the main seq over
  // the accepted prefix (the multi step does not touch seqs[b]).
  // MTP is enabled for BOTH one-shot and chunked prefill, as long as the
  // full-prompt trunk buffer was allocated (d_trunk_full != nullptr). The
  // chunked path accumulates the trunk across prefill chunks and runs the
  // draft-extend in chunks (MtpDraftExtend's chunked path). The draft
  // KV/indexer is allocated at MTP load time (not per-request), so the only
  // per-request cost is the trunk buffer; if its allocation failed, d_trunk_full
  // is null and we fall back to plain decode. MTP is precision-safe: the main
  // model verifies every draft token under the documented T>1 near-tie
  // contract; it need not be bit-identical to the T=1 arithmetic path.
  bool use_mtp = mtp_loaded_ && d_trunk_full != nullptr;
  int32_t mtp_b = stream_draft_ready ? next_token : -1;
  int32_t mtp_d0 = stream_draft_ready ? stream_d0 : -1;
  if (use_mtp && !stream_mtp) {
    if (cudaMalloc(reinterpret_cast<void**>(&d_mtp_g),
                   static_cast<size_t>(mtp_.cfg.hc) *
                       static_cast<size_t>(mtp_.cfg.hs) * 2) != cudaSuccess) {
      std::fprintf(stderr, "[q4t] MTP trunk alloc failed; plain decode\n");
      use_mtp = false;
    }
  }
  if (use_mtp && !stream_mtp) {
    {
      q4t::diagnostic::Batch execution_batch(request_id.c_str(), seq_id, 0, T);
      q4t::diagnostic::Scope execution_init("handler_mtp_init", 0, T);
      auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
      {
        q4t::diagnostic::Scope execution_reset("prompt_draft_reset", 0, T);
        s = !gpu_healthy_.load(std::memory_order_relaxed)
                ? Status::Fail("GPU unhealthy; restart server")
                : mtp::MtpResetState(mtp_, nullptr, seq_id);
      }
      if (s.ok()) {
        mtp_b = next_token;
        // EAGLE shift: shifted_ids[p] = t_{p+1}, with t_P := b at the tail.
        std::vector<int32_t> shifted(T);
        for (int i = 0; i < T - 1; ++i) shifted[i] = ids[i + 1];
        shifted[T - 1] = mtp_b;
        std::vector<int> pos(T);
        for (int i = 0; i < T; ++i) pos[i] = i;
        {
          // Aggregate call: includes internal submission, existing waits and
          // cleanup. Its child spans must not be added to this parent span.
          q4t::diagnostic::Scope execution_extend("prompt_draft_extend_call", 0,
                                                 T);
          s = mtp::MtpDraftExtend(mtp_, shifted.data(), d_trunk_full, pos.data(),
                                  T, &mtp_d0, d_mtp_g, nullptr, seq_id);
        }
        if (s.ok()) {
          q4t::diagnostic::Scope execution_reserve("prompt_verify_reserve", 0,
                                                  T);
          s = model::ModelReserveVerifyCheckpoints(model_.Get(), mtp_k_);
          if (s.ok())
            s = mtp::MtpReserveScratch(mtp_, mtp_k_ + 1);
        }
      }
      if (!s.ok()) {
        std::fprintf(stderr,
                     "[q4t] MTP init failed (%s); plain decode for this "
                     "request\n",
                     s.message().c_str());
        use_mtp = false;
      }

    }
  }
  // The prompt trunk is only needed for the draft-extend above; free it now so
  // decoding requests do not hold it (bounds concurrent memory).
  if (d_trunk_full) {
    q4t::diagnostic::Batch execution_batch(request_id.c_str(), seq_id, 0, T);
    q4t::diagnostic::Scope execution_free("prompt_trunk_free", 0, T);
    cudaFree(d_trunk_full);
    d_trunk_full = nullptr;
  }
  // Stage 2c (4b): the speculative steps are driven by the central scheduler,
  // which batches all concurrent MTP requests into ONE MtpSpeculativeStepMulti
  // (weights read once). This thread registers each step's input (mtp_b/d0/g),
  // blocks on the request's cv, and is woken with the step's output. It owns
  // the state-machine advance (position/history) over the accepted prefix.
  // Fallback: if the scheduler is unavailable or the pool is full, decode
  // plainly (the main seq is still usable).
  ActiveRequest mtp_ar;
  mtp::SuffixDrafter suffix;
  struct RequestClasses {
    ChatServer* server;
    uint64_t decodes = 0, lookups = 0, allocations = 0;
    size_t bytes = 0;
  } request_classes{this};
  std::array<uint64_t, 4> suffix_accepted{};
  std::array<uint64_t, 4> neural_accepted{};
  bool mtp_sched = false;
  if (use_mtp) {
    const std::lock_guard<std::mutex> lock(sched_mu_);
    mtp_sched = scheduler_active_ && !scheduler_stop_ && d_sched_logits_ &&
                static_cast<int>(active_.size()) < max_seq_;
    if (mtp_sched) {
      mtp_ar.is_mtp = true;
      mtp_ar.complete_terminal_draft = session.cache != nullptr;
      mtp_ar.session_last_trunk = d_session_trunk;
      mtp_ar.seq = &seq;
      mtp_ar.mtp_g = d_mtp_g;

      active_.push_back(&mtp_ar);
    } else {
      use_mtp = false;
    }
  }
  if (use_mtp && mtp_suffix_) {
    const Status suffix_status = suffix.Init(
        seq.history, model_.Get().cfg.vocab, request_capacity, {},
        {&request_classes,
         [](void* context, std::span<const int32_t> ids, bool* numeric) {
           auto& local = *static_cast<RequestClasses*>(context);
           auto* server = local.server;
           const std::lock_guard<std::mutex> lock(server->tok_mu_);
           auto& cache = server->suffix_token_cache_;
           const auto before = cache.Counters();
           const Status status = cache.Check(ids, numeric);
           const auto& after = cache.Counters();
           local.decodes += after.decodes - before.decodes;
           local.lookups += after.lookups - before.lookups;
           local.allocations += after.allocations - before.allocations;
           local.bytes = cache.MemoryBytes();
           return status;
         }});
    if (!suffix_status.ok())
      std::fprintf(stderr, "[suffix] request=%s disabled=%s\n",
                   request_id.c_str(), suffix_status.message().c_str());
  }
  if (use_mtp) {
    int accepted_histogram[17] = {};
    bool done = false;
    while (!done && static_cast<int>(generated.size()) < max_tokens) {
      if (cancelled()) break;
      if (!mtp_sched) break;
      if (is_stop_token(mtp_b)) {
        generated.push_back(mtp_b);
        finish_reason = "stop";
        break;
      }
      const int remaining = max_tokens - static_cast<int>(generated.size());
      if (!MtpGenerationStepFits(seq.position, request_capacity,
                                 model_.Get().cfg.max_prefill, mtp_k_,
                                 remaining)) {
        // No speculative work has started. Finish with ordinary decode,
        // preserving the pending token and the already emitted prefix.
        next_token = mtp_b;
        use_mtp = false;
        break;
      }
      // Register this step's input with the scheduler.
      mtp_ar.mtp_b = mtp_b;
      mtp_ar.mtp_d0 = mtp_d0;
      mtp_ar.mtp_suffix_ready = false;
      if (suffix.Active()) {
        const auto proposal = suffix.ProposeAfter(mtp_b, mtp_d0);
        mtp_ar.mtp_suffix_ready = proposal.ready;
        mtp_ar.mtp_suffix_tokens = proposal.tokens;
      }
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        mtp_ar.mtp_accepted_count = 0;
        mtp_ar.mtp_next_b = -1;
        mtp_ar.mtp_next_d0 = -1;
        mtp_ar.pending = !scheduler_stop_;
        mtp_ar.done = scheduler_stop_;
        if (scheduler_stop_) seq.Fail();
      }
      sched_cv_.notify_one();
      // Block until the scheduler's batched step yields this step's output.
      {
        RequestDiagnostics::Scope wait(diagnostic, Wait::kScheduler);
        std::unique_lock<std::mutex> lock(sched_mu_);
        mtp_ar.cv.wait(lock, [&mtp_ar] { return mtp_ar.done; });
      }
      if (mtp_ar.mtp_accepted_count <= 0 ||
          mtp_ar.mtp_accepted_count > mtp_k_ + 1) {
        // Scheduler failed the step or is shutting down.
        generation_failed = true;
        break;
      }
      ++accepted_histogram[mtp_ar.mtp_accepted_count - 1];
      if (mtp_suffix_) {
        auto& histogram = mtp_ar.mtp_suffix_ready ? suffix_accepted
                                                : neural_accepted;
        ++histogram[mtp_ar.mtp_accepted_count - 1];
      }
      for (int i = 0; i < mtp_ar.mtp_accepted_count &&
                          static_cast<int>(generated.size()) < max_tokens;
           ++i) {
        const int32_t tok_id = mtp_ar.mtp_accepted[i];
        generated.push_back(tok_id);
        if (stream) {
          std::vector<std::uint32_t> one(1, static_cast<std::uint32_t>(tok_id));
          std::string piece;
          bool wrote = true;
          {
            auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer);
            if (tok_->Decode(one, true, &piece).ok())
              wrote = write_content(utf8.Append(piece));
          }
          if (!wrote) {  // client disconnected: stop, free the slot
            done = true;
            finish_reason = "stop";
            break;
          }
        }
        if (is_stop_token(tok_id)) {
          done = true;
          break;
        }
      }
      // Advance the main seq over the accepted prefix [b, d_0..d_{a-1}] (the
      // multi step does not touch seqs[b]).
      seq.position += mtp_ar.mtp_accepted_count;
      for (int i = 0; i < mtp_ar.mtp_accepted_count; ++i)
        seq.history.push_back(mtp_ar.mtp_accepted[i]);
      if (suffix.Active()) {
        const Status suffix_status = suffix.Append(
            {mtp_ar.mtp_accepted,
             static_cast<size_t>(mtp_ar.mtp_accepted_count)});
        if (!suffix_status.ok())
          std::fprintf(stderr, "[suffix] request=%s disabled=%s\n",
                       request_id.c_str(), suffix_status.message().c_str());
      }
      if (static_cast<int>(generated.size()) >= max_tokens) {
        finish_reason = "length";
        break;
      }
      if (seq.position >= request_capacity) {
        finish_reason = "length";
        break;  // cannot decode further without exceeding the KV cache
      }
      mtp_b = mtp_ar.mtp_next_b;
      mtp_d0 = mtp_ar.mtp_next_d0;
      // d_mtp_g was overwritten in place with the next step's trunk.
    }
    flockfile(stderr);
    std::fprintf(stderr, "[mtp-accept] request=%s draft_counts=",
                 request_id.c_str());
    for (int i = 0; i <= mtp_k_; ++i)
      std::fprintf(stderr, "%s%d", i ? "," : "", accepted_histogram[i]);
    std::fprintf(stderr, "\n");
    funlockfile(stderr);
  }
  if (mtp_sched) {
    {
      const std::lock_guard<std::mutex> lock(sched_mu_);
      active_.erase(std::remove(active_.begin(), active_.end(), &mtp_ar),
                    active_.end());
    }
    // Lockstep predicate depends on active_ size: removing a request can
    // flip "pending_mtp == active_mtp" from false to true. Notify the
    // scheduler so it re-evaluates (fixes a lost-wakeup deadlock where the
    // departing request's notify was consumed before the scheduler slept).
    sched_cv_.notify_one();
  }
  // No scheduler owns this row now. Capture the complete target/shifted-draft
  // boundary before any ordinary-decode tail; publication waits for successful
  // HTTP completion. At most three emitted tail tokens will be recomputed.
  if (session.cache && mtp_sched && !generation_failed && !cancelled() &&
      seq.stage == model::ModelSequence::Stage::kDecode && !seq.HasPending() &&
      seq.position >= SessionCache::kMinPrefix &&
      seq.position < request_capacity) {
    const auto begin = std::chrono::steady_clock::now();
    SessionSaveTiming timing(request_id.c_str(), "private");
    std::unique_lock<std::mutex> lock;
    {
      SessionSaveTiming::Scope span(&timing, SessionSaveTiming::kModelLock);
      lock = diagnostic.Lock(model_mu_, Wait::kModel);
    }
    std::vector<SessionRange> ranges;
    Status saved;
    {
      SessionSaveTiming::Scope span(&timing, SessionSaveTiming::kRanges);
      saved = SessionStateRanges(model_.Get(), mtp_, seq_id, seq.position,
                                  d_mtp_g, d_session_trunk, &ranges);
    }
    const bool has_step = mtp_ar.mtp_accepted_count > 0;
    const int32_t pending = has_step ? mtp_ar.mtp_next_b : mtp_b;
    const int32_t last_d0 = has_step ? mtp_ar.mtp_next_d0 : mtp_d0;
    if (saved.ok() && pending >= 0 && last_d0 >= 0 &&
        model_.Get().rope_delta[seq_id] == 0) {
      std::vector<int32_t> prefix(seq.history);
      prefix.push_back(pending);
      saved = session_cache_.Save(session.key, prefix, ranges, session_copy,
                                    cancelled, last_d0, &timing);
    } else if (saved.ok()) {
      saved = Status::Fail("incomplete session boundary");
    }
    timing.Report(saved.ok());
    if (session_gpu_error) {
      generation_failed = true;
      use_mtp = true;  // Do not enter a plain tail after a device failure.
      seq.Fail();
    }
    std::fprintf(stderr,
                 "[session-cache] request=%s event=save prefix=%d "
                 "ms=%.3f disk_bytes=%zu error=%s\n", request_id.c_str(),
                 seq.position, std::chrono::duration<double, std::milli>(
                     std::chrono::steady_clock::now() - begin).count(),
                 session_cache_.Bytes(),
                 saved.ok() ? "none" : saved.message().c_str());
  }
  if (mtp_suffix_) {
    const size_t indexed = suffix.Size();
    suffix.Reset();
    const auto& stats = suffix.Counters();
    flockfile(stderr);
    std::fprintf(
        stderr,
        "[suffix] request=%s queries=%llu hits=%llu "
        "candidates=%llu compared=%llu matched=%llu "
        "no_match=%llu incomplete=%llu d0_mismatch=%llu "
        "invalid=%llu exhausted=%llu indexed=%zu peak_bytes=%zu "
        "object_bytes=%zu init_ns=%llu append_ns=%llu "
        "query_ns=%llu release_ns=%llu numeric_checks=%llu "
        "classified_tokens=%llu numeric_rejected=%llu "
        "numeric_ns=%llu short_rejected=%llu classify_failed=%llu "
        "cache_decodes=%llu cache_lookups=%llu "
        "cache_allocations=%llu cache_bytes=%zu accepted=",
        request_id.c_str(), static_cast<unsigned long long>(stats.queries),
        static_cast<unsigned long long>(stats.hits),
        static_cast<unsigned long long>(stats.candidates_examined),
        static_cast<unsigned long long>(stats.compared_tokens),
        static_cast<unsigned long long>(stats.matched_tokens),
        static_cast<unsigned long long>(stats.no_match),
        static_cast<unsigned long long>(stats.incomplete),
        static_cast<unsigned long long>(stats.first_token_mismatch),
        static_cast<unsigned long long>(stats.invalid_tokens),
        static_cast<unsigned long long>(stats.capacity_exhaustions), indexed,
        stats.peak_memory_bytes, sizeof(suffix),
        static_cast<unsigned long long>(stats.init_ns),
        static_cast<unsigned long long>(stats.append_ns),
        static_cast<unsigned long long>(stats.query_ns),
        static_cast<unsigned long long>(stats.release_ns),
        static_cast<unsigned long long>(stats.numeric_checks),
        static_cast<unsigned long long>(stats.classified_tokens),
        static_cast<unsigned long long>(stats.numeric_rejected),
        // numeric_ns is already included in query_ns.
        static_cast<unsigned long long>(stats.numeric_ns),
        static_cast<unsigned long long>(stats.short_rejected),
        static_cast<unsigned long long>(stats.classification_failures),
        static_cast<unsigned long long>(request_classes.decodes),
        static_cast<unsigned long long>(request_classes.lookups),
        static_cast<unsigned long long>(request_classes.allocations),
        request_classes.bytes);
    for (size_t i = 0; i < suffix_accepted.size(); ++i)
      std::fprintf(stderr, "%s%llu", i ? "," : "",
                   static_cast<unsigned long long>(suffix_accepted[i]));
    std::fprintf(stderr, " neural_accepted=");
    for (size_t i = 0; i < neural_accepted.size(); ++i)
      std::fprintf(stderr, "%s%llu", i ? "," : "",
                   static_cast<unsigned long long>(neural_accepted[i]));
    std::fprintf(stderr, "\n");
    funlockfile(stderr);
  }
  if (!use_mtp) {
    // B2b continuous batching: this request's decode steps are driven by the
    // central scheduler, which packs the current token of EVERY active
    // plain-decode request into ONE ModelDecodeBatchMulti (weights read once
    // for all B tokens). This thread only emits tokens and advances the
    // per-sequence state machine; the GPU forward is the scheduler's job.
    //
    // Fallback: if the scheduler is unavailable (buffer alloc failed) or the
    // active pool is full, this request decodes on its own via
    // ModelDecodeStepSeq (the B1 single-sequence path).
    bool use_sched = false;
    ActiveRequest ar;
    {
      const std::lock_guard<std::mutex> lock(sched_mu_);
      use_sched = scheduler_active_ && !scheduler_stop_ && d_sched_logits_ &&
                  static_cast<int>(active_.size()) < max_seq_;
      if (use_sched) {
        ar.seq = &seq;
        active_.push_back(&ar);
      }
    }
    for (int step = static_cast<int>(generated.size());
         step < max_tokens; ++step) {
      if (cancelled()) break;
      generated.push_back(next_token);
      if (is_stop_token(next_token)) {
        finish_reason = "stop";
        break;
      }
      const int32_t tok_id = next_token;
      // Emit the token text (decode this single token).
      if (stream) {
        std::vector<std::uint32_t> one(1, static_cast<std::uint32_t>(tok_id));
        std::string piece;
        bool wrote = true;
        {
          auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer);
          if (tok_->Decode(one, true, &piece).ok())
            wrote = write_content(utf8.Append(piece));
        }
        if (!wrote) {  // client disconnected: stop, free the slot
          finish_reason = "stop";
          break;
        }
      }
      // The current token is already emitted (or buffered for non-streaming).
      // Do not prepare another token when this request cannot consume it.
      if (step == max_tokens - 1 || seq.position + 1 >= request_capacity) {
        finish_reason = "length";
        break;
      }
      if (use_sched) {
        // Register this step's token with the scheduler (position + PLE
        // context are read from the per-sequence state BEFORE advancing).
        ar.token = tok_id;
        {
          const std::lock_guard<std::mutex> lock(sched_mu_);
          ar.next_token = -1;
          ar.pending = !scheduler_stop_;
          ar.done = scheduler_stop_;
          if (scheduler_stop_) seq.Fail();
        }
        sched_cv_.notify_one();
        // Block until the scheduler's packed forward yields this step's token.
        {
          RequestDiagnostics::Scope wait(diagnostic, Wait::kScheduler);
          std::unique_lock<std::mutex> lock(sched_mu_);
          ar.cv.wait(lock, [&ar] { return ar.done; });
        }
        if (ar.next_token < 0) {
          // Scheduler failed the forward or is shutting down.
          generation_failed = true;
          break;
        }
        next_token = ar.next_token;
      } else {
        // Fallback: single-sequence decode (B1 path).
        auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
        if (router_trace_) router_trace_->BeginForward(
            trace::RouteStage::kDecode, seq.position, 1);
        s = !gpu_healthy_.load(std::memory_order_relaxed)
                ? Status::Fail("GPU unhealthy; restart server")
                : model::ModelDecodeStepSeq(model_.Get(), &seq, tok_id, d_prefill_logits_,
                                      nullptr, nullptr, seq_id,
                                      model::SequenceCompletion::kDeferred);
        cudaError_t copy_error = cudaSuccess;
        if (s.ok())
          copy_error = cudaMemcpyAsync(
              h_logits.data(), d_prefill_logits_,
              static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost, nullptr);
        model::ModelSequence* sequence = &seq;
        s = FinishHostReadback(copy_error, &gpu_healthy_, {&sequence, 1}, s, router_trace_.get());
        if (!s.ok()) {
          generation_failed = true;
          break;
        }
        next_token = argmax(h_logits.data());
      }
    }
    if (use_sched) {
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        active_.erase(std::remove(active_.begin(), active_.end(), &ar),
                      active_.end());
      }
      // Lockstep depends on active_ size: a departing request can flip
      // "all active pending" to true, so wake the scheduler to re-evaluate.
      sched_cv_.notify_one();
    }
  }
  trace_output_tokens = generated.size();
  if (generation_failed) seq.Fail();
  model::ModelEndSequence(&seq);
  LogMemorySnapshot("request_end", id);
  if (residency_enabled_) {
    // Per-request residency load activity (cumulative counters; max_seq=1
    // makes the delta exact). Evidences on-demand load volume for acceptance.
    const auto now = model::SumResidencyStats(model_.Get());
    const auto& prev = residency_stats_prev_;
    std::fprintf(stderr,
                 "[q4t][residency] id=%s finish=%s in=%d out=%zu "
                 "loads=%llu load_mb=%.1f misses=%llu hits=%llu "
                 "evictions=%llu dmiss=%llu dlook=%llu pmiss=%llu "
                 "plook=%llu l2h=%llu l2m=%llu l2ev=%llu nvme_mb=%.1f "
                 "mh=%llu mw=%llu msk=%llu ld2h=%llu ld2m=%llu "
                 "ld2ev=%llu lp2h=%llu lp2m=%llu lp2ev=%llu "
                 "pmr=%llu pme=%llu\n",
                 id.c_str(), finish_reason.c_str(), T, generated.size(),
                 static_cast<unsigned long long>(now.loads - prev.loads),
                 (now.load_bytes - prev.load_bytes) / 1e6,
                 static_cast<unsigned long long>(now.misses - prev.misses),
                 static_cast<unsigned long long>(now.hits - prev.hits),
                 static_cast<unsigned long long>(
                     now.evictions - prev.evictions),
                 static_cast<unsigned long long>(
                     now.decode_misses - prev.decode_misses),
                 static_cast<unsigned long long>(
                     now.decode_lookups - prev.decode_lookups),
                 static_cast<unsigned long long>(
                     now.prefill_misses - prev.prefill_misses),
                 static_cast<unsigned long long>(
                     now.prefill_lookups - prev.prefill_lookups),
                 static_cast<unsigned long long>(now.l2_hits - prev.l2_hits),
                 static_cast<unsigned long long>(
                     now.l2_misses - prev.l2_misses),
                 static_cast<unsigned long long>(
                     now.l2_evictions - prev.l2_evictions),
                 (now.nvme_read_bytes - prev.nvme_read_bytes) / 1e6,
                 static_cast<unsigned long long>(
                     now.mirror_hits - prev.mirror_hits),
                 static_cast<unsigned long long>(
                     now.mirror_writebacks - prev.mirror_writebacks),
                 static_cast<unsigned long long>(
                     now.mirror_skips - prev.mirror_skips),
                 static_cast<unsigned long long>(
                     now.l2_decode_hits - prev.l2_decode_hits),
                 static_cast<unsigned long long>(
                     now.l2_decode_misses - prev.l2_decode_misses),
                 static_cast<unsigned long long>(
                     now.l2_decode_evictions - prev.l2_decode_evictions),
                 static_cast<unsigned long long>(
                     now.l2_prefill_hits - prev.l2_prefill_hits),
                 static_cast<unsigned long long>(
                     now.l2_prefill_misses - prev.l2_prefill_misses),
                 static_cast<unsigned long long>(
                     now.l2_prefill_evictions - prev.l2_prefill_evictions),
                 static_cast<unsigned long long>(
                     now.pread_merge_runs - prev.pread_merge_runs),
                 static_cast<unsigned long long>(
                     now.pread_merge_experts - prev.pread_merge_experts));
    residency_stats_prev_ = now;
    // Optional per-miss pipeline timing (Q4T_RESIDENCY_TIMING=1). Per-request
    // deltas of count/ns; max is cumulative (upper bound). Diagnostic only.
    const auto tim_now = model::SumResidencyTiming(model_.Get());
    if (tim_now.enabled) {
      const auto& tp = residency_timing_prev_;
      auto avg_us = [](uint64_t ns, uint64_t cnt) {
        return cnt ? (static_cast<double>(ns) / cnt) / 1e3 : 0.0;
      };
      std::fprintf(stderr,
                   "[q4t][residency][timing] id=%s "
                   "stage_n=%llu stage_avg_us=%.1f stage_max_us=%.1f "
                   "pread_n=%llu pread_avg_us=%.1f pread_max_us=%.1f "
                   "swz_n=%llu swz_avg_us=%.1f swz_max_us=%.1f "
                   "phase1_n=%llu phase1_avg_us=%.1f phase1_max_us=%.1f "
                   "dstage_n=%llu dstage_avg_us=%.1f dstage_max_us=%.1f "
                   "dpread_n=%llu dpread_avg_us=%.1f dpread_max_us=%.1f "
                   "dphase1_n=%llu dphase1_avg_us=%.1f dphase1_max_us=%.1f "
                   "d2h_n=%llu d2h_avg_us=%.1f d2h_max_us=%.1f "
                   "pmrun_n=%llu pmrun_avg_us=%.1f pmrun_max_us=%.1f\n",
                   id.c_str(),
                   static_cast<unsigned long long>(
                       tim_now.stage_count - tp.stage_count),
                   avg_us(tim_now.stage_ns - tp.stage_ns,
                          tim_now.stage_count - tp.stage_count),
                   tim_now.stage_max_ns / 1e3,
                   static_cast<unsigned long long>(
                       tim_now.pread_count - tp.pread_count),
                   avg_us(tim_now.pread_ns - tp.pread_ns,
                          tim_now.pread_count - tp.pread_count),
                   tim_now.pread_max_ns / 1e3,
                   static_cast<unsigned long long>(
                       tim_now.swz_count - tp.swz_count),
                   avg_us(tim_now.swz_ns - tp.swz_ns,
                          tim_now.swz_count - tp.swz_count),
                   tim_now.swz_max_ns / 1e3,
                   static_cast<unsigned long long>(
                       tim_now.phase1_count - tp.phase1_count),
                   avg_us(tim_now.phase1_ns - tp.phase1_ns,
                          tim_now.phase1_count - tp.phase1_count),
                   tim_now.phase1_max_ns / 1e3,
                   static_cast<unsigned long long>(
                       tim_now.dstage_count - tp.dstage_count),
                   avg_us(tim_now.dstage_ns - tp.dstage_ns,
                          tim_now.dstage_count - tp.dstage_count),
                   tim_now.dstage_max_ns / 1e3,
                   static_cast<unsigned long long>(
                       tim_now.dpread_count - tp.dpread_count),
                   avg_us(tim_now.dpread_ns - tp.dpread_ns,
                          tim_now.dpread_count - tp.dpread_count),
                   tim_now.dpread_max_ns / 1e3,
                   static_cast<unsigned long long>(
                       tim_now.dphase1_count - tp.dphase1_count),
                   avg_us(tim_now.dphase1_ns - tp.dphase1_ns,
                          tim_now.dphase1_count - tp.dphase1_count),
                   tim_now.dphase1_max_ns / 1e3,
                   static_cast<unsigned long long>(
                       tim_now.d2h_count - tp.d2h_count),
                   avg_us(tim_now.d2h_ns - tp.d2h_ns,
                          tim_now.d2h_count - tp.d2h_count),
                   tim_now.d2h_max_ns / 1e3,
                   static_cast<unsigned long long>(
                       tim_now.pread_merge_count - tp.pread_merge_count),
                   avg_us(tim_now.pread_merge_ns - tp.pread_merge_ns,
                          tim_now.pread_merge_count - tp.pread_merge_count),
                   tim_now.pread_merge_max_ns / 1e3);
      residency_timing_prev_ = tim_now;
    }
  }

  metrics_.prompt_tokens_total.fetch_add(static_cast<uint64_t>(T),
                                         std::memory_order_relaxed);
  metrics_.generation_tokens_total.fetch_add(generated.size(),
                                             std::memory_order_relaxed);
  metrics_.e2e_seconds.Observe(
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t_arrive)
          .count());

  // The handler has left every scheduler list and all submitted work is
  // complete. Cancellation remains possible until the response is sent.
  if (cancelled()) {
    count_abort();
    if (stream) {
      WriteAll(fd,
               "data: {\"error\":{\"message\":\"request cancelled\"}}\r\n\r\n");
      WriteAll(fd, "data: [DONE]\r\n\r\n");
    } else {
      SendError(fd, 409, "request cancelled");
    }
    cleanup();
    return;
  }
  if (generation_failed) {
    err_guard.ok = false;
    std::fprintf(stderr, "[q4t] generation failed id=%s\n", id.c_str());
    // The HTTP status is already committed for streaming requests. Publish
    // an error terminal, never a normal stop/usage or a successful request.
    if (stream) {
      WriteAll(fd,
               "data: {\"error\":{\"message\":\"generation failed\","
               "\"type\":\"server_error\",\"code\":\"generation_failed\"}}"
               "\r\n\r\n");
      WriteAll(fd, "data: [DONE]\r\n\r\n");
    } else {
      SendSimple(fd, 500, "Internal Server Error",
                 "{\"error\":{\"message\":\"generation failed\","
                 "\"type\":\"server_error\",\"code\":\"generation_failed\"}}",
                 "application/json");
    }
    cleanup();
    return;
  }
  diagnostic.Stage("response");
  const std::string cached_usage = (session.cache || !prefix_scope.empty())
      ? ",\"prompt_tokens_details\":{\"cached_tokens\":" +
            std::to_string(restored_prefix) + "}"
      : "";
  // 4. Finalize.
  if (stream) {
    write_content(utf8.Finish());
    write_stream(SseChunk(id, model_field, "", "", finish_reason, 0));
    if (include_usage && !client_disconnected) {
      const std::string usage_chunk =
          "data: {\"id\":\"" + id +
          "\",\"object\":\"chat.completion.chunk\",\"created\":" +
          std::to_string(created) + ",\"model\":\"" +
          JsonEscape(model_field) + "\",\"choices\":[],\"usage\":{" +
          "\"prompt_tokens\":" + std::to_string(T) +
          ",\"completion_tokens\":" + std::to_string(generated.size()) +
          ",\"total_tokens\":" + std::to_string(T + generated.size()) + cached_usage +
          "}}\r\n\r\n";
      write_stream(usage_chunk);
    }
    write_stream("data: [DONE]\r\n\r\n");
  } else {
    std::vector<std::uint32_t> gen_u32(generated.begin(), generated.end());
    std::string text;
    bool decoded = false;
    {
      auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer);
      decoded = tok_->Decode(gen_u32, true, &text).ok();
    }
    if (decoded) {
      text = detail::NormalizeUtf8(text);
      std::string resp =
          "{\"id\":\"" + id + "\",\"object\":\"chat.completion\","
          "\"created\":" +
          std::to_string(created) + ",\"model\":\"" + JsonEscape(model_field) +
          "\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\","
          "\"content\":\"" +
          JsonEscape(text) +
          "\"},\"finish_reason\":\"" + finish_reason + "\"}],"
          "\"usage\":{\"prompt_tokens\":" +
          std::to_string(T) +
          ",\"completion_tokens\":" + std::to_string(generated.size()) +
          ",\"total_tokens\":" + std::to_string(T + generated.size()) +
          cached_usage + "}";
      resp += "}";
      if (!SendSimple(fd, 200, "OK", resp, "application/json")) {
        client_disconnected = true;
        control->Cancel(RequestControl::State::kDisconnect);
        count_abort();
      }
    } else {
      err_guard.ok = false;
      SendError(fd, 500, "decode failed");
      cleanup();
      return;
    }
  }

  const bool response_success = !client_disconnected &&
                                !control->Cancelled() && control->Finish();
  trace_outcome = response_success ? trace::RequestOutcome::kSuccess
                                  : trace::RequestOutcome::kCancelled;
  if (response_success) {
    metrics_.response_seconds.Observe(std::chrono::duration<double>(
        std::chrono::steady_clock::now() - t_arrive).count());
    session.success = true;
    for (auto& lease : shared_writes) lease->success = true;
    diagnostic.Outcome("success", "completed");
    metrics_.requests_success.fetch_add(1, std::memory_order_relaxed);
  } else {
    count_abort();
    diagnostic.Outcome("cancelled", control->Reason());
  }
  cleanup();
}

}  // namespace q4t::server
