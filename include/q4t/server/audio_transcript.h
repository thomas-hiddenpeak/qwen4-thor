// Source-bound representation of actual ASR output for the text model.
#pragma once

#include <string>
#include <string_view>

namespace q4t::server {

// Preserve modality and part boundaries without prescribing the answer.
// The caller supplies native ASR output, never a client reference transcript.
std::string RenderAudioTranscript(std::string_view source,
                                  std::string_view transcript);

}  // namespace q4t::server
