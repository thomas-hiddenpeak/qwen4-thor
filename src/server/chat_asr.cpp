#include "chat_server_internal.h"

#include <algorithm>

#include "q4t/server/file_audio.h"
#include "q4t/server/asr_chunk_output.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
void ChatServer::HandleTranscription(int fd, std::string_view content_type,
                                     std::string_view body) {
  using Clock = std::chrono::steady_clock;
  auto* http = detail::HttpResponseContext::Current(fd);
  metrics_.requests_total.fetch_add(1);
  const auto started = Clock::now();
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
      detail::SendError(fd, 503,
                        "Authenticated transcription owner unavailable", {},
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
    AsrRequest request;
    ChatPlanError error;
    auto status = ParseAsrRequest(content_type, body, &request, &error);
    if (!status) {
      detail::SendError(fd, error.http_status, status.message(), {}, error.code,
                        error.param);
      return;
    }
    if (asr_model_dir_.empty() || request.model != audio::AsrConfig::kModelId) {
      detail::SendError(fd, 404, "Transcription model is not configured", {},
                        "model_not_found", "model");
      return;
    }
    // Diarization retains its required speaker-identity executor.
    if (request.format == AsrFormat::kDiarizedJson) {
      detail::SendError(fd, 503, "Native speaker diarization executor pending",
                        {}, "asr_executor_pending", "response_format");
      return;
    }
    const bool chunked = request.chunking.mode != AsrChunking::Mode::kSingle;
    const bool aligned = request.format == AsrFormat::kSrt ||
                         request.format == AsrFormat::kVtt ||
                         request.format == AsrFormat::kVerboseJson;
    const auto id = detail::NewRequestId("asr_");
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
    const auto send_failure = [&](const ObjectReply& reply) {
      if (!cancelled()) control->Cancel(RequestControl::State::kFailure);
      detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                        reply.ClientCode(), reply.value.GetString("param"));
    };
    if (request.stream) http->SetTranscriptionStream();
    const auto& client = http->Client();
    const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                       : "local:anonymous";
    const BinaryAccess access{scope, control->deadline(), cancelled};
    ObjectReply reply;
    {
      RequestDiagnostics::Scope waiting(diagnostic,
                                        RequestDiagnostics::Wait::kStorage);
      reply = EnsureNativeAsr(false, access);
      if (reply.ok() && aligned) reply = EnsureNativeAligner(false, access);
    }
    if (!reply.ok()) {
      send_failure(reply);
      return;
    }
    FileAudioLimits audio_limits;
    audio_limits.max_duration_ns =
        uint64_t(aligned && !chunked
                     ? std::min(asr_limits_.max_audio_seconds,
                                aligner_limits_.max_audio_seconds)
                     : asr_limits_.max_audio_seconds) *
        1000000000;
    audio_limits.max_output_samples = audio_limits.max_duration_ns / 62500;
    FileAudioPcm pcm;
    {
      RequestDiagnostics::Scope waiting(diagnostic,
                                        RequestDiagnostics::Wait::kStorage);
      reply = DecodeAudioContainer(request.file, audio_limits, access, &pcm);
    }
    if (!reply.ok()) {
      send_failure(reply);
      return;
    }
    // The caller's HTTP body remains bounded; release our duplicate file
    // storage before feature extraction and device workspace admission.
    std::string().swap(request.file);
    AsrServiceLimits limits = asr_limits_;
    if (const auto* client = http->Client().Limits())
      limits.max_output_tokens = std::min<uint64_t>(limits.max_output_tokens,
                                                    client->max_output_tokens);
    const auto output_budget = uint64_t(limits.max_output_tokens);
    ClientRateLease rate(http->RateDomain());
    ScopeExit settle([&] { rate.Settle(); });
    const auto admitted_rate = [&](ClientRateResult reserved) {
      if (reserved == ClientRateResult::kAllowed) return true;
      const bool rpm = reserved == ClientRateResult::kRpm;
      const bool tpm = reserved == ClientRateResult::kTpm;
      const bool frozen = reserved == ClientRateResult::kFrozen;
      detail::SendError(fd,
                        rpm || tpm ? 429
                        : frozen   ? 503
                                   : 500,
                        "Transcription quota unavailable", {},
                        rpm      ? "client_rpm_exhausted"
                        : tpm    ? "client_tpm_exhausted"
                        : frozen ? "service_draining"
                                 : "client_rate_invalid");
      return false;
    };
    bool reserved = false;
    std::vector<audio::VadChunk> chunks{{0, pcm.samples.size(), false}};
    if (chunked) {
      // Even a VAD-silent request consumes one RPM. Reserve the shared output
      // ceiling once; actual decoder inputs are extended before each clip.
      if (!admitted_rate(
              rate.Reserve(clients_, http->Client(), 0, output_budget)))
        return;
      rate.StartPrompt(0);
      reserved = true;
      diagnostic.Stage("vad");
      {
        RequestDiagnostics::Scope waiting(diagnostic,
                                          RequestDiagnostics::Wait::kMedia);
        reply = PrepareVadChunks(request, pcm.samples, access, &chunks);
      }
      if (!reply.ok()) {
        send_failure(reply);
        return;
      }
    }
    AsrChunkOutput output(request, pcm.samples.size());
    for (size_t index = 0; index < chunks.size(); ++index) {
      const auto& chunk = chunks[index];
      audio::AsrFeatures features;
      status = audio::ComputeAsrFeatures(
          std::span<const float>(pcm.samples)
              .subspan(chunk.begin_sample,
                       chunk.end_sample - chunk.begin_sample),
          audio::AsrConfig::kSampleRate,
          {audio_limits.max_output_samples,
           size_t(asr_limits_.max_audio_seconds) * 100},
          &features, cancelled);
      if (!status || cancelled()) {
        detail::SendError(
            fd, cancelled() ? 409 : 400, status.message(), {},
            cancelled() ? control->Reason() : "audio_features_invalid", "file");
        return;
      }
      const auto prior = http->Usage().Total();
      if (!prior || prior->output >= output_budget) {
        detail::SendError(fd, 502, "ASR shared output budget exhausted", {},
                          "asr_output_limit");
        return;
      }
      limits.max_output_tokens = output_budget - prior->output;
      AsrPrompt prompt;
      status = BuildAsrPrompt(
          request, audio::AsrEncodedFrames(features.frames), limits,
          [&](std::string_view text, bool special, std::vector<uint32_t>* ids) {
            RequestDiagnostics::Scope waiting(
                diagnostic, RequestDiagnostics::Wait::kTokenizer);
            auto lock = LockUnlessCancelled(asr_tok_mu_, cancelled);
            if (!lock.owns_lock())
              return Status::Fail("ASR tokenization cancelled");
            return asr_tok_->Encode(
                text, ids,
                {.cancelled = cancelled, .recognize_special_tokens = special});
          },
          &prompt, &error);
      if (!status || cancelled()) {
        detail::SendError(
            fd, cancelled() ? 409 : error.http_status, status.message(), {},
            cancelled() ? control->Reason() : error.code, error.param);
        return;
      }
      const uint64_t input =
          prompt.tokens.size() + prompt.no_speech_prefix.size();
      if (const auto* client = http->Client().Limits();
          client && (input > client->max_prompt_tokens ||
                     prior->input > client->max_prompt_tokens - input)) {
        detail::SendError(fd, 400,
                          "Transcription exceeds cumulative client input limit",
                          {}, "client_prompt_limit_exceeded", "file");
        return;
      }
      if (!admitted_rate(
              reserved ? rate.ExtendPrompt(clients_, http->Client(), input)
                       : rate.Reserve(clients_, http->Client(), input,
                                      output_budget)))
        return;
      reserved = true;
      AsrTranscript transcript(request.logprobs, request.language,
                               request.segment_timestamps);
      reply = RunNativeAsr(fd, request, features, prompt, control, diagnostic,
                           rate, http->Usage(), &transcript,
                           output.Text().empty() ? "" : " ");
      if (!reply.ok()) {
        send_failure(reply);
        return;
      }
      std::string part;
      size_t cues = 0;
      if (aligned) {
        reply = FormatAlignedTranscription(
            fd, request, features, (chunk.end_sample - chunk.begin_sample) / 16,
            transcript, control, diagnostic, rate, http->Usage(), &part,
            chunk.begin_sample / 16, output.CueCount() + 1, index == 0, &cues);
        if (!reply.ok()) {
          send_failure(reply);
          return;
        }
      } else {
        const auto actual = http->Usage().Total();
        status = actual ? transcript.ResultJson(*actual, prompt.audio_tokens,
                                                false, &part)
                        : Status::Fail("ASR usage unavailable");
        if (!status) {
          detail::SendError(fd, 502, status.message(), {}, "asr_usage_invalid");
          return;
        }
      }
      status = output.Append(transcript, std::move(part), chunk,
                             prompt.audio_tokens, cues);
      if (!status) {
        detail::SendError(fd, 502, status.message(), {}, "asr_output_invalid");
        return;
      }
    }
    std::string response;
    const auto actual = http->Usage().Total();
    status = actual ? output.Finish(*actual, &response)
                    : Status::Fail("ASR usage unavailable");
    if (!status) {
      detail::SendError(fd, 502, status.message(), {}, "asr_usage_invalid");
      return;
    }
    if (cancelled() || !control->Finish()) {
      detail::SendError(fd, 409, "Transcription cancelled", {},
                        control->Reason());
      return;
    }
    diagnostic.Stage("response");
    const detail::WriteOptions write{control->deadline()};
    if (request.stream) {
      if (*http->State() == detail::ResponseState::kUnsent &&
          !detail::BeginStreamingResponse(fd, http->State(), write))
        return;
      http->StopHeartbeats();
      http->BeforeTerminal();
      *http->State() = detail::ResponseState::kTerminal;
      success = detail::WriteAll(
          fd, "event: transcript.text.done\r\ndata: " + response + "\r\n\r\n",
          write);
      if (success) *http->State() = detail::ResponseState::kComplete;
    } else {
      const bool text = request.format == AsrFormat::kText;
      const char* content_type = request.format == AsrFormat::kVtt
                                     ? "text/vtt; charset=utf-8"
                                 : request.format == AsrFormat::kSrt
                                     ? "application/x-subrip; charset=utf-8"
                                 : text ? "text/plain; charset=utf-8"
                                        : "application/json";
      success =
          detail::SendSimple(fd, 200, "OK", response, content_type, write);
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
