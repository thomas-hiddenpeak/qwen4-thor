#include "chat_server_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/object_diagnostics.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/realtime_pcm.h"
#include "q4t/server/realtime_asr_policy.h"
#include "q4t/server/realtime_transcription_pump.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using namespace object_detail;
ObjectReply CapturedFailure(const FiniteResponse& response) {
  Json value;
  if (response.Complete() && io::ParseJson(response.Body(), &value)) {
    if (const auto* error = value.Find("error"))
      return Error(response.Status(), error->GetString("code", "server_error"),
                   error->GetString("message", "Transcription unavailable"));
  }
  return Error(503, "transcription_unavailable", "Transcription unavailable");
}
}  // namespace

void ChatServer::HandleRealtime(int fd, std::string_view path,
                                const HttpRequestHeaders& headers,
                                std::string_view initial,
                                std::string_view accept) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) return;
  if (path != "/v1/realtime?intent=transcription") {
    detail::SendError(fd, 400, "This route requires intent=transcription", {},
                      "realtime_session_type_invalid", "intent");
    return;
  }
  if (asr_model_dir_.empty()) {
    detail::SendError(fd, 404, "Transcription model is not configured", {},
                      "model_not_found", "model");
    return;
  }
  if (RejectUnhealthy(fd) ||
      !CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare))
    return;
  HttpBodyLease memory(realtime_host_budget_);
  if (!memory.Acquire(kRealtimeSessionHostBytes) ||
      !runtime::ReadSystemMemory().CanAllocate(kRealtimeSessionHostBytes +
                                               (512ULL << 20))) {
    detail::SendError(fd, 503, "Realtime host memory unavailable", {},
                      "memory_pressure");
    return;
  }
  const auto client = http->Client();
  // Optional CPU VAD preparation happens before 101, never inside the socket
  // event loop. A failed optional backend still permits healthy manual ASR.
  std::shared_ptr<const audio::SileroVad> vad;
  auto vad_ready =
      Error(503, "vad_not_configured", "Native VAD assets not configured");
  if (!vad_model_dir_.empty()) {
    BinaryAccess access;
    access.scope = client.Id();
    access.deadline = Clock::now() + std::chrono::seconds(3);
    access.cancelled = [&] { return stop_requested_.load(); };
    vad_ready = EnsureNativeVad(access, &vad);
  }
  const size_t message_bytes =
      client.Scoped() ? std::min<size_t>(kWebSocketMessageLimit,
                                         client.Limits()->max_body_bytes)
                      : kWebSocketMessageLimit;
  RealtimeTranscriptionCallbacks callbacks;
  callbacks.interrupted = [&] { return stop_requested_.load(); };
  callbacks.validate = [&](const RealtimeTranscriptionSettings& settings) {
    if (settings.request.model != audio::AsrConfig::kModelId)
      return Error(404, "model_not_found",
                   "Transcription model is not configured");
    for (const auto& code : settings.request.languages)
      if (AsrLanguageName(code).empty())
        return Error(400, "invalid_language",
                     "Transcription language unavailable");
    if (!settings.request.language.empty() &&
        AsrLanguageName(settings.request.language).empty())
      return Error(400, "invalid_language",
                   "Transcription language unavailable");
    // These remain scheduled execution tasks. Never acknowledge a processing
    // setting while silently running a different signal/turn policy.
    if (settings.turn == RealtimeTranscriptionSettings::Turn::kSemanticVad)
      return Error(400, "realtime_processor_unavailable",
                   "Requested audio processor is not connected");
    if (!settings.delay.empty() &&
        (!RealtimeAsrDelayPolicy(settings.delay).Valid() ||
         settings.turn != RealtimeTranscriptionSettings::Turn::kManual))
      return Error(400, "realtime_delay_turn_conflict",
                   "Continuous transcription uses explicit audio commits");
    if (settings.turn == RealtimeTranscriptionSettings::Turn::kServerVad &&
        !vad_ready.ok())
      return vad_ready;
    return ObjectReply{};
  };
  if (vad) {
    callbacks.vad_step = [&](auto samples, auto* state, auto* probability) {
      const auto status = vad->Step(samples, state, probability,
                                    [&] { return stop_requested_.load(); });
      if (!status && !stop_requested_.load())
        vad_health_.ComputeFailed(AsrFailure::kCompute, Clock::now());
      else if (status)
        vad_health_.ComputeSucceeded();
      return status;
    };
  }
  callbacks.acquire = [&](const std::string& id) {
    return requests_.Register(
        id, "", Clock::now() + std::chrono::milliseconds(request_deadline_ms_),
        client.Id());
  };
  callbacks.release = [&](const auto& control) { requests_.Remove(control); };
  callbacks.execute = [&](const auto& job, const auto& bytes,
                          const auto& control, const auto& emit) {
    return RunRealtimeTranscription(client, job, bytes, control, emit);
  };
  WebSocketOutput output(kWebSocketOutputMaximumBytes, 64,
                         kWebSocketMaximumLimit);
  RealtimeTranscriptionPump session(
      detail::NewRequestId("sess_"), output, std::move(callbacks),
      asr_limits_.max_audio_seconds, message_bytes);
  if (!session.Start()) {
    detail::SendError(fd, 503, "Realtime session initialization failed", {},
                      "realtime_session_unavailable");
    return;
  }
  std::string protocol;
  if (std::find(headers.websocket_protocols.begin(),
                headers.websocket_protocols.end(),
                "realtime") != headers.websocket_protocols.end())
    protocol = "realtime";
  if (!http->BeginWebSocket(accept, protocol)) return;
  WebSocketSessionCallbacks connection;
  connection.message = [&](auto opcode, auto message) {
    // A policy update can tighten the per-message budget during a connection.
    const auto current = clients_.RecoveryIdentity(client.Index());
    if (current.Scoped() && message.size() > current.Limits()->max_body_bytes)
      return false;
    return session.Message(opcode, message);
  };
  connection.authorized = [&] {
    const auto current =
        clients_.Authenticate(headers.client_id, headers.gateway_token);
    return current.Allowed() && current.Id() == client.Id();
  };
  connection.stopped = [&] { return stop_requested_.load(); };
  connection.cancel = [&] { session.Stop(); };
  connection.tick = [&] { return session.Alive(); };
  connection.message_ready = [&](size_t bytes) {
    return session.CanAccept(bytes);
  };
  WebSocketSessionLimits limits;
  limits.message_bytes = message_bytes;
  RunWebSocketSession(fd, initial, output, connection, limits);
  // Queue spans, client context and host reservation outlive the actual worker.
  // GPU calls are cooperative; the existing progress guardian covers stalls.
  session.Join();
}

