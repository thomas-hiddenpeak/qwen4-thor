#include "chat_server_internal.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/chat_audio_history.h"
#include "q4t/server/chat_audio_output.h"
#include "q4t/server/chat_choices.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::SynthesizeChatAudio(
    int fd, const ChatAudioOptions& options, uint64_t output_limit,
    std::span<const uint64_t> choice_tokens,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, ClientRateLease& rate,
    const std::function<bool(int, const std::string&, bool)>& delta,
    io::Json* completion) {
  using namespace object_detail;
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!completion || !http || !objects_ || !http->Client().Allowed() ||
      !options.enabled || !completion->GetArray("choices") ||
      completion->GetArray("choices")->array.size() != choice_tokens.size())
    return Error(503, "chat_audio_state_unavailable",
                 "Authenticated audio state unavailable");
  auto next = *completion;
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const BinaryAccess access{http->Client().Scoped()
                                ? "client:" + std::string(http->Client().Id())
                                : "local:anonymous",
                            control->deadline(), cancelled};
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Chat audio output cancelled");
  };
  auto& choices = Field(next, "choices").array;
  for (size_t index = 0; index < choices.size(); ++index) {
    auto& choice = choices[index];
    auto& message = Field(choice, "message");
    std::string transcript = message.GetString("content");
    if (transcript.empty()) transcript = message.GetString("refusal");
    // A tool-only turn or a blocked choice has no utterance to synthesize.
    if (transcript.empty() ||
        choice.GetString("finish_reason") == "content_filter")
      continue;
    SpeechRequest request = options.speech;
    request.input = transcript;
    request.model = kNativeTtsModel;
    const auto wire = Object(
        {{"model", String(request.model)},
         {"input", String(request.input)},
         {"voice", request.voice_id ? Object({{"id", String(request.voice)}})
                                    : String(request.voice)},
         {"response_format",
          String(std::string(SpeechFormatName(request.format)))}});
    ChatPlanError error;
    if (!ParseSpeechRequest(OutputJson(wire), &request, &error))
      return Error(502, "chat_audio_text_invalid",
                   "Generated utterance exceeds speech input contract");
    if (request.voice_id)
      return Error(404, "voice_not_found", "Voice is not registered");
    if (audio::TtsBuiltinVoiceId(request.voice) < 0)
      return Error(400, "invalid_speech_voice", "Voice is unavailable");
    auto loaded = EnsureNativeTts(false, access, &diagnostic);
    if (!loaded.ok()) return loaded;
    std::vector<uint32_t> tokens;
    {
      RequestDiagnostics::Scope wait(diagnostic,
                                     RequestDiagnostics::Wait::kTokenizer);
      auto lock = LockUnlessCancelled(tts_tok_mu_, cancelled);
      if (!lock.owns_lock() || cancelled()) return interrupted();
      const auto encoded = tts_tok_->Encode(
          request.input, &tokens,
          {.cancelled = cancelled, .recognize_special_tokens = false});
      if (!encoded)
        return cancelled() ? interrupted()
                           : Error(502, "chat_audio_text_invalid",
                                   "Generated utterance cannot be encoded");
    }
    audio::TtsPrompt prompt;
    const std::vector<int32_t> ids(tokens.begin(), tokens.end());
    // The complete utterance is available. Prefill all of its text, matching
    // CustomVoice's full-text input mode; PCM output remains incremental.
    const auto planned = audio::PlanTtsPrompt(
        audio::TtsVariant::kCustomVoice17B, ids, {}, request.voice, "auto",
        true, tts_limits_.max_prefill_tokens, &prompt);
    if (!planned ||
        prompt.text_ids.size() > size_t(tts_limits_.max_text_tokens))
      return Error(413, "chat_audio_prompt_capacity",
                   "Generated utterance exceeds synthesis capacity");
    // One per-choice public completion limit covers both primary generation
    // and actual audio codes. Input transcription remains preparation usage.
    const auto remaining = output_limit > choice_tokens[index]
                               ? output_limit - choice_tokens[index]
                               : 0;
    if (remaining < 2 * audio::TtsConfig::kCodeGroups + 1)
      return Error(502, "tts_output_limit",
                   "Completion budget exhausted before audio synthesis");
    const size_t frames =
        std::min<uint64_t>(tts_limits_.max_frames,
                           (remaining - 1) / audio::TtsConfig::kCodeGroups);
    const auto reserved =
        rate.ExtendPrompt(clients_, http->Client(), prompt.text_ids.size());
    if (reserved != ClientRateResult::kAllowed)
      return Error(reserved == ClientRateResult::kTpm      ? 429
                   : reserved == ClientRateResult::kFrozen ? 503
                                                           : 400,
                   reserved == ClientRateResult::kTpm ? "client_tpm_exhausted"
                   : reserved == ClientRateResult::kFrozen
                       ? "service_draining"
                       : "client_prompt_limit_exceeded",
                   "Chat audio prompt admission failed");
    const auto previous = http->Usage().Total();
    if (!previous)
      return Error(500, "tts_usage_invalid", "Prior Chat usage unavailable");
    const auto audio_id = NewObjectId(ObjectKind::kAudio);
    std::string event;
    if (!ChatAudioIdDelta(audio_id, &event))
      return Error(503, "request_identity_unavailable", "Audio id unavailable");
    if (delta && !delta(index, event, false)) return interrupted();
    if (!ChatAudioTranscriptDelta(transcript, &event))
      return Error(502, "chat_audio_text_invalid", "Invalid audio transcript");
    if (delta && !delta(index, event, false)) return interrupted();
    std::string bytes;
    ChatAudioDataStream audio_stream;
    std::vector<std::string> events;
    const auto sink = [&](std::string_view part) -> ObjectReply {
      if (cancelled()) return interrupted();
      if (part.size() > ChatChoices::kResponseLimit * 3 / 4 - bytes.size())
        return Error(413, "chat_audio_output_capacity",
                     "Audio exceeds response budget");
      bytes += part;
      if (delta) {
        if (!audio_stream.Append(part, &events))
          return Error(502, "chat_audio_data_invalid",
                       "Invalid encoded audio chunk");
        for (const auto& item : events)
          if (!delta(index, item, false)) return interrupted();
      }
      return {};
    };
    SpeechEncodingReceipt receipt;
    auto reply = RunNativeTts(fd, request, prompt, frames, control, diagnostic,
                              rate, http->Usage(), sink, &receipt);
    if (!reply.ok()) return reply;
    if (delta) {
      if (!audio_stream.Finish(&events))
        return Error(502, "chat_audio_data_invalid",
                     "Invalid audio stream end");
      for (const auto& item : events)
        if (!delta(index, item, false)) return interrupted();
    }
    const auto actual = http->Usage().Total();
    if (!actual || actual->input < previous->input ||
        actual->output < previous->output || receipt.bytes != bytes.size() ||
        receipt.sha256 != Digest(bytes))
      return Error(502, "tts_usage_invalid", "Chat audio receipt mismatch");
    const auto created = std::time(nullptr);
    const auto expires = created + kChatAudioLifetimeSeconds;
    Json encoded;
    auto status =
        AttachChatAudio(message, options, audio_id, expires, bytes, transcript,
                        ChatChoices::kResponseLimit, &encoded);
    if (status) {
      message = std::move(encoded);
      status = AddChatAudioUsage(
          {actual->input - previous->input, actual->output - previous->output},
          receipt.input_samples / audio::TtsConfig::kSamplesPerFrame *
              audio::TtsConfig::kCodeGroups,
          &Field(next, "usage"));
    }
    if (!status || OutputJson(next).size() > ChatChoices::kResponseLimit)
      return Error(413, "chat_audio_output_capacity",
                   "Audio response exceeds capacity");
    {
      RequestDiagnostics::Scope wait(diagnostic,
                                     RequestDiagnostics::Wait::kStorage);
      reply = PublishChatAudioHistory(*objects_, access, audio_id, created,
                                      transcript, request.format, receipt);
    }
    if (!reply.ok()) return reply;
    if (cancelled()) return interrupted();
    if (!ChatAudioExpiryDelta(expires, &event))
      return Error(500, "chat_audio_expiry_invalid",
                   "Invalid audio completion");
    if (delta && !delta(index, event, true)) return interrupted();
  }
  *completion = std::move(next);
  return {};
}
}  // namespace q4t::server
