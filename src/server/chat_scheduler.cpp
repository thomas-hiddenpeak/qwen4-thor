// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"
#include "q4t/diagnostic/execution_observer.h"
#include "q4t/server/scheduler_submission.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <span>
#include <string>
#include <vector>
#include <cuda_profiler_api.h>
#include <cuda_runtime.h>

namespace q4t::server {
using detail::RequestCancelled;
using detail::FinishHostReadback;

namespace detail {
// A successful launch is not a completed result. Drain the existing stream
// boundary even if a copy failed, and never publish stale host output.
Status FinishHostReadback(
    cudaError_t copy_error, std::atomic<bool>* gpu_healthy,
    std::span<model::ModelSequence* const> sequences,
    const Status& submitted, trace::RouterCollector* trace) {
  diagnostic::Scope observation("host_readback.wait_and_commit");
  const cudaError_t trace_error = trace ? trace->Readback(nullptr) : cudaSuccess;
  bool gpu_complete = false;
  const Status result = model::CompleteSequenceWork(sequences, submitted, [&] {
    const cudaError_t sync_error = [&] {
      diagnostic::Scope wait("host_readback.stream_sync");
      return cudaStreamSynchronize(nullptr);
    }();
    gpu_complete = sync_error == cudaSuccess;
    if (copy_error == cudaSuccess) copy_error = trace_error;
    const cudaError_t error =
        copy_error != cudaSuccess ? copy_error : sync_error;
    if (error == cudaSuccess) return Status();
    gpu_healthy->store(false, std::memory_order_relaxed);
    return Status::Fail(std::string("GPU result readback: ") +
                        cudaGetErrorString(error));
  });
  if (trace) trace->Complete(submitted.ok(), gpu_complete, result.ok());
  return result;
}

}

int ChatServer::AllocSeqId(RequestControl* control, int fd, size_t required) {
  std::unique_lock<std::mutex> lock(seq_mu_);
  for (;;) {
    if (seq_stopping_) return -1;
    if (!gpu_healthy_.load(std::memory_order_relaxed)) return -3;
    if (RequestCancelled(control, fd)) return -2;
    const int slot = runtime::SelectSequenceSlot(seq_capacities_, max_len_,
                                                 seq_free_, required);
    if (slot >= 0) {
      seq_free_[slot] = false;
      return slot;
    }
    // Socket EOF/deadlines need no extra watcher thread. Explicit cancellation
    // wakes this wait; timeout bounds polling even if a notification races it.
    seq_cv_.wait_for(lock, std::chrono::milliseconds(100));
  }
}

void ChatServer::FreeSeqId(int seq_id) {
  if (seq_id < 0) return;
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    if (seq_id < max_seq_) seq_free_[static_cast<size_t>(seq_id)] = true;
  }
  seq_cv_.notify_all();  // waiters may need different slot capacities
}

void ChatServer::ReleaseActiveRequest(ActiveRequest* request) noexcept {
  if (std::uncaught_exceptions())
    request->control->Cancel(RequestControl::State::kFailure);
  std::unique_lock<std::mutex> lock(sched_mu_);
  sched_cv_.notify_one();
  request->cv.wait(lock, [&] { return !request->pending; });
  active_.erase(std::remove(active_.begin(), active_.end(), request),
                 active_.end());
  request->retired = true;
  lock.unlock();
  sched_cv_.notify_one();
}

void ChatServer::AbortBackendExecution() noexcept {
  gpu_healthy_.store(false, std::memory_order_relaxed);
  serving_failed_.store(true);
  RequestStop();
  const auto drained = cudaStreamSynchronize(nullptr);
  if (drained != cudaSuccess) {
    // No proof that DMA/kernel references are gone: do not unwind/reuse the
    // remaining request or model storage. Process teardown owns reclamation.
    std::fprintf(stderr, "[q4t] exception drain failed: %s; exiting\n",
                 cudaGetErrorString(drained));
    std::_Exit(1);
  }
}

