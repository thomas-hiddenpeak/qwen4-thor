#include "chat_server_internal.h"

#include "object_protocol.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/server/voice_registry.h"

namespace q4t::server {
ObjectReply ChatServer::PlanNativeSpeech(const SpeechRequest& request,
                                         const BinaryAccess& access,
                                         RequestDiagnostics& diagnostic,
                                         audio::TtsPrompt* prompt) {
  using object_detail::Error;
  if (!prompt) return Error(500, "tts_prompt_invalid", "Speech owner missing");
  const bool base = request.voice_id;
  const auto variant =
      base ? audio::TtsVariant::kBase17B : audio::TtsVariant::kCustomVoice17B;
  if ((base ? tts_base_model_dir_ : tts_model_dir_).empty() ||
      !(base ? tts_base_catalog_ : tts_catalog_).Contains(request.model))
    return Error(404, "model_not_found", "Speech model is not configured");
  const auto cancelled = [&] {
    return stop_requested_.load() ||
           std::chrono::steady_clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  VoiceSynthesisBinding voice;
  {
    RequestDiagnostics::Scope wait(diagnostic,
                                   RequestDiagnostics::Wait::kStorage);
    // Every invocation checks the live same-owner consent. The returned
    // immutable condition belongs only to this bounded request.
    if (base) {
      if (!objects_)
        return Error(503, "object_store_unavailable",
                     "Voice store unavailable");
      auto reply =
          VoiceRegistry(*objects_).Resolve(request.voice, access, &voice);
      if (!reply.ok()) return reply;
    } else if (audio::TtsBuiltinVoiceId(request.voice) < 0) {
      return Error(400, "invalid_speech_voice", "Voice is unavailable");
    }
    auto loaded = EnsureNativeTts(false, access, &diagnostic, variant);
    if (!loaded.ok()) return loaded;
  }
  std::vector<uint32_t> text, instruction, reference;
  {
    RequestDiagnostics::Scope wait(diagnostic,
                                   RequestDiagnostics::Wait::kTokenizer);
    auto lock = LockUnlessCancelled(tts_tok_mu_, cancelled);
    if (!lock.owns_lock() || cancelled())
      return Error(409, "tts_cancelled", "Speech tokenization cancelled");
    auto& tokenizer = base ? tts_base_tok_ : tts_tok_;
    auto status = tokenizer->Encode(
        request.input, &text,
        {.cancelled = cancelled, .recognize_special_tokens = false});
    if (status && !request.instructions.empty())
      status = tokenizer->Encode(
          request.instructions, &instruction,
          {.cancelled = cancelled, .recognize_special_tokens = false});
    if (status && !voice.reference_codes.empty())
      status = tokenizer->Encode(
          voice.reference_transcript, &reference,
          {.cancelled = cancelled, .recognize_special_tokens = false});
    if (!status)
      return Error(cancelled() ? 409 : 400,
                   cancelled() ? "tts_cancelled" : "invalid_speech_text",
                   "Speech tokenization failed");
  }
  const std::vector<int32_t> ids(text.begin(), text.end());
  const std::vector<int32_t> instructions(instruction.begin(),
                                          instruction.end());
  const std::vector<int32_t> reference_ids(reference.begin(), reference.end());
  audio::TtsPrompt next;
  Status status;
  if (base && !voice.reference_codes.empty()) {
    status = audio::PlanTtsBaseIclPrompt(
        ids, reference_ids, voice.reference_codes, instructions,
        voice.condition, "auto", true, tts_limits_.max_prefill_tokens, &next);
  } else if (base) {
    status =
        audio::PlanTtsBasePrompt(ids, instructions, voice.condition, "auto",
                                 true, tts_limits_.max_prefill_tokens, &next);
  } else {
    status =
        audio::PlanTtsPrompt(variant, ids, instructions, request.voice, "auto",
                             true, tts_limits_.max_prefill_tokens, &next);
  }
  if (!status || next.text_ids.size() > size_t(tts_limits_.max_text_tokens))
    return Error(400, "invalid_speech_prompt",
                 status ? "Speech prompt exceeds capacity" : status.message());
  if (cancelled())
    return Error(409, "tts_cancelled", "Speech preparation cancelled");
  *prompt = std::move(next);
  return {};
}
}  // namespace q4t::server
