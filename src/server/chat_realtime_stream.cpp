#include "chat_server_internal.h"

#include <algorithm>
#include <limits>

#include "chat_model_step.h"
#include "object_protocol.h"
#include "q4t/server/asr_stream_output.h"
#include "q4t/server/realtime_asr_policy.h"
#include "q4t/server/realtime_semantic_score.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::RunRealtimeAsrStream(
    const ClientIdentity& client, const RealtimeTranscriptionJob& job,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, ActualUsageCounter& usage,
    const RealtimeTranscriptSink& emit) {
  using object_detail::Error;
  const auto policy =
      RealtimeAsrDelayPolicy(job.semantic ? "minimal" : job.settings.delay);
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
  uint64_t semantic_input = 0, semantic_output = 0;
  const auto score_turn = [&](const AsrStreamStep& pass,
                               const AsrTranscript& hypothesis) {
    if (!job.semantic || pass.final || !hypothesis.Complete() ||
        hypothesis.Text().empty() || cancelled())
      return;
    auto classifier = std::make_shared<RequestControl>(
        control->id() + "-semantic-" + std::to_string(pass.sequence), "",
        std::min(control->deadline(),
                 std::chrono::steady_clock::now() + std::chrono::seconds(2)),
        client.Id());
    RealtimeSemanticTicket ticket;
    if (!job.semantic->TryScore(pass.sequence,
                                job.audio.begin_ticks + pass.samples * 3,
                                classifier, &ticket))
      return;
    std::optional<double> probability;
    ScopeExit complete([&] { job.semantic->Complete(ticket, probability); });
    const auto previous = usage.Total();
    if (!previous) return;
    // The ASR invocation has released its slot/workspace. This serial native
    // model step borrows the existing client/RPM lease, with a separately
    // reserved finite output allowance. No loopback request or new worker.
    try {
      io::Json request;
      auto config = PlanningConfig(-1);
      if (!BuildRealtimeSemanticRequest(config.model, hypothesis.Text(),
                                          &request))
        return;
      const auto current = clients_.RecoveryIdentity(client.Index());
      if (!clients_.OwnsIdentity(client) || !current.Allowed() ||
          current.Id() != client.Id())
        return;
      ModelPreparation accounting(clients_, current, rate,
                                   admitted_output_budget, 0,
                                   kRealtimeSemanticStepTokens);
      ScopeExit observe([&] {
        semantic_input += accounting.InputTokens();
        semantic_output += accounting.OutputTokens();
      });
      if (!accounting.ContinueReservation(previous->input, previous->output))
        return;
      // The parent output limit applies to its ASR answer. The private 128
      // tokens were explicitly reserved in the same client TPM lease below.
      config.client_limits = nullptr;
      InternalModelStep step{request,
                             config,
                             accounting,
                             [&] {
                               return cancelled() || classifier->Cancelled()
                                          ? Error(409, "explicit_cancel",
                                                   "Semantic score cancelled")
                                          : ObjectReply{};
                             },
                             {}, false, {}, {}, 0, 0};
      diagnostic.Stage("semantic_turn");
      RequestDiagnostics::Scope waiting(diagnostic,
                                        RequestDiagnostics::Wait::kModel);
      const auto executed = RunInternalModelStep(-1, &step, classifier);
      if (!executed.ok() || classifier->Cancelled() || cancelled()) return;
      double value = 0;
      if (ParseRealtimeSemanticScore(step.content, step.finish_reason, &value))
        probability = value;
      else
        probability = std::numeric_limits<double>::quiet_NaN();
    } catch (...) {
      // Genuine device failures retain the normal core health/progress path;
      // a missing auxiliary score itself cannot stop a healthy ASR invocation.
    }
  };
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
                                admitted_output_budget, ClientClock::now(), 0,
                                job.semantic ? kRealtimeSemanticOutputTokens
                                             : 0);
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
    score_turn(input.step, hypothesis);
    if (RejectUnhealthy(-1))
      return Error(503, "service_unhealthy",
                   "Native execution health protection activated");
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
  if (job.semantic) {
    using namespace object_detail;
    const auto observed = job.semantic->Inspect();
    Set(&result.value, "q4t_semantic_vad",
        Object({{"profile", String("q4t-native-semantic-turn-v1")},
                {"attempts", Number(observed.attempts)},
                {"accepted", Number(observed.accepted)},
                {"unavailable", Number(observed.unavailable)},
                {"invalid", Number(observed.invalid)},
                {"stale", Number(observed.stale)},
                {"maximum_wait", Boolean(observed.decision.maximum_wait)},
                {"probability_available",
                 Boolean(observed.decision.probability_available)},
                {"input_tokens", Number(semantic_input)},
                {"output_tokens", Number(semantic_output)},
                {"included_in_usage", Boolean(true)}}));
  }
  return result;
}
}  // namespace q4t::server