void ChatServer::AbortScheduler(std::span<PrefillReq* const> selected,
                               ChunkPrefillReq* chunk) noexcept {
  serving_failed_.store(true);
  gpu_healthy_.store(false, std::memory_order_relaxed);
  RequestStop();
  const std::lock_guard<std::mutex> lock(sched_mu_);
  scheduler_stop_ = true;
  for (auto* r : active_) {
    if (r->pending) {
      r->seq->FailAfterDrain();
      r->pending = false;
      r->done = true;
      r->next_token = -1;
      r->mtp_accepted_count = 0;
      r->mtp_next_b = r->mtp_next_d0 = -1;
    }
    r->cv.notify_one();
  }
  const auto fail_prefill = [](PrefillReq* p) {
    p->seq->FailAfterDrain();
    p->pending = false;
    p->done = true;
    p->ok = false;
    p->cv.notify_one();
  };
  for (auto* p : selected) fail_prefill(p);
  for (auto* p : prefill_pending_) fail_prefill(p);
  prefill_pending_.clear();
  const auto fail_chunk = [](ChunkPrefillReq* p) {
    p->seq->FailAfterDrain();
    p->ok = false;
    p->done = true;
    p->cv.notify_one();
  };
  if (chunk) fail_chunk(chunk);
  for (auto* p : chunk_prefill_pending_) fail_chunk(p);
  chunk_prefill_pending_.clear();
  sched_cv_.notify_all();
}

// B2b continuous batching: the central scheduler loop.
//
// Each ACTIVE plain-decode request registers its current decode token (its
// `pending` flag is set under sched_mu_ and the scheduler is signalled). The
// scheduler collects ALL pending requests, runs them in ONE packed
// ModelDecodeBatchMulti (the 84 GB of weights is read once for all B tokens
// instead of B times — the decode throughput win), copies the [B, vocab]
// logits back to the host, argmaxes each row, and wakes each request with its
// next token. The packed forward runs under model_mu_ (the shared per-forward
// scratch); the D2H + argmax run under model_mu_ too (they must be ordered
// after the forward on the default stream and before the next forward's H2D).
//
// MTP requests use the separate batched speculative path below, including B=1.
void ChatServer::RunOnePrefillChunk() {
  ChunkPrefillReq* req = nullptr;
  OnException failed_chunk([&] { AbortScheduler({}, req); });
  {
    const std::lock_guard<std::mutex> lock(sched_mu_);
    if (scheduler_stop_ || chunk_prefill_pending_.empty()) return;
    req = chunk_prefill_pending_.front();
    chunk_prefill_pending_.erase(chunk_prefill_pending_.begin());
  }
  Status s;
  bool last = false;
  {
    const std::lock_guard<std::mutex> lock(model_mu_);
    OnException backend_failure([&] { AbortBackendExecution(); });
    if (!gpu_healthy_.load(std::memory_order_relaxed)) {
      s = Status::Fail("chunk prefill: GPU unhealthy");
    } else if (RequestCancelled(req->control, req->fd)) {
      req->cancelled = true;
    } else {
      if (req->seq->stage == model::ModelSequence::Stage::kIdle) {
        s = model::ModelBeginSequence(model_.Get(), req->seq, nullptr, req->seq_id);
      }
      const int base = req->seq->position;
      const int count = std::min(max_prefill_, req->len - base);
      last = base + count == req->len;
      if (s.ok()) {
        if (router_trace_) router_trace_->BeginForward(
            trace::RouteStage::kPrefill, base, count);
        s = model::ModelPrefillTextChunk(
            model_.Get(), req->seq, req->ids, req->len, count,
            last ? d_prefill_logits_ : nullptr, nullptr, nullptr,
            model::LogitsRows::kLastRow, model::SequenceCompletion::kDeferred);
      }
      cudaError_t copy_error = cudaSuccess;
      if (s.ok() && last) {
        copy_error = cudaMemcpyAsync(
            req->h_logits,
            d_prefill_logits_,
            static_cast<size_t>(model_.Get().cfg.vocab) * 2,
            cudaMemcpyDeviceToHost, nullptr);
      }
      // A host cursor is not device completion. Drain even a failed forward
      // before handing shared scratch to another request.
      s = FinishHostReadback(copy_error, &gpu_healthy_, {&req->seq, 1}, s, router_trace_.get());
      if (s.ok() && RequestCancelled(req->control, req->fd)) {
        req->cancelled = true;
      }
      if (!s.ok() && cudaPeekAtLastError() != cudaSuccess) {
        gpu_healthy_.store(false, std::memory_order_relaxed);
      }
    }
  }
  {
    const std::lock_guard<std::mutex> lock(sched_mu_);
    if (s.ok() && !req->cancelled && !last && !scheduler_stop_) {
      chunk_prefill_pending_.push_back(req);
    } else {
      req->ok = s.ok() && !req->cancelled && last && !scheduler_stop_;
      if (!s.ok()) req->err = s.message();
      if (!req->ok) req->seq->Fail();
      req->done = true;
      req->cv.notify_one();
    }
    req = nullptr;  // Completion/requeue transferred ownership; no stale borrow.
  }
}

