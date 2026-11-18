#pragma once

#include <functional>

#include "q4t/server/voice_registry.h"

namespace q4t::server {
// Called only after parsing and (for a voice) same-owner consent resolution.
// language is empty for voice samples. Output comes from independent audio
// execution, never data accepted from a public request.
using VoiceVerifier = std::function<ObjectReply(
    const VoiceAudio&, std::string_view language, VoiceSampleEvidence*,
    std::string* verified_phrase)>;
ObjectReply HandleVoiceRequest(ObjectStore& objects, std::string_view method,
                               std::string_view target,
                               std::string_view content_type,
                               std::string_view body,
                               const BinaryAccess& access,
                               const VoiceVerifier& verify);
}  // namespace q4t::server
