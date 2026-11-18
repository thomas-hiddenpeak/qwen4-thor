#pragma once

#include <string_view>

#include "q4t/status.h"

namespace q4t::text {
// Compare independently recognized speech with the server's trusted consent
// phrase. Case, canonical Unicode, punctuation and whitespace may differ;
// letters, numbers, combining marks, order and negation must remain intact.
// The expected phrase must never be fed to ASR or sourced from the HTTP body.
// Matching text alone does not verify audio, identity or permission to publish.
// Invalid input/library failure leaves matches unchanged; a valid nonmatch is
// successful comparison with false. No model or filesystem operations occur.
Status MatchVoiceConsentTranscript(std::string_view transcript,
                                   std::string_view expected, bool* matches);
}  // namespace q4t::text
