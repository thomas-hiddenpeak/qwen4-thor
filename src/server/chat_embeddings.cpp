#include "chat_server_internal.h"

#include <algorithm>
#include <new>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/server/embeddings_execution.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::RunEmbeddingInput(
    int fd, const std::vector<int32_t>& tokens,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, EmbeddingStepResult* result) {
  using object_detail::Error;
  using Wait = RequestDiagnostics::Wait;
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Embedding execution cancelled");
  };
  *result = {};
  diagnostic.Stage("slot_admission");
  int slot = -1;
  {
    RequestDiagnostics::Scope waiting(diagnostic, Wait::kSlot);
    slot = AllocSeqId(control.get(), fd, tokens.size());
  }
  if (slot == -2) return interrupted();
  if (slot == -5 || slot == -6) {
    RecordMemoryRejection(
        fd, slot == -5 ? MemoryState::kLow : MemoryState::kUnavailable,
        MemoryCheckpoint::kExecute);
    return Error(
        503, slot == -5 ? "memory_pressure" : "memory_observation_unavailable",
        "Embedding memory admission unavailable");
  }
  if (slot < 0)
    return Error(503,
                 slot == -4   ? "queue_capacity_exhausted"
                 : slot == -3 ? "gpu_unhealthy"
                              : "server_stopping",
                 "Embedding sequence admission unavailable");
  diagnostic.Admitted(slot);
  ScopeExit release_slot([&] {
    FreeSeqId(slot);
    diagnostic.Released();
  });
  uint16_t* trunk = nullptr;
  uint16_t* hidden = nullptr;
  bool buffers_ok = true;
  const auto release = [&](auto*& pointer, const char* site) {
    const bool allocated = pointer != nullptr;
    const std::lock_guard lock(model_mu_);
    RequestDiagnostics::ExecutionScope work(diagnostic,
                                            ProgressStage::kCleanup);
    const auto memory =
        detail::ReleaseCudaMemory(pointer, site, control->id().c_str());
    RecordDeviceMemoryFailure(memory, site);
    buffers_ok = buffers_ok && memory.ok();
    if (!memory.ok()) AbortBackendExecution();
    if (allocated && memory.ok()) work.Completed();
  };
  ScopeExit release_buffers([&] {
    release(hidden, "embedding.hidden_free");
    release(trunk, "embedding.trunk_free");
  });
  const auto allocate = [&](auto*& pointer, size_t bytes, const char* site) {
    RequestDiagnostics::ExecutionScope work(diagnostic, ProgressStage::kCache);
    const auto memory =
        detail::AllocateCudaMemory(pointer, bytes, site, control->id().c_str());
    RecordDeviceMemoryFailure(memory, site);
    if (memory.state == DeviceMemoryState::kFailure) AbortBackendExecution();
    if (memory.ok()) work.Completed();
    return memory;
  };
  const auto allocation_error = [&](const DeviceMemoryResult& memory) {
    return Error(503, memory.exhausted() ? memory.Reason() : "gpu_unhealthy",
                 "Embedding device allocation unavailable");
  };
  const auto& model = model_.Get();
  const int length = static_cast<int>(tokens.size());
  const int chunk = std::min(max_prefill_, length);
  result->hidden.resize(model.cfg.hs);
  model::ModelSequence sequence;
  bool completed = false;
  if (router_trace_) router_trace_->BeginRequest(tokens, control->id());
  ScopeExit trace([&] {
    metrics_.prompt_tokens_total.fetch_add(result->committed_input,
                                           std::memory_order_relaxed);
    if (router_trace_)
      router_trace_->EndRequest(control->Cancelled()
                                    ? trace::RequestOutcome::kCancelled
                                : completed ? trace::RequestOutcome::kSuccess
                                            : trace::RequestOutcome::kFailed,
                                0);
  });
  Status status;
  {
    RequestDiagnostics::Scope waiting(diagnostic, Wait::kModel);
    auto lock = LockUnlessCancelled(model_mu_, cancelled);
    if (!lock.owns_lock() || cancelled()) return interrupted();
    status = ExecutionStatus();
    if (!status) return Error(503, "gpu_unhealthy", status.message());
    auto memory =
        allocate(trunk, static_cast<size_t>(chunk) * model.hc_dim() * 2,
                 "embedding.trunk");
    if (!memory.ok()) return allocation_error(memory);
    memory = allocate(hidden, static_cast<size_t>(model.cfg.hs) * 2,
                      "embedding.hidden");
    if (!memory.ok()) return allocation_error(memory);
    RequestDiagnostics::ExecutionScope work(diagnostic,
                                            ProgressStage::kPrefill);
    OnException failure([&] { AbortBackendExecution(); });
    status = model::ModelBeginSequence(model, &sequence, nullptr, slot);
    if (status) work.Completed();
  }
  diagnostic.Stage("prefill");
  while (status && sequence.position < length) {
    if (cancelled()) return interrupted();
    std::unique_lock<std::mutex> lock;
    {
      RequestDiagnostics::Scope waiting(diagnostic, Wait::kModel);
      lock = LockUnlessCancelled(model_mu_, cancelled);
    }
    if (!lock.owns_lock() || cancelled()) return interrupted();
    ProgressExecution work(ProgressWork::kPrefill);
    OnException failure([&] { AbortBackendExecution(); });
    status = ExecutionStatus();
    if (!status) break;
    const int base = sequence.position;
    const int count = std::min(chunk, length - base);
    const bool last = base + count == length;
    if (router_trace_)
      router_trace_->BeginForward(trace::RouteStage::kPrefill, base, count);
    status = model::ModelPrefillTextChunk(
        model, &sequence, tokens.data(), length, count, nullptr, nullptr,
        last ? trunk : nullptr, model::LogitsRows::kLastRow,
        model::SequenceCompletion::kDeferred);
    if (status && last)
      status = model::HeadHidden(
          model.head, trunk + static_cast<size_t>(count - 1) * model.hc_dim(),
          hidden, 1, model.d_ws, model.ws_bytes, nullptr);
    cudaError_t copied = cudaSuccess;
    if (status && last)
      copied = cudaMemcpyAsync(result->hidden.data(), hidden,
                               static_cast<size_t>(model.cfg.hs) * 2,
                               cudaMemcpyDeviceToHost, nullptr);
    model::ModelSequence* pending = &sequence;
    status = detail::FinishHostReadback(copied, &gpu_healthy_, {&pending, 1},
                                        status, router_trace_.get(),
                                        &metrics_.execution_steps_total);
    // Sequence commits only after actual synchronized success. Failed final
    // work cannot turn its reserved token count into observed usage.
    result->committed_input = sequence.position;
  }
  if (!status) {
    gpu_healthy_.store(false, std::memory_order_relaxed);
    return Error(503, "gpu_unhealthy", status.message());
  }
  release_buffers.Run();
  if (!buffers_ok)
    return Error(503, "gpu_unhealthy", "Embedding buffer cleanup failed");
  completed = !cancelled();
  return completed ? ObjectReply{} : interrupted();
}

