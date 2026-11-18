// Evidence from independently decoded audio, never caller-provided vectors.
#pragma once

#include <string_view>

#include "q4t/audio/diarization.h"

namespace q4t::server {
// Fixed interoperable consent phrases; language subtags select the same
// primary-language wording. The phrase is never supplied to the recognizer.
// Matching is a recorded protocol check, not proof of a person's legal
// identity.
std::string_view VoiceConsentPhrase(std::string_view language);
// Normalize each window, require pairwise single-speaker consistency, and
// average by the non-overlapping speech cores. Invalid/multi-speaker input
// fails atomically, without forcing unrelated speakers into one identity.
Status BuildVoiceIdentity(std::span<const audio::SpeakerWindow> windows,
                          std::span<const audio::SpeakerEmbedding> embeddings,
                          size_t total_samples, std::vector<float>* identity,
                          const std::function<bool()>& cancelled = {});
}  // namespace q4t::server
