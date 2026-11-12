#include "chat_server_internal.h"

#include <new>

#include "chat_model_step.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/server/vector_search_api.h"

namespace q4t::server {
void ChatServer::HandleVectorSearch(int fd, const std::string& method,
                                    const std::string& path,
                                    const std::string& body) {
  auto* http = detail::HttpResponseContext::Current(fd);
  metrics_.requests_total.fetch_add(1, std::memory_order_relaxed);
  bool success = false;
  const auto started = std::chrono::steady_clock::now();
  std::shared_ptr<RequestControl> control;
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
    if (control) requests_.Remove(control);
    if (http) http->RefreshUsage();
  });
  try {
    if (!objects_ || !http || !http->Client().Allowed()) {
      detail::SendError(fd, 503, "Authenticated application state unavailable",
                        {}, "object_scope_unavailable");
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
    const auto id = detail::NewRequestId("search_");
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
    ScopeExit unbind([&] { control->BindProgress({}); });
    RequestDiagnostics diagnostic(stderr, http->TraceId());
    diagnostic.BindProgress(progress.Handle());
    diagnostic.Bind(id);
    ProgressContext context(progress.Handle());
    const auto cancelled = [&] {
      if (stop_requested_.load())
        control->Cancel(RequestControl::State::kShutdown);
      return detail::RequestCancelled(control.get(), fd);
    };
    const auto& client = http->Client();
    const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                       : "local:anonymous";
    BinaryAccess access{scope, control->deadline(), cancelled};
    ClientRateLease rate(http->RateDomain());
    VectorNativeSearchCallbacks callbacks;
    callbacks.encode = [&](auto text, const auto& budget, auto* output) {
      RequestDiagnostics::Scope waiting(diagnostic,
                                        RequestDiagnostics::Wait::kTokenizer);
      return EncodeVectorDocument(text, budget, output);
    };
    callbacks.embed = [&](const auto& tokens, auto* output) {
      return RunNativeEmbeddingInput(fd, tokens, control, diagnostic, output);
    };
    callbacks.rewrite = [&](const auto& rewrite, auto& accounting,
                            auto* output) {
      InternalModelStep step{rewrite.request,
                             PlanningConfig(fd),
                             accounting,
                             [] { return ObjectReply{}; },
                             {},
                             false,
                             {},
                             {},
                             0,
                             0};
      auto reply = RunInternalModelStep(fd, &step, control);
      if (reply.ok())
        *output = {std::move(step.content), std::move(step.finish_reason)};
      return reply;
    };
    auto reply = HandleVectorSearchRequest(
        *objects_, clients_, !embedding_model_dir_.empty(), method, path, body,
        client, access, rate, http->Usage(), callbacks);
    if (!reply.ok()) {
      detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                        reply.ClientCode(), reply.value.GetString("param"),
                        reply.value.GetString("allow"));
      return;
    }
    if (cancelled() || !control->Finish()) {
      detail::SendError(fd, 409, "Search request cancelled", {},
                        control->Reason());
      return;
    }
    diagnostic.Stage("response");
    success = detail::SendSimple(fd, 200, "OK", OutputJson(reply.value),
                                 "application/json", {control->deadline()});
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
