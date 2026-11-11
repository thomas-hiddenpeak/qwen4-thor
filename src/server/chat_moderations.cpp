#include "chat_server_internal.h"

#include <new>

#include "chat_model_step.h"
#include "q4t/server/moderations_execution.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
void ChatServer::HandleModerations(int fd, const std::string& body,
                                   std::shared_ptr<RequestControl> owner) {
  auto* http = detail::HttpResponseContext::Current(fd);
  metrics_.requests_total.fetch_add(1, std::memory_order_relaxed);
  bool success = false;
  const auto started = std::chrono::steady_clock::now();
  std::shared_ptr<RequestControl> control = std::move(owner);
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
    auto status = io::ParseJson(body, &request, true, kApplicationJsonLimits);
    if (!status) {
      detail::SendError(fd, 400, "Invalid moderation JSON", {}, "invalid_json");
      return;
    }
    const auto config = PlanningConfig(fd);
    ModerationsRequestPlan plan;
    ChatPlanError error;
    status = PlanModerationsRequest(request, config, &plan, &error);
    if (!status) {
      detail::SendError(fd, error.http_status, status.message(), {}, error.code,
                        error.param);
      return;
    }
    const auto id = detail::NewRequestId("modr_");
    if (!control)
      control = requests_.Register(
          id, "", started + std::chrono::milliseconds(request_deadline_ms_),
          http->Client().Id());
    if (!control) {
      detail::SendError(fd, 409, "Request identity already active", {},
                        "request_id_conflict");
      return;
    }
    const auto cancelled = [&] {
      if (stop_requested_.load())
        control->Cancel(RequestControl::State::kShutdown);
      return detail::RequestCancelled(control.get(), fd);
    };
    ClientRateLease own_rate(http->RateDomain());
    const auto batch_rate = http->BatchRate();
    if (batch_rate && (http->RateDomain() != ClientRateDomain::kBatch ||
                       !batch_rate->PrechargedFor(http->Client()))) {
      detail::SendError(fd, 500, "Native Batch rate ownership changed", {},
                        "batch_execution_rate_invalid");
      return;
    }
    auto& rate = batch_rate ? *batch_rate : own_rate;
    ScopeExit settle([&] { rate.Settle(); });
    auto reply = ExecuteModerationsRequest(
        id, plan, clients_, http->Client(), rate,
        [&](const ModerationPlan& input, ModelPreparation& accounting,
            ModerationStepResult* result) {
          InternalModelStep step{input.request,
                                 config,
                                 accounting,
                                 [] { return ObjectReply{}; },
                                 {},
                                 false,
                                 {},
                                 {},
                                 0,
                                 0};
          auto executed = RunInternalModelStep(fd, &step, control);
          if (executed.ok()) {
            result->content = std::move(step.content);
            result->finish_reason = std::move(step.finish_reason);
          }
          return executed;
        },
        cancelled);
    // All device, grammar and internal step owners are already drained.
    rate.Settle();
    if (!reply.ok()) {
      detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                        reply.ClientCode(), reply.value.GetString("param"));
      return;
    }
    if (cancelled() || !control->Finish()) {
      detail::SendError(fd, 409, "Moderation request was cancelled", {},
                        control->Reason());
      return;
    }
    detail::SendSimple(fd, 200, "OK", OutputJson(reply.value),
                       "application/json", {control->deadline()});
    success = true;
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