void ChatServer::SchedulerLoop() {
  const int vocab = model_.Get().cfg.vocab;
  // One shutdown summary per observed (batch rows, suffix rows), not a log
  // per token. Entries are bounded by the configured active-slot count.
  std::map<std::pair<int, int>, uint64_t> mtp_batches;
  for (;;) {
    std::vector<ActiveRequest*> pending;
    std::vector<PrefillReq*> pf;
    OnException failed_batch([&] { AbortScheduler(pf); });
    {
      std::unique_lock<std::mutex> lock(sched_mu_);
      const auto ready = [this] {
        if (scheduler_stop_) return true;
        RetireCancelledSchedulerRequests(
            active_, [](ActiveRequest* r) {
              return RequestCancelled(r->control, r->fd);
            }, [](ActiveRequest* r) {
              r->seq->Fail();
              r->pending = false;
              r->done = true;
              r->next_token = -1;
              r->mtp_accepted_count = 0;
              r->mtp_next_b = -1;
              r->mtp_next_d0 = -1;
              r->cv.notify_one();
            });
        // Batched prefill runs opportunistically (prefills arrive as encoding
        // completes, not in lockstep), so ANY pending prefill wakes the loop.
        if (!prefill_pending_.empty() || !chunk_prefill_pending_.empty())
          return true;
        // Lockstep (both plain B2b and MTP plan A): run only when EVERY active
        // request of a kind has registered its step, so the packed forward is
        // B = all active requests (uniform width, like vllm/sglang) instead of
        // a ragged subset. Opportunistic "any pending" fragmented the decode
        // into many small sub-steps, each re-reading the 84 GB of weights — the
        // decode throughput killer. A straggler gates the step; a finishing
        // request removes itself from active_ + notifies so this re-evaluates.
        int active_plain = 0, pending_plain = 0;
        int active_mtp = 0, pending_mtp = 0;
        for (ActiveRequest* r : active_) {
          if (r->is_mtp) {
            ++active_mtp;
            if (r->pending) ++pending_mtp;
          } else {
            ++active_plain;
            if (r->pending) ++pending_plain;
          }
        }
        if (active_plain > 0 && pending_plain == active_plain) return true;
        return active_mtp > 0 && pending_mtp == active_mtp;
      };
      while (!ready()) {
        // A deadline or disconnect need not have a notifying API call.
        // Timeout only rechecks cancellation; it never runs a partial batch.
        if (active_.empty()) sched_cv_.wait(lock);
        else sched_cv_.wait_for(lock, std::chrono::milliseconds(25));
      }
      if (scheduler_stop_) {
        // Drain: wake any request that is still waiting so it can exit.
        for (ActiveRequest* r : active_) {
          // Completed results belong to their request thread. Do not race
          // its readback or erase completed forward-attempt diagnostics.
          if (r->pending) {
            if (r->seq) r->seq->Fail();
            r->pending = false;
            r->done = true;
            r->next_token = -1;
            r->mtp_accepted_count = 0;
            r->mtp_next_b = -1;
            r->mtp_next_d0 = -1;
          }
          r->cv.notify_one();
        }
        for (PrefillReq* p : prefill_pending_) {
          p->seq->Fail();
          p->pending = false;
          p->done = true;
          p->ok = false;  // sentinel: scheduler is shutting down
          p->cv.notify_one();
        }
        prefill_pending_.clear();
        for (ChunkPrefillReq* p : chunk_prefill_pending_) {
          p->seq->Fail();
          p->ok = false;
          p->done = true;
          p->cv.notify_one();
        }
        chunk_prefill_pending_.clear();
        for (const auto& [shape, count] : mtp_batches)
          std::fprintf(stderr, "[mtp-batches] rows=%d suffix=%d steps=%llu\n",
                       shape.first, shape.second,
                       static_cast<unsigned long long>(count));
        return;
      }
      // Collect pending prefills into one batch (up to max_seq sequences /
      // max_prefill packed tokens). Opportunistic: whatever is queued now.
      int ttot = 0;
      while (!prefill_pending_.empty() &&
             static_cast<int>(pf.size()) < max_seq_) {
        PrefillReq* p = prefill_pending_.front();
        if (RequestCancelled(p->control, p->fd)) {
          p->seq->Fail();
          prefill_pending_.erase(prefill_pending_.begin());
          p->pending = false;
          p->done = true;
          p->ok = false;
          p->cv.notify_one();
          continue;
        }
        if (ttot + p->len > max_prefill_) break;  // token budget
        ttot += p->len;
        pf.push_back(p);
        prefill_pending_.erase(prefill_pending_.begin());
      }
      for (ActiveRequest* r : active_)
        if (r->pending) pending.push_back(r);
    }
    detail::ServeFaultPoint("scheduler.after_select");

    // Batched prefill: pack pf into ONE ModelPrefillBatch (the dense weights
    // are read once for the whole batch instead of per request), then hand each
    // request its last-token logits. Runs under model_mu_ (shared per-forward
    // scratch), like the decode step below. A LONE prefill (Bp == 1) uses the
    // single-seq ModelPrefill instead, selecting only its last head row.
    // The multi-seq full-attention indexer rounds differently from the
    // single-seq tensor-core GEMM, which can flip a near-tie token — harmless
    // but a visible change; only actual batching (Bp > 1) accepts it.
    if (!pf.empty()) {
      const int Bp = static_cast<int>(pf.size());
      Status sp;
      std::vector<model::ModelSequence*> sequences;
      for (auto* request : pf) sequences.push_back(request->seq);
      {
        const std::lock_guard<std::mutex> lock(model_mu_);
        OnException backend_failure([&] { AbortBackendExecution(); });
        cudaError_t copy_error = cudaSuccess;
        // Health may change while this group waits for model_mu_.
        if (!gpu_healthy_.load(std::memory_order_relaxed))
          sp = Status::Fail("GPU unhealthy; restart server");
        if (sp.ok() && Bp == 1) {
          sp = model::ModelBeginSequence(model_.Get(), pf[0]->seq, nullptr,
                                          pf[0]->seq_id);
          if (sp.ok() && router_trace_) router_trace_->BeginForward(
              trace::RouteStage::kPrefill, 0, pf[0]->len);
          if (sp.ok())
            sp = model::ModelPrefill(model_.Get(), pf[0]->seq, pf[0]->ids, pf[0]->len,
                                     d_prefill_logits_, nullptr, nullptr,
                                     nullptr, pf[0]->seq_id,
                                     model::LogitsRows::kLastRow,
                                     model::SequenceCompletion::kDeferred);
          if (sp.ok()) {
            copy_error = cudaMemcpyAsync(
                pf[0]->h_logits,
                d_prefill_logits_,
                static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost, nullptr);
          }
        } else if (sp.ok()) {
          std::vector<int32_t> pk_tokens;
          std::vector<int> pk_lens(Bp), pk_seq(Bp);
          for (int i = 0; i < Bp; ++i) {
            pk_lens[i] = pf[i]->len;
            pk_seq[i] = pf[i]->seq_id;
            if (sp.ok()) sp = pf[i]->seq->Begin(pf[i]->seq_id);
            if (sp.ok())
              sp = pf[i]->seq->Submit(
                  {pf[i]->ids, static_cast<size_t>(pf[i]->len)},
                  model::ModelSequence::Stage::kDecode, model_.Get().cfg.max_len);
            pk_tokens.insert(pk_tokens.end(), pf[i]->ids,
                             pf[i]->ids + pf[i]->len);
          }
          if (sp.ok()) sp = model::ModelPrefillBatch(model_.Get(), pk_tokens.data(),
                                        pk_lens.data(), pk_seq.data(), Bp,
                                        d_prefill_logits_, nullptr,
                                        model::PrefillBatchLogitsRows::
                                            kSequenceLastRows);
          if (!sp.ok() && cudaPeekAtLastError() != cudaSuccess)
            gpu_healthy_.store(false, std::memory_order_relaxed);
          if (sp.ok()) {
            // Head output row i belongs to packed sequence i.
            for (int i = 0; i < Bp; ++i) {
              const cudaError_t error = cudaMemcpyAsync(
                  pf[i]->h_logits,
                  d_prefill_logits_ + static_cast<size_t>(i) * vocab,
                  static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost,
                  nullptr);
              if (copy_error == cudaSuccess) copy_error = error;
            }
          }
        }
        // Even a failed forward may have queued GPU writes. Drain before
        // publishing completion so cancellation/cleanup cannot reuse buffers
        // still owned by that work. Device failure takes precedence over abort.
        sp = FinishHostReadback(copy_error, &gpu_healthy_, sequences, sp, router_trace_.get());
      }
      {
        if (!sp.ok()) {
          std::fprintf(stderr,
                       "[q4t][sched] batched prefill failed Bp=%d: %s\n",
                       Bp, sp.message().c_str());
        }
        const std::lock_guard<std::mutex> lock(sched_mu_);
        for (int i = 0; i < Bp; ++i) {
          pf[i]->pending = false;
          pf[i]->done = true;
          pf[i]->ok = sp.ok();
          pf[i]->cv.notify_one();
        }
        pf.clear();  // Handlers may destroy these requests once the lock opens.
      }
    }
    if (pending.empty()) {
      RunOnePrefillChunk();
      continue;
    }

    // Split pending into MTP (speculative) and plain (single-token) requests.
    // MTP requests are batched into ONE MtpSpeculativeStepMulti (Stage 2c:
    // batched draft loop + ModelVerifyMulti + batched extend, weights read
    // once); plain requests into ONE ModelDecodeBatchMulti (B2b). The two
    // groups run as separate forwards (both under model_mu_, sequential).
    std::vector<ActiveRequest*> mtp_reqs, plain_reqs;
    for (ActiveRequest* r : pending)
      (r->is_mtp ? mtp_reqs : plain_reqs).push_back(r);

    if (!mtp_reqs.empty()) {
      const int B = static_cast<int>(mtp_reqs.size());
      if (getenv("Q4T_SCHED_DEBUG") != nullptr)
        std::fprintf(stderr, "[q4t][sched] MTP step B=%d\n", B);
      std::vector<model::ModelSequence*> seqs(B);
      std::vector<int32_t> b_tok(B), d0(B);
      std::vector<const uint16_t*> g_in(B);
      std::vector<uint16_t*> next_g(B);
      std::vector<mtp::SuffixProposal> suffix;
      std::vector<uint8_t> complete_terminal;
      if (session_cache_.Enabled()) complete_terminal.resize(B);
      if (mtp_suffix_) suffix.resize(B);
      int suffix_rows = 0;
      for (int i = 0; i < B; ++i) {
        seqs[i] = mtp_reqs[i]->seq;
        b_tok[i] = mtp_reqs[i]->mtp_b;
        d0[i] = mtp_reqs[i]->mtp_d0;
        g_in[i] = mtp_reqs[i]->mtp_g;
        next_g[i] = mtp_reqs[i]->mtp_g;  // trunk rolled in place
        if (!complete_terminal.empty())
          complete_terminal[i] = mtp_reqs[i]->complete_terminal_draft;
        if (mtp_suffix_) {
          suffix[i].ready = mtp_reqs[i]->mtp_suffix_ready;
          suffix[i].tokens = mtp_reqs[i]->mtp_suffix_tokens;
          suffix_rows += suffix[i].ready;
        }
      }
      std::vector<int32_t> accepted(static_cast<size_t>(B) * (mtp_k_ + 1));
      std::vector<int> acc_count(B, 0);
      std::vector<int32_t> next_b(B, 0), next_d0(B, 0);
      Status s;
      {
        const std::lock_guard<std::mutex> lock(model_mu_);
        OnException backend_failure([&] { AbortBackendExecution(); });
        s = !gpu_healthy_.load(std::memory_order_relaxed)
                ? Status::Fail("GPU unhealthy; restart server")
                : mtp::MtpSpeculativeStepMulti(model_.Get(), mtp_, seqs.data(),
                                         b_tok.data(), d0.data(), g_in.data(),
                                         B, mtp_k_, accepted.data(),
                                         acc_count.data(), next_b.data(),
                                         next_d0.data(), next_g.data(),
                                         nullptr, stop_token_ids_, {}, suffix,
                                         complete_terminal);
        if (s.ok()) {
          for (int i = 0; i < B; ++i) {
            if (!mtp_reqs[i]->session_last_trunk) continue;
            const auto error = cudaMemcpy(
                mtp_reqs[i]->session_last_trunk,
                mtp_.d_ms_vtrunk +
                    (static_cast<size_t>(i) * (mtp_k_ + 1) + acc_count[i] - 1) *
                        model_.Get().hc_dim(),
                static_cast<size_t>(model_.Get().hc_dim()) * 2,
                cudaMemcpyDeviceToDevice);
            if (error != cudaSuccess) {
              s = Status::Fail(std::string("session final trunk: ") +
                               cudaGetErrorString(error));
              gpu_healthy_.store(false, std::memory_order_relaxed);
              break;
            }
          }
        }
        if (!s.ok())
          for (auto* sequence : seqs) sequence->Fail();
        if (!s.ok() && cudaPeekAtLastError() != cudaSuccess)
          gpu_healthy_.store(false, std::memory_order_relaxed);
      }
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        if (s.ok()) ++mtp_batches[{B, suffix_rows}];
        for (int i = 0; i < B; ++i) {
          ActiveRequest* r = mtp_reqs[i];
          r->pending = false;
          r->done = true;
          if (s.ok()) {
            r->mtp_accepted_count = acc_count[i];
            for (int t = 0; t < acc_count[i]; ++t)
              r->mtp_accepted[t] =
                  accepted[static_cast<size_t>(i) * (mtp_k_ + 1) + t];
            r->mtp_next_b = next_b[i];
            r->mtp_next_d0 = next_d0[i];
          } else {
            r->mtp_accepted_count = 0;  // sentinel: step failed
          }
          r->cv.notify_one();
        }
      }
    }

    if (plain_reqs.empty()) {
      RunOnePrefillChunk();
      continue;
    }
    const int B = static_cast<int>(plain_reqs.size());
    std::vector<int32_t> tokens(B);
    std::vector<int> positions(B), seq_ids(B);
    std::vector<int32_t> hist_flat(static_cast<size_t>(B) *
                                   static_cast<size_t>(
                                       model_.Get().ple_hash.ngram_size - 1));
    for (int i = 0; i < B; ++i) {
      tokens[i] = plain_reqs[i]->token;
      const auto& seq = *plain_reqs[i]->seq;
      positions[i] = seq.position;
      seq_ids[i] = seq.seq_id;
      const int width = model_.Get().ple_hash.ngram_size - 1;
      for (int j = 0; j < width; ++j) {
        const int src = seq.position - (width - j);
        hist_flat[static_cast<size_t>(i) * width + j] =
            src >= 0 ? seq.history[src] : model_.Get().cfg.eos_token_id;
      }
    }

    // Q4T_PROFILE_DECODE=1: bracket each decode step with the CUDA profiler
    // API so `nsys --capture-range=cudaProfilerApi` captures ONLY decode
    // (isolating the O(context) QSA indexer cost from prefill).
    static const bool kProfileDecode =
        std::getenv("Q4T_PROFILE_DECODE") != nullptr;
    if (kProfileDecode) cudaProfilerStart();
    Status s;
    std::vector<model::ModelSequence*> sequences;
    for (auto* request : plain_reqs) sequences.push_back(request->seq);
    {
      const std::lock_guard<std::mutex> lock(model_mu_);
      OnException backend_failure([&] { AbortBackendExecution(); });
      if (!gpu_healthy_.load(std::memory_order_relaxed))
        s = Status::Fail("GPU unhealthy; restart server");
      for (int i = 0; i < B && s.ok(); ++i)
        s = sequences[i]->Submit({&tokens[i], 1},
                                  model::ModelSequence::Stage::kDecode,
                                  model_.Get().cfg.max_len);
      if (s.ok() && router_trace_) router_trace_->BeginForward(
          trace::RouteStage::kDecode, positions[0], B);
      if (s.ok())
        s = model::ModelDecodeBatchMulti(model_.Get(), tokens.data(), positions.data(),
                                         seq_ids.data(), hist_flat.data(), B,
                                         d_sched_logits_, nullptr, nullptr);
      if (!s.ok() && cudaPeekAtLastError() != cudaSuccess)
        gpu_healthy_.store(false, std::memory_order_relaxed);
      cudaError_t copy_error = cudaSuccess;
      if (s.ok()) {
        s = model::ArgmaxBf16Rows(d_sched_logits_, B, vocab, d_sched_tokens_,
                                  nullptr);
        if (s.ok())
          copy_error = cudaMemcpyAsync(
              h_sched_tokens_.data(), d_sched_tokens_,
              static_cast<size_t>(B) * sizeof(int32_t),
              cudaMemcpyDeviceToHost, nullptr);
      }
      // Includes launch/argmax failures: scratch cannot escape before drain.
      detail::ServeFaultPoint("scheduler.after_submit");
      s = FinishHostReadback(copy_error, &gpu_healthy_, sequences, s, router_trace_.get());
    }
    if (kProfileDecode) cudaProfilerStop();

    // State is committed (or failed) before notifying the request thread.
    {
      const std::lock_guard<std::mutex> lock(sched_mu_);
      for (int i = 0; i < B; ++i) {
        ActiveRequest* r = plain_reqs[i];
        r->pending = false;
        r->done = true;
        r->next_token = s.ok() ? h_sched_tokens_[i] : -1;
        r->cv.notify_one();
      }
    }
    RunOnePrefillChunk();
  }
}

}  // namespace q4t::server
