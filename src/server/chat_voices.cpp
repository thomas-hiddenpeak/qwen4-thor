#include "chat_server_internal.h"

#include <algorithm>

#include "object_protocol.h"

#include "q4t/server/application_routes.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/server/voice_api.h"

namespace q4t::server {
void ChatServer::HandleVoices(int fd, std::string_view method,
                              std::string_view target,
                              std::string_view content_type,
                              std::string_view body) {
  using Clock = std::chrono::steady_clock;
  auto* http = detail::HttpResponseContext::Current(fd);
  const auto started = Clock::now();
  metrics_.requests_total.fetch_add(1);
  bool success = false;
  std::shared_ptr<RequestControl> control;
  ScopeExit finish([&] {
    if (success) {
      metrics_.requests_success.fetch_add(1);
      metrics_.e2e_seconds.Observe(
          std::chrono::duration<double>(Clock::now() - started).count());
    } else if (control && control->Cancelled() && !control->Failed()) {
      metrics_.requests_aborted.fetch_add(1);
      metrics_.abort_reasons[AbortReasonIndex(control->Reason())].fetch_add(1);
    } else {
      metrics_.requests_error.fetch_add(1);
    }
    if (control) requests_.Remove(control);
    if (http) http->RefreshUsage();
  });
  try {
    if (!http || !http->Client().Allowed() || !objects_) {
      detail::SendError(fd, 503, "Authenticated voice store unavailable", {},
                        "object_scope_unavailable");
      return;
    }
    const bool creating = IsVoiceCreation(method, target);
    RequestAdmissionLease admitted(active_chats_, admission_limits_.inflight);
    if (creating) {
      http->Usage().Enable();
      if (RejectUnhealthy(fd) ||
          !CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare))
        return;
      if (!admitted.TryAcquire()) {
        metrics_.text_admission_rejected.fetch_add(1);
        detail::SendError(fd, 503, "Request capacity exhausted", {},
                          "capacity_exhausted");
        return;
      }
    }
    // Metadata, revoke and cleanup remain available without healthy models.
    const auto id = detail::NewRequestId("voice_");
    control = requests_.Register(
        id, "", started + std::chrono::milliseconds(request_deadline_ms_),
        http->Client().Id());
    if (!control) {
      detail::SendError(fd, 409, "Request identity already active", {},
                        "request_id_conflict");
      return;
    }
    OnException failure(
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
    const BinaryAccess access{http->Client().Scoped()
                                  ? "client:" + std::string(http->Client().Id())
                                  : "local:anonymous",
                              control->deadline(), cancelled};
    ClientRateLease rate(http->RateDomain());
    const auto verify = [&](const VoiceAudio& audio, std::string_view language,
                            VoiceSampleEvidence* evidence,
                            std::string* phrase) {
      auto output = uint64_t(asr_limits_.max_output_tokens);
      if (const auto* client = http->Client().Limits())
        output = std::min<uint64_t>(output, client->max_output_tokens);
      const auto reserved = rate.Reserve(clients_, http->Client(), 0, output);
      if (reserved != ClientRateResult::kAllowed) {
        const bool rpm = reserved == ClientRateResult::kRpm;
        const bool tpm = reserved == ClientRateResult::kTpm;
        return object_detail::Error(rpm || tpm ? 429 : 503,
                                    rpm   ? "client_rpm_exhausted"
                                    : tpm ? "client_tpm_exhausted"
                                    : reserved == ClientRateResult::kFrozen
                                        ? "service_draining"
                                        : "client_rate_invalid",
                                    "Voice verification quota unavailable");
      }
      rate.StartPrompt(0);  // Silent/rejected evidence still consumes one RPM.
      return PrepareVoiceEvidence(fd, audio, language, control, diagnostic,
                                  rate, evidence, phrase);
    };
    const auto reply = HandleVoiceRequest(*objects_, method, target,
                                          content_type, body, access, verify);
    if (reply.ok()) {
      success = detail::SendSimple(fd, reply.http_status, "OK",
                                   OutputJson(reply.value), "application/json",
                                   {control->deadline()});
    } else {
      if (!cancelled()) control->Cancel(RequestControl::State::kFailure);
      if (reply.http_status == 405)
        http->RecordReceiveFailure(HttpReceiveFailure::kMethod, 405);
      detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                        reply.ClientCode(), reply.value.GetString("param"),
                        reply.value.GetString("allow"));
    }
    const std::string reason = success ? "complete"
                               : reply.ok() ? "response_write_failed"
                                            : std::string(reply.ClientCode());
    diagnostic.Outcome(success ? "success" : "error", reason.c_str());
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
