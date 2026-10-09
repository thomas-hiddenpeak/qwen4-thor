// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <memory>
#include <span>
#include <string>
#include <vector>
#include <cuda_runtime.h>
#include "q4t/mtp/init_timing.h"
#include "q4t/quant/moe_gemm.h"
#include "q4t/trace/mtp_cycle_timing.h"
#include "q4t/trace/mtp_verify_moe_timing.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/mtp_policy.h"
#include "q4t/server/request_json.h"
#include "q4t/server/scheduler_submission.h"

namespace q4t::server {
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
  const int chat_limit = std::max(8, max_seq_ * 2);
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
  auto deadline = t_arrive + std::chrono::minutes(20);
  if (const io::Json* timeout = req.Find("request_timeout_ms")) {
    const double value = timeout->AsDouble(-1);
    if (!timeout->IsNumber() || !(value >= 1 && value <= 1200000) ||
        value != static_cast<double>(static_cast<int64_t>(value))) {
      SendError(fd, 400,
                "request_timeout_ms must be an integer in [1,1200000]");
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
  struct Registration {
    RequestRegistry& registry;
    std::shared_ptr<RequestControl> control;
    ~Registration() { registry.Remove(control); }
  } registration{requests_, control};
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

  // B1 multi-request scheduling: each request runs on its own thread. All
  // GPU work uses the DEFAULT stream (the model's per-forward scratch is a
  // single shared allocation, and per-sequence state is pooled + isolated by
  // seq_id), so model forwards are serialized behind model_mu_ for both
  // scratch safety and GPU-stream ordering. GPU readback completes under
  // the lock; tokenization and SSE output use the completed host result.
  const int seq_id = AllocSeqId(control.get(), fd);
  if (seq_id == -2) {
    count_abort();
    SendError(fd, 409, "request cancelled before admission");
    return;
  }
  if (seq_id < 0) {
    SendError(fd, 503, "server shutting down");
    return;
  }
  metrics_.queue_seconds.Observe(
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t_arrive)
          .count());
  // Per-request device buffers (freed by `cleanup` on any exit path). Prefill
  // logits use the shared d_prefill_logits_ (under model_mu_), not a per-
  // request buffer.
  uint16_t* d_trunk_full = nullptr;
  uint16_t* d_vfeats = nullptr;
  uint16_t* d_mtp_g = nullptr;  // MTP rolling draft trunk [hc*hs] (Stage 2c:
                                // per-request, so concurrent MTP requests don't
                                // share the legacy d_g_/d_g_next_ double buffer)
  trace::RequestOutcome trace_outcome = trace::RequestOutcome::kFailed;
  uint64_t trace_output_tokens = 0;
  auto cleanup = [&]() {
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
  };

  if (cancelled()) {
    count_abort();
    cleanup();
    SendError(fd, 409, "request cancelled before encoding");
    return;
  }

  // 1. Encode prompt (CPU, outside model_mu_). The tokenizer's ICU regex
  // engine is not thread-safe, so Encode is serialized behind tok_mu_.
  std::vector<std::uint32_t> prompt_u32;
  {
    const std::lock_guard<std::mutex> lock(tok_mu_);
    s = tok_->Encode(prompt, &prompt_u32);
  }
  if (!s.ok()) {
    SendError(fd, 400, "encode failed: " + s.message());
    cleanup();
    return;
  }
  std::vector<int32_t> ids(prompt_u32.begin(), prompt_u32.end());

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
      const std::lock_guard<std::mutex> lock(model_mu_);
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
  if (T >= max_len_) {
    SendError(fd, 400,
              "prompt too long for context: " + std::to_string(T) +
                  " tokens >= " + std::to_string(max_len_) + " max_len");
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
  // max_len). Per-request costs also include the draft-extend temporaries;
  // the startup allocation estimate accounts for their overlap. If this
  // trunk allocation fails, fall back to plain decode and record the path.
  const size_t trunk_hc_dim =
      mtp_loaded_
          ? static_cast<size_t>(mtp_.cfg.hc) * static_cast<size_t>(mtp_.cfg.hs)
          : 0;
  const char* mtp_fallback =
      !mtp_loaded_ && mtp_requested_ ? "load_failed" : "none";
  const bool sequential_mtp =
      mtp_requested_ && mtp_verifier_ == MtpVerifier::kSequential;
  const char* tail_reason = "none";
  uint64_t draft_forward_calls = 0;
  uint64_t target_t1_calls = 0;
  uint64_t extend_forward_calls = 0;
  // Snapshot after acquiring the sequence slot, before any main forward.
  // With one slot the process-counter delta belongs to this request. With
  // concurrent slots it is only a process window, never per-request routing.
  const bool batch_gather_enabled = quant::MoEBatchGatherEnabled();
  const quant::MoEBatchGatherStats batch_gather_begin =
      batch_gather_enabled ? quant::GetMoEBatchGatherStats()
                           : quant::MoEBatchGatherStats{};
  std::unique_ptr<trace::MtpVerifyMoeTiming> verify_moe_timing;
  const char* verify_moe_env = getenv("Q4T_MTP_VERIFY_MOE_TIMING");
  if (verify_moe_env && std::strcmp(verify_moe_env, "1") == 0) {
    const bool supported =
        mtp_loaded_ && max_seq_ == 1 && items.empty() && mtp_k_ == 3;
    try {
      verify_moe_timing = std::make_unique<trace::MtpVerifyMoeTiming>(
          request_id, T, max_tokens, mtp_k_, supported);
    } catch (...) {
      std::fprintf(stderr,
                   "[q4t][mtp_verify_moe_timing] {\"schema_version\":1,"
                   "\"response_id\":\"%s\",\"valid\":false,"
                   "\"complete\":false,\"error\":\"setup_exception\"}\n",
                   request_id.c_str());
    }
  }
  std::unique_ptr<trace::MtpCycleTiming> cycle_timing;
  const char* cycle_env = getenv("Q4T_MTP_CYCLE_TIMING");
  if (cycle_env && std::strcmp(cycle_env, "1") == 0 && mtp_loaded_ &&
      max_seq_ == 1 && items.empty()) {
    try {
      cycle_timing = std::make_unique<trace::MtpCycleTiming>(
          request_id, T, max_tokens, mtp_k_, t_arrive);
    } catch (...) {
      std::fprintf(stderr,
                   "[q4t][mtp_cycle_timing] {\"schema_version\":1,"
                   "\"response_id\":\"%s\",\"valid\":false,"
                   "\"complete\":false,\"error\":\"setup_exception\"}\n",
                   request_id.c_str());
    }
  }
  std::unique_ptr<mtp::MtpInitTiming> init_timing;
  const char* timing_env = getenv("Q4T_MTP_INIT_TIMING");
  if (timing_env && std::strcmp(timing_env, "1") == 0 && mtp_loaded_ &&
      max_seq_ == 1 && items.empty()) {
    try {
      init_timing = std::make_unique<mtp::MtpInitTiming>(
          request_id, T, chunk, static_cast<size_t>(T) * trunk_hc_dim * 2,
          t_arrive);
    } catch (...) {
      std::fprintf(stderr,
                   "[q4t][mtp_init_timing] {\"schema_version\":1,"
                   "\"response_id\":\"%s\",\"valid\":false,"
                   "\"complete\":false,\"error\":\"setup_exception\"}\n",
                   request_id.c_str());
    }
  }
  if (init_timing) init_timing->MarkHost("trunk_prepare_begin");
  if (mtp_loaded_) {
    // Runtime heuristic in addition to the startup allocation estimate.
    // MemAvailable includes reclaimable file cache; it cannot guarantee that
    // a CUDA allocation succeeds or that the system avoids an OOM.
    const size_t trunk_bytes = static_cast<size_t>(T) * trunk_hc_dim * 2;
    const size_t draft_logits_bytes =
        static_cast<size_t>(max_prefill_) *
        static_cast<size_t>(model_.Get().cfg.vocab) * 2;
    const size_t kPreflightMargin = 2u * 1024u * 1024u * 1024u;  // 2 GB
    const size_t need = trunk_bytes + draft_logits_bytes + kPreflightMargin;
    const size_t mem_avail = runtime::ReadMemAvailable();
    if (mem_avail > 0 && mem_avail < need) {
      std::fprintf(
          stderr,
          "[q4t] OOM preflight: MemAvailable=%.2f GB < need=%.2f GB (trunk "
          "T=%d); plain decode\n",
          mem_avail / 1e9, need / 1e9, T);
      d_trunk_full = nullptr;
      mtp_fallback = "memory_preflight";
    } else {
      if (init_timing) init_timing->MarkHost("trunk_alloc_begin");
      const auto allocation =
          cudaMalloc(reinterpret_cast<void**>(&d_trunk_full), trunk_bytes);
      if (init_timing) init_timing->MarkHost("trunk_alloc_end");
      if (allocation != cudaSuccess) {
        std::fprintf(stderr,
                     "[q4t] trunk alloc failed for T=%d; plain decode\n", T);
        d_trunk_full = nullptr;
        mtp_fallback = "trunk_allocation";
      }
    }
  }
  if (init_timing) init_timing->MarkHost("trunk_prepare_end");
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
  if (scheduled_chunk_prefill) {
    ChunkPrefillReq pr;
    pr.fd = fd;
    pr.control = control.get();
    pr.seq = &seq;
    pr.seq_id = seq_id;
    pr.ids = ids.data();
    pr.len = T;
    pr.h_logits = h_logits.data();
    SubmitSchedulerRequestAndWait(
        sched_mu_, scheduler_stop_, sched_cv_, pr.cv,
        [&] { chunk_prefill_pending_.push_back(&pr); },
        [&] { return pr.done; });
    prefill_cancelled = pr.cancelled;
    s = pr.ok ? Status() : Status::Fail("scheduled chunk prefill failed");
  } else if (batched_prefill) {
    PrefillReq pr;
    pr.control = control.get();
    pr.fd = fd;
    pr.seq = &seq;
    pr.seq_id = seq_id;
    pr.ids = ids.data();
    pr.len = T;
    pr.h_logits = h_logits.data();
    SubmitSchedulerRequestAndWait(
        sched_mu_, scheduler_stop_, sched_cv_, pr.cv,
        [&] {
          pr.pending = true;
          prefill_pending_.push_back(&pr);
        },
        [&] { return pr.done; });
    s = pr.ok ? Status() : Status::Fail("batched prefill failed");
  } else {
    if (init_timing) init_timing->MarkHost("main_lock_wait_begin");
    const std::lock_guard<std::mutex> lock(model_mu_);
    if (init_timing) {
      init_timing->MarkHost("main_lock_acquired");
      init_timing->MarkHost("main_reset_begin");
    }
    s = cancelled() ? Status::Fail("request cancelled")
                    : model::ModelBeginSequence(model_.Get(), &seq, nullptr, seq_id);
    if (init_timing) {
      init_timing->MarkHost("main_reset_end");
      init_timing->MarkHost("main_prefill_begin");
      init_timing->MarkOuter(mtp::InitOuterPoint::kPrefillBegin, nullptr);
    }
    if (s.ok()) {
      if (!chunked) {
        if (init_timing) init_timing->MarkHost("main_chunk_begin", 0, T);
        if (router_trace_) router_trace_->BeginForward(
            trace::RouteStage::kPrefill, 0, T);
        // Keep full trunk output for MTP, but only the final logits row.
        s = model::ModelPrefill(model_.Get(), &seq, ids.data(), T, d_prefill_logits_,
                                nullptr, mtp_loaded_ ? d_trunk_full : nullptr,
                                vptr, seq_id, model::LogitsRows::kLastRow,
                                model::SequenceCompletion::kDeferred);
        if (init_timing) init_timing->MarkHost("main_chunk_submit_end", 0, T);
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
          if (init_timing)
            init_timing->MarkHost("main_chunk_begin", base, c);
          if (router_trace_) router_trace_->BeginForward(
              trace::RouteStage::kPrefill, base, c);
          s = model::ModelPrefillTextChunk(
              model_.Get(), &seq, ids.data(), T, c,
              last ? d_prefill_logits_ : nullptr, nullptr,
              d_trunk_full
                  ? d_trunk_full + static_cast<size_t>(base) * trunk_hc_dim
                  : nullptr, model::LogitsRows::kLastRow,
              model::SequenceCompletion::kDeferred);
          if (init_timing)
            init_timing->MarkHost("main_chunk_submit_end", base, c);
          if (!last) {
            model::ModelSequence* sequence = &seq;
            s = FinishHostReadback(cudaSuccess, &gpu_healthy_, {&sequence, 1},
                                   s, router_trace_.get());
            if (init_timing)
              init_timing->MarkHost("main_chunk_complete", base, c);
          }
          if (last || !s.ok()) break;
        }
      }
    }
    if (init_timing)
      init_timing->MarkOuter(mtp::InitOuterPoint::kPrefillEnd, nullptr);
    cudaError_t copy_error = cudaSuccess;
    if (s.ok())
      copy_error = cudaMemcpyAsync(
          h_logits.data(), d_prefill_logits_, static_cast<size_t>(vocab) * 2,
          cudaMemcpyDeviceToHost, nullptr);
    model::ModelSequence* sequence = &seq;
    // Drain and commit while holding model_mu_, also on failed submission.
    const auto pending = seq.HasPending()
        ? std::span<model::ModelSequence* const>(&sequence, 1)
        : std::span<model::ModelSequence* const>();
    s = FinishHostReadback(copy_error, &gpu_healthy_, pending, s, router_trace_.get());
    if (init_timing) {
      const int last_base = ((T - 1) / chunk) * chunk;
      init_timing->MarkHost("main_chunk_complete", last_base, T - last_base);
      init_timing->MarkHost("main_prefill_end");
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

  // 3. Decode loop (greedy).
  const std::string id = request_id;
  const std::string model_field = model_name_;
  const int created = static_cast<int>(std::time(nullptr));
  bool client_disconnected = false;
  const auto write_stream = [&](const std::string& data) {
    if (client_disconnected) return false;
    if (!cancelled() && WriteAll(fd, data)) return true;
    client_disconnected = true;
    control->Cancel(RequestControl::State::kDisconnect);
    count_abort();
    return false;
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
  if (init_timing) init_timing->MarkHost("prefill_argmax_begin");
  next_token = argmax(h_logits.data());
  if (init_timing) init_timing->MarkHost("prefill_argmax_end");
  metrics_.ttft_seconds.Observe(
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
  // trunk is one of the per-request costs. Exact equivalence to plain greedy
  // also depends on the verifier's numerical/state contract, tested separately.
  const bool prefill_only = max_tokens == 1 || is_stop_token(next_token);
  if (cycle_timing) cycle_timing->MarkRequest("main_prefill_finished");
  int mtp_steps = 0;
  int plain_steps = 0;
  int plain_tail_tokens = 0;
  bool plain_tail = false;
  bool use_mtp = mtp_loaded_ && d_trunk_full != nullptr && !prefill_only;
  if (use_mtp && sequential_mtp &&
      !MtpSequentialStepFits(seq.position, max_len_, max_prefill_, mtp_k_,
                            max_tokens)) {
    use_mtp = false;
    plain_tail = true;
    tail_reason = max_tokens <= mtp_k_ + 1 ? "output_limit" : "context_limit";
  } else if (use_mtp &&
             !MtpStepFits(seq.position, max_len_, max_prefill_, mtp_k_)) {
    use_mtp = false;
    mtp_fallback = "context_tail";
  }
  int32_t mtp_b = -1, mtp_d0 = -1;
  if (use_mtp) {
    if (init_timing) init_timing->MarkHost("rolling_trunk_alloc_begin");
    if (cudaMalloc(reinterpret_cast<void**>(&d_mtp_g),
                   static_cast<size_t>(mtp_.cfg.hc) *
                       static_cast<size_t>(mtp_.cfg.hs) * 2) != cudaSuccess) {
      std::fprintf(stderr, "[q4t] MTP trunk alloc failed; plain decode\n");
      use_mtp = false;
      mtp_fallback = "rolling_trunk_allocation";
    }
    if (init_timing) init_timing->MarkHost("rolling_trunk_alloc_end");
  }
  if (use_mtp) {
    {
      if (init_timing) init_timing->MarkHost("init_lock_wait_begin");
      const std::lock_guard<std::mutex> lock(model_mu_);
      if (init_timing) {
        init_timing->MarkHost("init_lock_acquired");
        init_timing->MarkHost("draft_reset_begin");
        init_timing->MarkOuter(mtp::InitOuterPoint::kResetBegin, nullptr);
      }
      s = mtp::MtpResetState(mtp_, nullptr, seq_id);
      if (init_timing) {
        init_timing->MarkOuter(mtp::InitOuterPoint::kResetEnd, nullptr);
        init_timing->MarkHost("draft_reset_end");
      }
      if (s.ok()) {
        mtp_b = next_token;
        // EAGLE shift: shifted_ids[p] = t_{p+1}, with t_P := b at the tail.
        if (init_timing) init_timing->MarkHost("shift_begin");
        std::vector<int32_t> shifted(T);
        for (int i = 0; i < T - 1; ++i) shifted[i] = ids[i + 1];
        shifted[T - 1] = mtp_b;
        std::vector<int> pos(T);
        for (int i = 0; i < T; ++i) pos[i] = i;
        if (init_timing) {
          init_timing->MarkHost("shift_end");
          init_timing->MarkHost("extend_begin");
        }
        const bool skip_unused_tail =
            max_seq_ == 1 && seq_id == 0 && items.empty() &&
            mtp_.cfg.max_prefill == 8192 && T > mtp_.cfg.max_prefill;
        const auto init_policy = skip_unused_tail
                                     ? mtp::MtpInitPolicy::kSkipUnusedTail
                                     : mtp::MtpInitPolicy::kFull;
        s = mtp::MtpDraftExtend(mtp_, shifted.data(), d_trunk_full, pos.data(),
                                T, &mtp_d0, d_mtp_g, nullptr, seq_id,
                                model::LogitsRows::kLastRow, init_timing.get(),
                                init_policy);
        if (init_timing) init_timing->MarkHost("extend_end");
        if (s.ok()) {
          if (init_timing) init_timing->MarkHost("checkpoint_reserve_begin");
          if (!sequential_mtp)
            s = model::ModelReserveVerifyCheckpoints(model_.Get(), mtp_k_);
          if (init_timing) init_timing->MarkHost("checkpoint_reserve_end");
          if (s.ok()) {
            if (init_timing) init_timing->MarkHost("scratch_reserve_begin");
            s = mtp::MtpReserveScratch(mtp_, mtp_k_ + 1);
            if (init_timing) init_timing->MarkHost("scratch_reserve_end");
          }
        }
      }
      if (init_timing) init_timing->FinishInit(s.ok());
      if (!s.ok()) {
        std::fprintf(stderr,
                     "[q4t] MTP init failed (%s); plain decode for this "
                     "request\n",
                     s.message().c_str());
        use_mtp = false;
        mtp_fallback = "initialization";
      }

    }
  }
  // The prompt trunk is only needed for the draft-extend above; free it now so
  // decoding requests do not hold it (bounds concurrent memory).
  if (d_trunk_full) {
    if (init_timing) init_timing->MarkHost("trunk_free_begin");
    cudaFree(d_trunk_full);
    d_trunk_full = nullptr;
    if (init_timing) init_timing->MarkHost("trunk_free_end");
  }
  if (cycle_timing) cycle_timing->MarkRequest("init_finished");
  // Stage 2c (4b): the speculative steps are driven by the central scheduler,
  // which batches all concurrent MTP requests into ONE MtpSpeculativeStepMulti
  // (weights read once). This thread registers each step's input (mtp_b/d0/g),
  // blocks on the request's cv, and is woken with the step's output. It owns
  // the state-machine advance (position/history) over the accepted prefix.
  // Fallback: if the scheduler is unavailable or the pool is full, decode
  // plainly (the main seq is still usable).
  ActiveRequest mtp_ar;
  bool mtp_sched = false;
  if (use_mtp) {
    const std::lock_guard<std::mutex> lock(sched_mu_);
    mtp_sched = scheduler_active_ && !scheduler_stop_ && d_sched_logits_ &&
                static_cast<int>(active_.size()) < max_seq_;
    if (mtp_sched) {
      mtp_ar.is_mtp = true;
      mtp_ar.seq = &seq;
      mtp_ar.mtp_g = d_mtp_g;

      active_.push_back(&mtp_ar);
    } else {
      // No speculative step has touched main state. Preserve the pending
      // prefill token and enter the ordinary decode loop below.
      use_mtp = false;
      mtp_fallback = "scheduler_unavailable";
    }
  }
  if (use_mtp) {
    bool done = false;
    while (!done && static_cast<int>(generated.size()) < max_tokens) {
      if (cancelled()) break;
      if (is_stop_token(mtp_b)) {
        generated.push_back(mtp_b);
        finish_reason = "stop";
        break;
      }
      const int output_remaining =
          max_tokens - static_cast<int>(generated.size());
      if ((sequential_mtp &&
           !MtpSequentialStepFits(seq.position, max_len_, max_prefill_, mtp_k_,
                                  output_remaining)) ||
          !MtpStepFits(seq.position, max_len_, max_prefill_, mtp_k_)) {
        // Keep the final allowed output unconsumed in strict mode. Both
        // verifiers continue from the valid pending correction at a tail.
        next_token = mtp_b;
        use_mtp = false;
        plain_tail = true;
        if (sequential_mtp)
          tail_reason = output_remaining <= mtp_k_ + 1 ? "output_limit"
                                                       : "context_limit";
        break;
      }
      // Register this step's input with the scheduler.
      mtp_ar.mtp_b = mtp_b;
      mtp_ar.mtp_d0 = mtp_d0;
      mtp_ar.mtp_output_remaining = output_remaining;
      trace::MtpCycleStep* cycle_step =
          cycle_timing ? cycle_timing->BeginStep(seq.position, mtp_k_)
                       : nullptr;
      trace::MtpVerifyMoeStep* verify_moe_step =
          verify_moe_timing
              ? verify_moe_timing->BeginStep(seq.position, mtp_k_)
              : nullptr;
      mtp_ar.mtp_cycle_step = cycle_step;
      mtp_ar.mtp_verify_moe_step = verify_moe_step;
      if (cycle_step) cycle_step->Mark("submit");
      if (init_timing && mtp_steps == 0)
        init_timing->MarkHost("first_step_submit");
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        mtp_ar.mtp_accepted_count = 0;
        mtp_ar.mtp_next_b = -1;
        mtp_ar.mtp_next_d0 = -1;
        mtp_ar.mtp_sequential_result = {};
        mtp_ar.pending = !scheduler_stop_;
        mtp_ar.done = scheduler_stop_;
        if (scheduler_stop_) seq.Fail();
      }
      sched_cv_.notify_one();
      // Block until the scheduler's batched step yields this step's output.
      if (init_timing && mtp_steps == 0)
        init_timing->MarkHost("first_step_wait_begin");
      {
        std::unique_lock<std::mutex> lock(sched_mu_);
        mtp_ar.cv.wait(lock, [&mtp_ar] { return mtp_ar.done; });
      }
      if (cycle_step) cycle_step->Mark("request_wake");
      if (init_timing && mtp_steps == 0)
        init_timing->MarkHost("first_step_wait_end");
      if (sequential_mtp) {
        const auto& result = mtp_ar.mtp_sequential_result;
        draft_forward_calls += result.draft_forward_calls;
        target_t1_calls += result.target_forward_calls;
        extend_forward_calls += result.extend_forward_calls;
        if (mtp_ar.mtp_accepted_count > 0 &&
            (mtp_ar.mtp_accepted_count > mtp_k_ + 1 ||
             result.terminal != is_stop_token(result.next_b) ||
             result.next_seed_valid == result.terminal ||
             result.next_b < 0 ||
             std::any_of(result.accepted_tokens.begin(),
                         result.accepted_tokens.begin() + result.accepted_count,
                         is_stop_token))) {
          generation_failed = true;
          break;
        }
      }
      if (mtp_ar.mtp_accepted_count <= 0) {
        // Scheduler failed the step or is shutting down.
        generation_failed = true;
        break;
      }
      ++mtp_steps;
      const size_t generated_before_step = generated.size();
      int token_piece_writes = 0;
      int nonempty_writes = 0;
      if (cycle_step) cycle_step->Mark("emit_begin");
      for (int i = 0; i < mtp_ar.mtp_accepted_count &&
                          static_cast<int>(generated.size()) < max_tokens;
           ++i) {
        const int32_t tok_id = mtp_ar.mtp_accepted[i];
        generated.push_back(tok_id);
        if (is_stop_token(tok_id)) {
          done = true;
          finish_reason = "stop";
          break;
        }
        if (stream) {
          std::vector<std::uint32_t> one(1, static_cast<std::uint32_t>(tok_id));
          std::string piece;
          bool wrote = true;
          {
            const std::lock_guard<std::mutex> lock(tok_mu_);
            if (tok_->Decode(one, true, &piece).ok()) {
              const bool first_content = init_timing && !piece.empty() &&
                                         !init_timing->FirstContentWritten();
              if (first_content)
                init_timing->MarkHost("first_content_write_begin");
              const bool first_cycle_content =
                  cycle_timing && !piece.empty() &&
                  !cycle_timing->FirstContentWritten();
              if (first_cycle_content)
                cycle_timing->MarkRequest("first_content_write_begin");
              wrote = write_stream(SseChunk(id, model_field, "", piece, "", 0));
              if (first_content && wrote) init_timing->FinishFirstContent();
              if (first_cycle_content && wrote)
                cycle_timing->FinishFirstContent();
              if (cycle_step && wrote) {
                ++token_piece_writes;
                if (!piece.empty()) ++nonempty_writes;
              }
            }
          }
          if (!wrote) {  // client disconnected: stop, free the slot
            done = true;
            finish_reason = "stop";
            break;
          }
        }
      }
      if (cycle_step) {
        cycle_step->Mark("emit_end");
        cycle_step->SetDelivery(
            static_cast<int>(generated.size() - generated_before_step),
            token_piece_writes, nonempty_writes);
      }
      if (verify_moe_step)
        verify_moe_step->SetDelivery(
            static_cast<int>(generated.size() - generated_before_step));
      // Advance the main seq over the accepted prefix [b, d_0..d_{a-1}] (the
      // multi step does not touch seqs[b]).
      seq.position += mtp_ar.mtp_accepted_count;
      for (int i = 0; i < mtp_ar.mtp_accepted_count; ++i)
        seq.history.push_back(mtp_ar.mtp_accepted[i]);
      if (cycle_step) cycle_step->Mark("advance_end");
      if (done) break;  // EOS wins over a coincident output/context limit.
      if (static_cast<int>(generated.size()) >= max_tokens) {
        finish_reason = "length";
        break;
      }
      if (seq.position >= max_len_) {
        finish_reason = "length";
        break;  // cannot decode further without exceeding the KV cache
      }
      mtp_b = mtp_ar.mtp_next_b;
      mtp_d0 = mtp_ar.mtp_next_d0;
      // d_mtp_g was overwritten in place with the next step's trunk.
    }
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
    if (sequential_mtp && !use_sched) {
      // Strict mode cannot substitute the numerically different standalone
      // ModelDecodeStepSeq implementation, even for a resource fallback.
      generation_failed = true;
    }
    for (int step = static_cast<int>(generated.size());
         !generation_failed && step < max_tokens;
         ++step) {
      if (cancelled()) break;
      generated.push_back(next_token);
      if (plain_tail) ++plain_tail_tokens;
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
          const std::lock_guard<std::mutex> lock(tok_mu_);
          if (tok_->Decode(one, true, &piece).ok())
            wrote = write_stream(SseChunk(id, model_field, "", piece, "", 0));
        }
        if (!wrote) {  // client disconnected: stop, free the slot
          finish_reason = "stop";
          break;
        }
      }
      // The current token is already emitted (or buffered for non-streaming).
      // Do not prepare another token when this request cannot consume it.
      if (step == max_tokens - 1 || seq.position + 1 >= max_len_) {
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
        const std::lock_guard<std::mutex> lock(model_mu_);
        if (router_trace_) router_trace_->BeginForward(
            trace::RouteStage::kDecode, seq.position, 1);
        s = model::ModelDecodeStepSeq(model_.Get(), &seq, tok_id, d_prefill_logits_,
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
      ++plain_steps;
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
  const char* decode_path = "plain";
  if (mtp_steps > 0) {
    decode_path = sequential_mtp
                      ? "mtp_sequential_b1"
                      : (max_seq_ == 1 ? "mtp_multi_b1" : "mtp_multi");
  } else if (plain_steps == 0 && prefill_only) {
    decode_path = "prefill_only";
  } else if (sequential_mtp && plain_tail) {
    decode_path = "plain_tail_b1";
  }
  if (sequential_mtp) {
    std::fprintf(stderr,
                 "[q4t][decode_path] id=%s requested_mtp=%d path=%s "
                 "mtp_steps=%d fallback=%s plain_tail_tokens=%d "
                 "verifier=sequential tail_reason=%s "
                 "draft_forward_calls=%llu target_t1_calls=%llu"
                 " target_t4_calls=0 extend_forward_calls=%llu "
                 "forward_count_scope=sequential_attempts\n",
                 id.c_str(), mtp_requested_, decode_path, mtp_steps,
                 prefill_only ? "none" : mtp_fallback, plain_tail_tokens,
                 tail_reason,
                 static_cast<unsigned long long>(draft_forward_calls),
                 static_cast<unsigned long long>(target_t1_calls),
                 static_cast<unsigned long long>(extend_forward_calls));
  } else {
    std::fprintf(stderr,
                 "[q4t][decode_path] id=%s requested_mtp=%d path=%s "
                 "mtp_steps=%d fallback=%s plain_tail_tokens=%d verifier=%s\n",
                 id.c_str(), mtp_requested_, decode_path, mtp_steps,
                 prefill_only ? "none" : mtp_fallback, plain_tail_tokens,
                 mtp_requested_ ? MtpVerifierName(mtp_verifier_) : "none");
  }
  if (batch_gather_enabled) {
    const auto end = quant::GetMoEBatchGatherStats();
    std::fprintf(
        stderr,
        "[q4t][moe_batch_gather] id=%s scope=%s applied_calls=%llu "
        "legacy_calls=%llu bad_counts_fallback=%llu batch_launches=%llu "
        "legacy_gather_launches=%llu replaced_gather_launches=%llu\n",
        id.c_str(), max_seq_ == 1 ? "single_sequence_window" : "process_window",
        static_cast<unsigned long long>(end.applied_calls -
                                        batch_gather_begin.applied_calls),
        static_cast<unsigned long long>(end.legacy_calls -
                                        batch_gather_begin.legacy_calls),
        static_cast<unsigned long long>(end.bad_counts_fallback -
                                        batch_gather_begin.bad_counts_fallback),
        static_cast<unsigned long long>(end.batch_launches -
                                        batch_gather_begin.batch_launches),
        static_cast<unsigned long long>(end.legacy_gather_launches -
                                        batch_gather_begin.legacy_gather_launches),
        static_cast<unsigned long long>(
            end.replaced_gather_launches -
            batch_gather_begin.replaced_gather_launches));
  }
  if (generation_failed) seq.Fail();
  model::ModelEndSequence(&seq);

  metrics_.prompt_tokens_total.fetch_add(static_cast<uint64_t>(T),
                                         std::memory_order_relaxed);
  metrics_.generation_tokens_total.fetch_add(generated.size(),
                                             std::memory_order_relaxed);
  metrics_.e2e_seconds.Observe(
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t_arrive)
          .count());

  // The handler has left every scheduler list and all submitted work is
  // complete. Linearize terminal completion before publishing success.
  if (cancelled() || !control->Finish()) {
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
  // 4. Finalize.
  if (stream) {
    write_stream(SseChunk(id, model_field, "", "", finish_reason, 0));
    if (include_usage && !client_disconnected) {
      const std::string usage_chunk =
          "data: {\"id\":\"" + id +
          "\",\"object\":\"chat.completion.chunk\",\"created\":" +
          std::to_string(created) + ",\"model\":\"" +
          JsonEscape(model_field) + "\",\"choices\":[],\"usage\":{" +
          "\"prompt_tokens\":" + std::to_string(T) +
          ",\"completion_tokens\":" + std::to_string(generated.size()) +
          ",\"total_tokens\":" + std::to_string(T + generated.size()) +
          "}}\r\n\r\n";
      write_stream(usage_chunk);
    }
    write_stream("data: [DONE]\r\n\r\n");
  } else {
    std::vector<std::uint32_t> gen_u32(generated.begin(), generated.end());
    std::string text;
    bool decoded = false;
    {
      const std::lock_guard<std::mutex> lock(tok_mu_);
      decoded = tok_->Decode(gen_u32, true, &text).ok();
    }
    if (decoded) {
      std::string resp =
          "{\"id\":\"" + id + "\",\"object\":\"chat.completion\","
          "\"created\":" +
          std::to_string(created) + ",\"model\":\"" + model_field +
          "\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\","
          "\"content\":\"" +
          JsonEscape(text) +
          "\"},\"finish_reason\":\"" + finish_reason + "\"}],"
          "\"usage\":{\"prompt_tokens\":" +
          std::to_string(T) +
          ",\"completion_tokens\":" + std::to_string(generated.size()) +
          ",\"total_tokens\":" + std::to_string(T + generated.size()) + "}";
      resp += "}";
      SendSimple(fd, 200, "OK", resp, "application/json");
    } else {
      err_guard.ok = false;
      SendError(fd, 500, "decode failed");
      cleanup();
      return;
    }
  }

  trace_outcome = client_disconnected ? trace::RequestOutcome::kCancelled
                                      : trace::RequestOutcome::kSuccess;
  if (!client_disconnected) {
    metrics_.requests_success.fetch_add(1, std::memory_order_relaxed);
  }
  cleanup();
  if (verify_moe_timing)
    verify_moe_timing->Finish(
        static_cast<int>(generated.size()), mtp_steps, plain_tail,
        finish_reason, mtp_fallback,
        !generation_failed && !client_disconnected);
  if (cycle_timing) {
    cycle_timing->MarkRequest("request_end");
    cycle_timing->Finish(static_cast<int>(generated.size()), mtp_steps,
                         plain_tail, finish_reason, mtp_fallback,
                         !generation_failed && !client_disconnected);
  }
}

}  // namespace q4t::server
