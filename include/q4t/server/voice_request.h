// Voice creation and consent input contracts. No model, filesystem or authority
// is selected by parsing. Callers must verify real audio before registration.
#pragma once

#include <cstddef>
#include <string>
#include <string_view>

#include "q4t/server/chat_plan_error.h"
#include "q4t/status.h"

namespace q4t::server {
inline constexpr size_t kVoiceAudioBytes = 10ULL << 20;
inline constexpr size_t kVoiceFormBytes = kVoiceAudioBytes + 64 * 1024;
enum class VoiceType { kAudioSample, kPrompt };
struct VoiceAudio {
  std::string bytes, filename, content_type;
};
struct VoiceCreateRequest {
  VoiceType type = VoiceType::kAudioSample;
  std::string name, consent, prompt, model = "auto", script_hint;
  VoiceAudio audio_sample;
};
struct VoiceConsentRequest {
  std::string name, language;
  VoiceAudio recording;
};

// RFC 5646 well-formedness, including grandfathered tags, private use and
// duplicate variants/singletons. This does not claim registry/model support.
bool WellFormedVoiceLanguage(std::string_view tag);
Status ParseVoiceCreateRequest(std::string_view content_type,
                               std::string_view body, VoiceCreateRequest* out,
                               ChatPlanError* error);
Status ParseVoiceConsentRequest(std::string_view content_type,
                                std::string_view body, VoiceConsentRequest* out,
                                ChatPlanError* error);
Status ParseVoiceConsentUpdate(std::string_view content_type,
                               std::string_view body, std::string* name,
                               ChatPlanError* error);
}  // namespace q4t::server
