#include "chat_server_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/asr_stream_output.h"
#include "q4t/server/realtime_asr_policy.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::RunRealtimeAsrStream(
    const ClientIdentity& client, const RealtimeTranscriptionJob& job,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, ActualUsageCounter& usage,
    const RealtimeTranscriptSink& emit) {
  using object_detail::Error;
  const auto policy = RealtimeAsrDelayPolicy(job.settings.delay);
  if (!job.stream || !job.stream->Valid() || !policy.Valid() || !emit)
    return Error(500, "asr_stream_state_invalid",
                 "Streaming ASR owner invalid");
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return control->Cancelled();
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Streaming transcription cancelled");
  };
  auto limits = asr_limits_;
  if (client.Scoped())
    limits.max_output_tokens = std::min<uint64_t>(
        limits.max_output_tokens, client.Limits()->max_output_tokens);
  auto request = job.settings.request;
  request.stream = true;  // Incremental confidence policy, no buffered retries.
  const size_t output_budget = limits.max_output_tokens;
  AsrStreamOutput output(job.audio.epoch, output_budget, request.logprobs,
                         request.language, policy.prefix);
  if (!output.Valid())
    return Error(500, "asr_stream_state_invalid",
                 "Streaming ASR budget invalid");
  ClientRateLease rate;
  ScopeExit settle([&] { rate.Settle(); });
  bool reserved = false;
  size_t admitted_output_budget = output_budget;
  uint64_t audio_tokens = 0;
  const auto delta = [&](const AsrTextDelta& part) {
    io::Json value;
    return !cancelled() &&
           io::ParseJson(output.Transcript().DeltaJson(part), &value) &&
           emit(value);
  };
  const auto output_error = [&](const Status& status) {
    return cancelled() ? interrupted()
                       : Error(502, "asr_output_invalid", status.message());
  };
  for (;;) {
    if (cancelled()) return interrupted();
    diagnostic.Stage("stream_input");
    AsrStreamRead input;
    {
      RequestDiagnostics::Scope waiting(diagnostic,
                                        RequestDiagnostics::Wait::kInput);
      input = job.stream->Read(control->deadline(), cancelled);
    }
    if (input.kind == AsrStreamReadKind::kCancelled ||
        input.kind == AsrStreamReadKind::kDeadline) {
      control->Cancel(input.kind == AsrStreamReadKind::kDeadline
                          ? RequestControl::State::kDeadline
                          : RequestControl::State::kExplicit);
      return interrupted();
    }
    if (input.kind == AsrStreamReadKind::kEnd) {
      const auto final = output.Finalize(delta);
      if (!final) return output_error(final);
      break;
    }
    if (input.kind != AsrStreamReadKind::kReady)
      return Error(502, "asr_stream_state_invalid",
                   "Streaming ASR sample or step budget exhausted");
    // Cancellation still waits for native execution to stop borrowing before
    // releasing this exact ticket. The producer never overwrites this prefix.
    ScopeExit borrowed([&] { job.stream->Complete(input.step); });
    const auto current = clients_.RecoveryIdentity(client.Index());
    if (!clients_.OwnsIdentity(client) || !current.Allowed() ||
        current.Id() != client.Id())
      return Error(401, "client_authentication_failed",
                   "Client no longer authorized");
    if (output.Remaining() == 0)
      return Error(502, "asr_output_limit",
                   "Streaming ASR cumulative output budget exhausted");
    limits.max_output_tokens = output.Remaining();
    if (!reserved && current.Scoped())
      admitted_output_budget = std::min<size_t>(
          admitted_output_budget, current.Limits()->max_output_tokens);
    if (output.Generated() >= admitted_output_budget)
      return Error(502, "asr_output_limit",
                   "Streaming ASR admitted output budget exhausted");
    limits.max_output_tokens = std::min<size_t>(
        limits.max_output_tokens, admitted_output_budget - output.Generated());
    if (current.Scoped()) {
      if (output.Generated() >= current.Limits()->max_output_tokens)
        return Error(400, "client_output_limit_exceeded",
                     "Streaming output exceeds the current client limit");
      limits.max_output_tokens = std::min<uint64_t>(
          limits.max_output_tokens,
          current.Limits()->max_output_tokens - output.Generated());
    }
    audio::AsrFeatures features;
    diagnostic.Stage("media");
    auto status = audio::ComputeAsrFeatures(
        input.samples, audio::AsrConfig::kSampleRate,
        {size_t(asr_limits_.max_audio_seconds) * 16000,
         size_t(asr_limits_.max_audio_seconds) * 100},
        &features, cancelled);
    if (!status || cancelled())
      return cancelled()
                 ? interrupted()
                 : Error(400, "audio_features_invalid", status.message());
    AsrPrompt prompt;
    ChatPlanError error;
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
    if (!status || cancelled())
      return cancelled()
                 ? interrupted()
                 : Error(error.http_status, error.code, status.message());
    AsrTranscript hypothesis(request.logprobs, request.language);
    status =
        output.Prepare(&prompt, asr_limits_.max_context_tokens, &hypothesis);
    if (!status) return Error(400, "asr_context_limit", status.message());
    const auto prior = usage.Total();
    if (!prior) return Error(500, "asr_usage_invalid", "ASR usage unavailable");
    if (current.Scoped() &&
        (prompt.tokens.size() > current.Limits()->max_prompt_tokens ||
         prior->input >
             current.Limits()->max_prompt_tokens - prompt.tokens.size()))
      return Error(400, "client_prompt_limit_exceeded",
                   "Cumulative transcription input limit exceeded");
    const auto admission =
        reserved ? rate.ExtendPrompt(clients_, current, prompt.tokens.size())
                 : rate.Reserve(clients_, current, prompt.tokens.size(),
                                admitted_output_budget);
    if (admission != ClientRateResult::kAllowed) {
      const bool rpm = admission == ClientRateResult::kRpm;
      const bool tpm = admission == ClientRateResult::kTpm;
      return Error(rpm || tpm ? 429 : 503,
                   rpm   ? "client_rpm_exhausted"
                   : tpm ? "client_tpm_exhausted"
                   : admission == ClientRateResult::kFrozen
                       ? "service_draining"
                       : "client_rate_invalid",
                   "Streaming transcription quota unavailable");
    }
    reserved = true;
    AsrStreamPass captured(prompt);
    auto reply = RunNativeAsr(
        -1, request, features, prompt, control, diagnostic, rate, usage,
        &hypothesis, {}, [](const AsrTextDelta&) { return true; }, &captured);
    if (!reply.ok()) return reply;
    if (cancelled()) return interrupted();
    status = output.Accept(captured, input.step.final, delta);
    if (!status) return output_error(status);
    audio_tokens += prompt.audio_tokens;
    // Bounded numeric observations only; log failure cannot control the model.
    std::fprintf(stderr,
                 "[asr-stream] id=%s pass=%llu samples=%llu prefix=%zu "
                 "input=%zu output=%zu cumulative_output=%zu final=%d\n",
                 control->id().c_str(),
                 static_cast<unsigned long long>(input.step.sequence),
                 static_cast<unsigned long long>(input.step.samples),
                 prompt.continuation_tokens, prompt.tokens.size(),
                 captured.Generated(), output.Generated(),
                 int(input.step.final));
    borrowed.Run();
    if (input.step.final) break;
  }
  if (cancelled()) return interrupted();
  const auto total = usage.Total();
  ObjectReply result;
  std::string json;
  if (!output.Finished() || !job.stream->Drained() || !total ||
      !output.Transcript().ResultJson(*total, audio_tokens, false, &json) ||
      !io::ParseJson(json, &result.value))
    return Error(502, "asr_output_invalid",
                 "Streaming transcription incomplete");
  return result;
}
}  // namespace q4t::server