ObjectReply ChatServer::RunRealtimeTranscription(
    const ClientIdentity& original, const RealtimeTranscriptionJob& job,
    const std::array<std::span<const uint8_t>, 2>& /*bytes*/,
    const std::shared_ptr<RequestControl>& control,
    const RealtimeTranscriptSink& emit) {
  metrics_.requests_total.fetch_add(1);
  const auto started = Clock::now();
  RequestDiagnostics diagnostic(stderr, control->id());
  diagnostic.Bind(control->id());
  bool success = false;
  ScopeExit finish([&] {
    if (success) {
      metrics_.requests_success.fetch_add(1);
      metrics_.e2e_seconds.Observe(
          std::chrono::duration<double>(Clock::now() - started).count());
    } else if (control->Cancelled() && !control->Failed()) {
      metrics_.requests_aborted.fetch_add(1);
      metrics_.abort_reasons[AbortReasonIndex(control->Reason())].fetch_add(1);
    } else {
      metrics_.requests_error.fetch_add(1);
    }
  });
  return RunDiagnosedObjectRequest(
      diagnostic, *control,
      [&]() -> ObjectReply {
        const auto client = clients_.RecoveryIdentity(original.Index());
        if (!clients_.OwnsIdentity(original) || !client.Allowed() ||
            client.Id() != original.Id())
          return Error(401, "client_authentication_failed",
                       "Client no longer authorized");
        ClientLease permit;
        if (!permit.Acquire(clients_, client, ClientLane::kData))
          return Error(429, "client_capacity_exhausted",
                       "Client request capacity exhausted");
        FiniteResponse captured(65536);
        detail::HttpResponseContext http(
            detail::HttpResponseContext::Detached{-1, &captured});
        http.SetClient(client, &permit);
        http.SetTraceId(control->id());
        http.Usage().Enable();
        if (RejectUnhealthy(-1) ||
            !CheckMemoryAdmission(-1, MemoryCheckpoint::kPrepare))
          return CapturedFailure(captured);
        RequestAdmissionLease admitted(active_chats_,
                                       admission_limits_.inflight);
        if (!admitted.TryAcquire()) {
          metrics_.text_admission_rejected.fetch_add(1);
          return Error(503, "capacity_exhausted", "Request capacity exhausted");
        }
        auto progress = progress_.Acquire(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                control->deadline().time_since_epoch())
                .count());
        control->BindProgress(progress.Handle());
        diagnostic.BindProgress(progress.Handle());
        ProgressContext progress_context(progress.Handle());
        const auto cancelled = [&] {
          if (stop_requested_.load())
            control->Cancel(RequestControl::State::kShutdown);
          return control->Cancelled();
        };
        const auto interrupted = [&] {
          return Error(409, control->Reason(), "Transcription cancelled");
        };
        if (cancelled()) return interrupted();
        OnException failure(
            [&] { control->Cancel(RequestControl::State::kFailure); });
        const auto scope = client.Scoped()
                               ? "client:" + std::string(client.Id())
                               : "local:anonymous";
        const BinaryAccess access{scope, control->deadline(), cancelled};
        auto reply = EnsureNativeAsr(false, access, &diagnostic);
        if (!reply.ok()) return reply;
        if (job.stream)
          return RunRealtimeAsrStream(client, job, control, diagnostic,
                                      http.Usage(), emit);
        const size_t max_samples =
            size_t(asr_limits_.max_audio_seconds) * 16000;
        const auto pcm = job.processed_audio ? job.processed_audio->Samples()
                                             : std::span<const float>{};
        if (pcm.empty() || pcm.size() > max_samples)
          return Error(400, "audio_invalid",
                       "Committed processed audio missing");
        audio::AsrFeatures features;
        auto status = audio::ComputeAsrFeatures(
            pcm, audio::AsrConfig::kSampleRate,
            {max_samples, size_t(asr_limits_.max_audio_seconds) * 100},
            &features, cancelled);
        if (!status || cancelled())
          return cancelled()
                     ? interrupted()
                     : Error(400, "audio_features_invalid", status.message());
        auto limits = asr_limits_;
        if (client.Scoped())
          limits.max_output_tokens = std::min<uint64_t>(
              limits.max_output_tokens, client.Limits()->max_output_tokens);
        AsrPrompt prompt;
        ChatPlanError error;
        const auto& request = job.settings.request;
        status = BuildAsrPrompt(
            request, audio::AsrEncodedFrames(features.frames), limits,
            [&](std::string_view text, bool special,
                std::vector<uint32_t>* ids) {
              RequestDiagnostics::Scope waiting(
                  diagnostic, RequestDiagnostics::Wait::kTokenizer);
              auto lock = LockUnlessCancelled(asr_tok_mu_, cancelled);
              if (!lock.owns_lock())
                return Status::Fail("ASR tokenization cancelled");
              return asr_tok_->Encode(text, ids,
                                      {.cancelled = cancelled,
                                       .recognize_special_tokens = special});
            },
            &prompt, &error);
        if (!status || cancelled())
          return cancelled()
                     ? interrupted()
                     : Error(error.http_status, error.code, status.message());
        if (client.Scoped() &&
            prompt.tokens.size() > client.Limits()->max_prompt_tokens)
          return Error(400, "client_prompt_limit_exceeded",
                       "Transcription input limit exceeded");
        ClientRateLease rate;
        ScopeExit settle([&] { rate.Settle(); });
        const auto reserved = rate.Reserve(
            clients_, client, prompt.tokens.size(), limits.max_output_tokens);
        if (reserved != ClientRateResult::kAllowed) {
          const bool rpm = reserved == ClientRateResult::kRpm;
          const bool tpm = reserved == ClientRateResult::kTpm;
          return Error(rpm || tpm ? 429 : 503,
                       rpm   ? "client_rpm_exhausted"
                       : tpm ? "client_tpm_exhausted"
                       : reserved == ClientRateResult::kFrozen
                           ? "service_draining"
                           : "client_rate_invalid",
                       "Transcription quota unavailable");
        }
        AsrTranscript transcript(request.logprobs, request.language);
        reply = RunNativeAsr(
            -1, request, features, prompt, control, diagnostic, rate,
            http.Usage(), &transcript, {}, [&](const AsrTextDelta& delta) {
              Json value;
              return io::ParseJson(transcript.DeltaJson(delta), &value) &&
                     emit(value);
            });
        if (!reply.ok()) return reply;
        if (cancelled()) return interrupted();
        const auto usage = http.Usage().Total();
        std::string result;
        if (!usage ||
            !transcript.ResultJson(*usage, prompt.audio_tokens, false,
                                   &result) ||
            !io::ParseJson(result, &reply.value))
          return Error(502, "asr_usage_invalid",
                       "Transcription accounting unavailable");
        return reply;
      },
      &success);
}
}  // namespace q4t::server
