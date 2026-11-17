#include "chat_server_internal.h"

#include "object_protocol.h"
#include "q4t/server/chat_audio_inputs.h"
#include "q4t/server/file_audio.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::PrepareNativeChatAudio(
    int fd, const io::Json& request,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, ClientRateLease& rate,
    uint64_t public_output, uint64_t auxiliary_output, io::Json* output,
    ActualUsage* actual, uint64_t* audio_tokens) {
  using namespace object_detail;
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed() || !output || !actual ||
      !audio_tokens || !control)
    return Error(500, "chat_audio_owner_invalid",
                 "Audio preparation owner unavailable");
  const auto before = http->Usage().Total();
  if (!before || before->input || before->output)
    return Error(500, "chat_audio_usage_invalid",
                 "Audio preparation must precede main model execution");
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const BinaryAccess access{http->Client().Scoped()
                                ? "client:" + std::string(http->Client().Id())
                                : "local:anonymous",
                            control->deadline(), cancelled};
  ChatAudioInputs inputs;
  auto reply = inputs.Prepare(request, access);
  if (!reply.ok()) return reply;
  if (!inputs.Active())
    return Error(500, "chat_audio_owner_invalid", "No audio input to prepare");
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kStorage);
    reply = EnsureNativeAsr(false, access, &diagnostic);
  }
  if (!reply.ok()) return reply;
  const uint64_t max_samples = uint64_t(asr_limits_.max_audio_seconds) * 16000;
  const uint64_t output_budget = asr_limits_.max_output_tokens;
  uint64_t samples = 0, encoded_audio = 0;
  bool reserved = false;
  for (size_t i = 0; i < inputs.Inputs().size(); ++i) {
    const auto& item = inputs.Inputs()[i];
    const auto at = [&](ObjectReply failure) {
      if (!failure.ok())
        failure.value = Object({{"param", String(item.param)}});
      return failure;
    };
    if (samples == max_samples)
      return at(Error(413, "chat_audio_duration_capacity",
                      "Audio clips exceed the shared duration budget"));
    FileAudioLimits audio_limits;
    audio_limits.max_output_samples = max_samples - samples;
    audio_limits.max_duration_ns = (max_samples - samples) * 62500;
    FileAudioPcm pcm;
    diagnostic.Stage("audio_decode");
    {
      RequestDiagnostics::Scope waiting(diagnostic,
                                        RequestDiagnostics::Wait::kMedia);
      reply =
          DecodeAudioContainer(item.source.bytes, audio_limits, access, &pcm);
    }
    if (!reply.ok()) return at(std::move(reply));
    samples += pcm.samples.size();
    audio::AsrFeatures features;
    const auto features_ok =
        audio::ComputeAsrFeatures(pcm.samples, audio::AsrConfig::kSampleRate,
                                  {audio_limits.max_output_samples,
                                   size_t(asr_limits_.max_audio_seconds) * 100},
                                  &features, cancelled);
    if (!features_ok || cancelled())
      return at(
          Error(cancelled() ? 409 : 400,
                cancelled() ? control->Reason() : "audio_features_invalid",
                "Audio feature preparation failed"));
    std::vector<float>().swap(pcm.samples);
    const auto prior = http->Usage().Total();
    if (!prior || prior->output >= output_budget)
      return at(Error(502, "asr_output_limit",
                      "Audio transcription shared output budget exhausted"));
    auto limits = asr_limits_;
    limits.max_output_tokens = output_budget - prior->output;
    AsrRequest transcription;
    transcription.model = audio::AsrConfig::kModelId;
    AsrPrompt prompt;
    ChatPlanError error;
    auto status = BuildAsrPrompt(
        transcription, audio::AsrEncodedFrames(features.frames), limits,
        [&](std::string_view text, bool special, std::vector<uint32_t>* ids) {
          auto lock = diagnostic.Lock(
              asr_tok_mu_, RequestDiagnostics::Wait::kTokenizer, cancelled);
          if (!lock.owns_lock())
            return Status::Fail("Audio tokenizer cancelled");
          return asr_tok_->Encode(
              text, ids,
              {.cancelled = cancelled, .recognize_special_tokens = special});
        },
        &prompt, &error);
    if (!status)
      return at(Error(error.http_status, error.code, status.message()));
    if (const auto* client = http->Client().Limits();
        client &&
        (prompt.tokens.size() > client->max_prompt_tokens ||
         prior->input > client->max_prompt_tokens - prompt.tokens.size()))
      return at(Error(400, "client_prompt_limit_exceeded",
                      "Audio preparation exceeds cumulative client input"));
    const auto reservation =
        reserved
            ? rate.ExtendPrompt(clients_, http->Client(), prompt.tokens.size())
            : rate.Reserve(clients_, http->Client(), prompt.tokens.size(),
                           public_output, ClientClock::now(), auxiliary_output,
                           output_budget);
    if (reservation != ClientRateResult::kAllowed) {
      const bool rpm = reservation == ClientRateResult::kRpm;
      const bool tpm = reservation == ClientRateResult::kTpm;
      const bool frozen = reservation == ClientRateResult::kFrozen;
      return at(Error(rpm || tpm ? 429
                      : frozen   ? 503
                                 : 500,
                      rpm      ? "client_rpm_exhausted"
                      : tpm    ? "client_tpm_exhausted"
                      : frozen ? "service_draining"
                               : "client_rate_invalid",
                      "Audio preparation quota unavailable"));
    }
    reserved = true;
    AsrTranscript transcript(false, "");
    reply = RunNativeAsr(fd, transcription, features, prompt, control,
                         diagnostic, rate, http->Usage(), &transcript);
    if (!reply.ok()) return at(std::move(reply));
    reply = inputs.Complete(i, transcript.Text());
    if (!reply.ok()) return at(std::move(reply));
    encoded_audio += prompt.audio_tokens;
  }
  const auto observed = http->Usage().Total();
  if (!observed || encoded_audio > observed->input)
    return Error(500, "chat_audio_usage_invalid", "Audio usage unavailable");
  reply = inputs.Render(request, access, output);
  if (!reply.ok()) return reply;
  *actual = *observed;
  *audio_tokens = encoded_audio;
  return {};
}
}  // namespace q4t::server
