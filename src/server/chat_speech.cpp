#include "chat_server_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
void ChatServer::HandleSpeech(int fd, const std::string& body,
                              std::string_view accept) {
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
    if (!http || !http->Client().Allowed()) {
      detail::SendError(fd, 503, "Authenticated speech owner unavailable", {},
                        "object_scope_unavailable");
      return;
    }
    http->Usage().Enable();
    if (RejectUnhealthy(fd) ||
        !CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare))
      return;
    RequestAdmissionLease admitted(active_chats_, admission_limits_.inflight);
    if (!admitted.TryAcquire()) {
      metrics_.text_admission_rejected.fetch_add(1);
      detail::SendError(fd, 503, "Request capacity exhausted", {},
                        "capacity_exhausted");
      return;
    }
    SpeechRequest request;
    ChatPlanError error;
    if (auto status = ParseSpeechRequest(body, &request, &error); !status) {
      detail::SendError(fd, error.http_status, status.message(), {}, error.code,
                        error.param);
      return;
    }
    std::string_view content_type;
    if (!request.sse) {
      const auto status = NegotiateSpeechContentType(
          request.format, accept, &content_type, &error);
      if (!status) {
        detail::SendError(fd, error.http_status, status.message(), {}, error.code);
        return;
      }
    }
    const auto id = detail::NewRequestId("tts_");
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
    const auto fail = [&](const ObjectReply& reply) {
      if (!cancelled()) control->Cancel(RequestControl::State::kFailure);
      detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                        reply.ClientCode(), reply.value.GetString("param"));
    };
    if (request.sse) http->SetSpeechStream();
    const BinaryAccess access{http->Client().Scoped()
                                  ? "client:" + std::string(http->Client().Id())
                                  : "local:anonymous",
                              control->deadline(), cancelled};
    audio::TtsPrompt prompt;
    auto planned = PlanNativeSpeech(request, access, diagnostic, &prompt);
    if (!planned.ok()) {
      fail(planned);
      return;
    }
    size_t max_frames = tts_limits_.max_frames;
    if (const auto* limits = http->Client().Limits()) {
      if (prompt.InputTokens() > limits->max_prompt_tokens ||
          limits->max_output_tokens < 2 * audio::TtsConfig::kCodeGroups + 1) {
        detail::SendError(fd, 400, "Speech exceeds client token limits", {},
                          "client_token_limit_exceeded");
        return;
      }
      max_frames =
          std::min<uint64_t>(max_frames, (limits->max_output_tokens - 1) /
                                             audio::TtsConfig::kCodeGroups);
    }
    ClientRateLease rate(http->RateDomain());
    ScopeExit settle([&] { rate.Settle(); });
    const auto reserved =
        rate.Reserve(clients_, http->Client(), prompt.InputTokens(),
                     max_frames * audio::TtsConfig::kCodeGroups + 1);
    if (reserved != ClientRateResult::kAllowed) {
      const bool rpm = reserved == ClientRateResult::kRpm;
      const bool tpm = reserved == ClientRateResult::kTpm;
      detail::SendError(
          fd, rpm || tpm ? 429 : 503, "Speech quota unavailable", {},
          rpm                                     ? "client_rpm_exhausted"
          : tpm                                   ? "client_tpm_exhausted"
          : reserved == ClientRateResult::kFrozen ? "service_draining"
                                                  : "client_rate_invalid");
      return;
    }
    std::string finite;
    const auto sink = [&](std::string_view bytes) -> ObjectReply {
      if (cancelled())
        return object_detail::Error(409, control->Reason(), "Speech cancelled");
      if (!request.sse && !http->Http11()) {
        if (bytes.size() >
            (uint64_t(tts_limits_.max_output_mib) << 20) - finite.size())
          return object_detail::Error(413, "speech_encoding_capacity",
                                      "Speech output exceeds budget");
        finite += bytes;
        return {};
      }
      RequestDiagnostics::Scope wait(diagnostic,
                                     RequestDiagnostics::Wait::kWrite);
      const detail::WriteOptions write{control->deadline(), control.get()};
      if (*http->State() == detail::ResponseState::kUnsent) {
        const bool started =
            request.sse
                ? detail::BeginStreamingResponse(fd, http->State(), write)
                : detail::BeginBinaryStreamResponse(
                      fd, content_type, write);
        if (!started)
          return object_detail::Error(409, "response_write_failed",
                                      "Speech header failed");
      }
      std::string event;
      if (request.sse) {
        if (!SpeechAudioDelta(bytes, &event))
          return object_detail::Error(502, "speech_output_invalid",
                                      "Speech event invalid");
        bytes = event;
      }
      if (!detail::WriteAll(fd, bytes.data(), bytes.size(), write))
        return object_detail::Error(409, "response_write_failed",
                                    "Speech write failed");
      return {};
    };
    SpeechEncodingReceipt receipt;
    auto result = RunNativeTts(fd, request, prompt, max_frames, control,
                               diagnostic, rate, http->Usage(), sink, &receipt);
    if (!result.ok()) {
      fail(result);
      return;
    }
    const auto actual = http->Usage().Total();
    std::string terminal;
    if (!actual || (request.sse && !SpeechAudioDone(*actual, &terminal))) {
      detail::SendError(fd, 502, "Speech usage unavailable", {},
                        "tts_usage_invalid");
      return;
    }
    if (cancelled() || !control->Finish()) {
      detail::SendError(fd, 409, "Speech cancelled", {}, control->Reason());
      return;
    }
    const detail::WriteOptions write{control->deadline()};
    diagnostic.Stage("response");
    if (request.sse) {
      http->BeforeTerminal();
      *http->State() = detail::ResponseState::kTerminal;
      success = detail::WriteAll(fd, terminal, write);
      if (success) *http->State() = detail::ResponseState::kComplete;
    } else if (!http->Http11()) {
      success = detail::SendSimple(
          fd, 200, "OK", finite, std::string(content_type),
          write);
    } else {
      http->BeforeTerminal();
      *http->State() = detail::ResponseState::kComplete;
      success = true;  // Outer transport writes the terminating chunk.
    }
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