void ChatServer::HandleEmbeddings(int fd, const std::string& body,
                                  std::shared_ptr<RequestControl> owner) {
  auto* http = detail::HttpResponseContext::Current(fd);
  metrics_.requests_total.fetch_add(1, std::memory_order_relaxed);
  const auto started = std::chrono::steady_clock::now();
  bool success = false;
  auto control = std::move(owner);
  const bool borrowed = static_cast<bool>(control);
  ScopeExit finish([&] {
    if (success) {
      metrics_.requests_success.fetch_add(1, std::memory_order_relaxed);
      metrics_.e2e_seconds.Observe(
          std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                        started)
              .count());
    } else if (control && control->Cancelled() && !control->Failed()) {
      metrics_.requests_aborted.fetch_add(1, std::memory_order_relaxed);
      metrics_.abort_reasons[AbortReasonIndex(control->Reason())].fetch_add(
          1, std::memory_order_relaxed);
    } else {
      metrics_.requests_error.fetch_add(1, std::memory_order_relaxed);
    }
    if (control && !borrowed) requests_.Remove(control);
    if (http) http->RefreshUsage();
  });
  try {
    if (!http || !http->Client().Allowed()) {
      detail::SendError(fd, 503, "Authenticated model owner unavailable", {},
                        "object_scope_unavailable");
      return;
    }
    http->Usage().Enable();
    if (RejectUnhealthy(fd) ||
        !CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare))
      return;
    RequestAdmissionLease admitted(active_chats_, admission_limits_.inflight);
    if (!admitted.TryAcquire()) {
      metrics_.text_admission_rejected.fetch_add(1, std::memory_order_relaxed);
      detail::SendError(fd, 503, "Request capacity exhausted", {},
                        "capacity_exhausted");
      return;
    }
    io::Json request;
    auto status =
        io::ParseJson(body, &request, true, kEmbeddingInputJsonLimits);
    if (!status) {
      detail::SendError(fd, 400, "Invalid embedding JSON", {}, "invalid_json");
      return;
    }
    const auto config = PlanningConfig(fd);
    EmbeddingsRequestPlan plan;
    ChatPlanError error;
    status = PlanEmbeddingsRequest(request, config, &plan, &error);
    if (!status) {
      detail::SendError(fd, error.http_status, status.message(), {}, error.code,
                        error.param);
      return;
    }
    const auto id = control ? control->id() : detail::NewRequestId("embd_");
    if (!control)
      control = requests_.Register(
          id, "", started + std::chrono::milliseconds(request_deadline_ms_),
          http->Client().Id());
    if (!control) {
      detail::SendError(fd, 409, "Request identity already active", {},
                        "request_id_conflict");
      return;
    }
    OnException request_failure(
        [&] { control->Cancel(RequestControl::State::kFailure); });
    auto progress =
        progress_.Acquire(std::chrono::duration_cast<std::chrono::nanoseconds>(
                              control->deadline().time_since_epoch())
                              .count());
    control->BindProgress(progress.Handle());
    RequestDiagnostics diagnostic(stderr, http->TraceId());
    diagnostic.BindProgress(progress.Handle());
    diagnostic.Bind(id);
    ProgressContext progress_context(progress.Handle());
    const auto cancelled = [&] {
      if (stop_requested_.load())
        control->Cancel(RequestControl::State::kShutdown);
      return detail::RequestCancelled(control.get(), fd);
    };
    std::vector<std::vector<int32_t>> inputs;
    uint64_t total = 0;
    status = PrepareEmbeddingInputs(
        plan, config,
        [&](std::string_view text, std::vector<uint32_t>* tokens) {
          RequestDiagnostics::Scope waiting(
              diagnostic, RequestDiagnostics::Wait::kTokenizer);
          auto lock = LockUnlessCancelled(tok_mu_, cancelled);
          if (!lock.owns_lock())
            return Status::Fail("Embedding tokenization cancelled");
          return tok_->Encode(text, tokens, {.cancelled = cancelled});
        },
        &inputs, &total, &error);
    if (cancelled()) {
      detail::SendError(fd, 409, "Embedding request cancelled", {},
                        control->Reason());
      return;
    }
    if (!status) {
      detail::SendError(fd, error.http_status, status.message(), {}, error.code,
                        error.param);
      return;
    }
    ClientRateLease own_rate(http->RateDomain());
    const auto batch_rate = http->BatchRate();
    if (batch_rate && (http->RateDomain() != ClientRateDomain::kBatch ||
                       !batch_rate->PrechargedFor(http->Client()))) {
      detail::SendError(fd, 500, "Native Batch rate ownership changed", {},
                        "batch_execution_rate_invalid");
      return;
    }
    auto& rate = batch_rate ? *batch_rate : own_rate;
    std::string response;
    auto reply = ExecuteEmbeddingInputs(
        plan, inputs, clients_, http->Client(), rate, http->Usage(),
        [&](const auto& input, auto* result) {
          return RunEmbeddingInput(fd, input, control, diagnostic, result);
        },
        cancelled, &response);
    if (!reply.ok()) {
      detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                        reply.ClientCode());
      return;
    }
    if (cancelled() || !control->Finish()) {
      detail::SendError(fd, 409, "Embedding request cancelled", {},
                        control->Reason());
      return;
    }
    diagnostic.Stage("response");
    success = detail::SendSimple(fd, 200, "OK", response, "application/json",
                                 {control->deadline()});
    diagnostic.Outcome(success ? "success" : "error",
                       success ? "complete" : "response_write_failed");
  } catch (const std::bad_alloc&) {
    detail::SendExceptionResponse(
        fd, http ? *http->State() : detail::ResponseState::kUnsent,
        detail::ExceptionKind::kHostMemory);
  } catch (...) {
    detail::SendExceptionResponse(
        fd, http ? *http->State() : detail::ResponseState::kUnsent);
  }
}
}  // namespace q4t::server
