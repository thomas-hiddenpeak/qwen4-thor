#include "chat_server_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/file_audio.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/server/voice_verification.h"
#include "q4t/text/voice_consent.h"

namespace q4t::server {
ObjectReply ChatServer::PrepareVoiceEvidence(
    int fd, const VoiceAudio& audio, std::string_view consent_language,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, ClientRateLease& rate,
    VoiceSampleEvidence* evidence, std::string* verified_phrase) {
  using Clock = std::chrono::steady_clock;
  using object_detail::Error;
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!evidence || !verified_phrase || !http || !http->Client().Allowed())
    return Error(503, "object_scope_unavailable", "Voice owner unavailable");
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Voice verification cancelled");
  };
  const BinaryAccess access{http->Client().Scoped()
                                ? "client:" + std::string(http->Client().Id())
                                : "local:anonymous",
                            control->deadline(), cancelled};
  const auto phrase = VoiceConsentPhrase(consent_language);
  if (!consent_language.empty() && phrase.empty())
    return Error(400, "voice_consent_language",
                 "Consent language has no phrase");
  if (tts_base_model_dir_.empty() || asr_model_dir_.empty() ||
      speaker_model_dir_.empty() || vad_model_dir_.empty())
    return Error(503, "voice_verification_not_configured",
                 "Voice verification assets are not configured");
  // Both original-rate decodes, uploaded body copies and frontend buffers
  // coexist in Thor unified RAM. Per-model GPU allocations are separately
  // admitted under the existing sequence/auxiliary ownership discipline.
  constexpr uint64_t kPending =
      (24ULL << 20) + 60 * (16000 + 24000) * sizeof(float) + (32ULL << 20);
  if (!runtime::ReadSystemMemory().CanAllocate(kPending))
    return Error(503, "voice_resource_exhausted",
                 "Voice host memory unavailable");
  if (cancelled()) return interrupted();
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kStorage);
    auto reply = EnsureNativeAsr(false, access, &diagnostic);
    if (reply.ok()) reply = EnsureNativeSpeaker(false, access, &diagnostic);
    if (!reply.ok()) return reply;
  }
  FileAudioLimits audio_limits;
  audio_limits.max_source_bytes = kVoiceAudioBytes;
  audio_limits.max_duration_ns = 60000000000ULL;
  audio_limits.max_output_samples = 60 * 16000;
  FileAudioPcm pcm;
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kMedia);
    auto reply = DecodeAudioContainer(audio.bytes, audio_limits, access, &pcm);
    if (!reply.ok()) return reply;
  }
  std::vector<audio::VadChunk> chunks;
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kMedia);
    std::shared_ptr<const audio::SileroVad> model;
    auto reply = EnsureNativeVad(access, &model);
    if (!reply.ok()) return reply;
    audio::VadChunkOptions options;
    options.max_samples = 60 * 16000;
    options.max_chunk_samples =
        std::min(30, asr_limits_.max_audio_seconds) * 16000;
    options.max_chunks = vad_max_chunks_;
    std::vector<audio::VadFrame> frames;
    const auto scored =
        model->Score(pcm.samples, options.max_samples, &frames, cancelled);
    if (!scored) {
      if (cancelled()) return interrupted();
      vad_health_.ComputeFailed(AsrFailure::kCompute, Clock::now());
      return Error(503, "vad_compute_failed", scored.message());
    }
    const auto split = audio::SplitVadChunks(frames, pcm.samples.size(),
                                             options, &chunks, cancelled);
    if (cancelled()) return interrupted();
    if (!split) return Error(400, "vad_chunk_limit", split.message());
    vad_health_.ComputeSucceeded();
  }
  if (chunks.empty())
    return Error(400, "voice_no_speech",
                 "Recording contains no detected speech");
  VoiceSampleEvidence next;
  next.source_sha256 = object_detail::Digest(audio.bytes);
  next.duration_ns = pcm.duration_ns;
  AsrServiceLimits limits = asr_limits_;
  if (const auto* client = http->Client().Limits())
    limits.max_output_tokens =
        std::min<uint64_t>(limits.max_output_tokens, client->max_output_tokens);
  const auto output_budget = uint64_t(limits.max_output_tokens);
  // No original phrase, sample transcript or language hint reaches ASR.
  AsrRequest request;
  request.model = audio::AsrConfig::kModelId;
  for (const auto& chunk : chunks) {
    if (cancelled()) return interrupted();
    audio::AsrFeatures features;
    auto status = audio::ComputeAsrFeatures(
        std::span(pcm.samples)
            .subspan(chunk.begin_sample, chunk.end_sample - chunk.begin_sample),
        16000,
        {size_t(limits.max_audio_seconds) * 16000,
         size_t(limits.max_audio_seconds) * 100},
        &features, cancelled);
    if (!status)
      return cancelled()
                 ? interrupted()
                 : Error(400, "audio_features_invalid", status.message());
    const auto prior = http->Usage().Total();
    if (!prior || prior->output >= output_budget)
      return Error(502, "asr_output_limit",
                   "Voice verification output exhausted");
    limits.max_output_tokens = output_budget - prior->output;
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
    if (!status)
      return cancelled()
                 ? interrupted()
                 : Error(error.http_status, error.code, status.message());
    const uint64_t input =
        prompt.tokens.size() + prompt.no_speech_prefix.size();
    const auto admitted = rate.ExtendPrompt(clients_, http->Client(), input);
    if (admitted != ClientRateResult::kAllowed)
      return Error(admitted == ClientRateResult::kTpm      ? 429
                   : admitted == ClientRateResult::kFrozen ? 503
                                                           : 400,
                   admitted == ClientRateResult::kTpm ? "client_tpm_exhausted"
                   : admitted == ClientRateResult::kFrozen
                       ? "service_draining"
                       : "client_prompt_limit_exceeded",
                   "Voice verification quota unavailable");
    AsrTranscript transcript(false, {}, false);
    auto reply = RunNativeAsr(fd, request, features, prompt, control,
                              diagnostic, rate, http->Usage(), &transcript);
    if (!reply.ok()) return reply;
    if (!next.transcript.empty() && !transcript.Text().empty())
      next.transcript += ' ';
    next.transcript += transcript.Text();
  }
  if (next.transcript.empty())
    return Error(400, "voice_no_speech", "Recording has no recognized speech");
  std::string matched;
  if (!consent_language.empty()) {
    bool matches = false;
    const auto compared =
        text::MatchVoiceConsentTranscript(next.transcript, phrase, &matches);
    if (!compared || !matches)
      return Error(400, "voice_consent_mismatch",
                   "Recording does not match the complete consent phrase");
    matched = phrase;
  }
  std::vector<audio::SpeakerWindow> windows;
  const auto planned =
      audio::PlanSpeakerWindows(chunks, pcm.samples.size(),
                                {size_t(speaker_limits_.window_ms) * 16,
                                 size_t(speaker_limits_.step_ms) * 16,
                                 size_t(speaker_limits_.max_windows)},
                                &windows, cancelled);
  if (!planned)
    return cancelled() ? interrupted()
                       : Error(400, "voice_speaker_limit", planned.message());
  std::vector<std::span<const float>> clips;
  for (const auto& window : windows)
    clips.push_back(std::span(pcm.samples)
                        .subspan(window.begin_sample,
                                 window.end_sample - window.begin_sample));
  std::vector<audio::SpeakerEmbedding> identities;
  auto reply = RunNativeSpeaker(fd, clips, control, diagnostic, &identities);
  if (!reply.ok()) return reply;
  auto identified = BuildVoiceIdentity(windows, identities, pcm.samples.size(),
                                       &next.identity, cancelled);
  if (!identified)
    return cancelled()
               ? interrupted()
               : Error(400, "voice_speaker_mismatch", identified.message());
  // Consent keeps only the independent identity. A reusable voice condition
  // comes from the original upload at 24k, never upsampled ASR PCM or a preset.
  if (consent_language.empty()) {
    FileAudioPcm original;
    audio_limits.output_sample_rate = 24000;
    audio_limits.max_output_samples = 60 * 24000;
    {
      RequestDiagnostics::Scope waiting(diagnostic,
                                        RequestDiagnostics::Wait::kMedia);
      reply =
          DecodeAudioContainer(audio.bytes, audio_limits, access, &original);
    }
    if (!reply.ok()) return reply;
    next.duration_ns = original.duration_ns;
    reply = RunNativeVoiceCondition(fd, original.samples, control, diagnostic,
                                    &next.condition, &next.reference_codes);
    if (!reply.ok()) return reply;
    next.reference_samples = original.samples.size();
  }
  if (cancelled()) return interrupted();
  *evidence = std::move(next);
  *verified_phrase = std::move(matched);
  return {};
}
}  // namespace q4t::server
