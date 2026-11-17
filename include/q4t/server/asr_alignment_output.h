#pragma once

#include <span>
#include <string>
#include <string_view>

#include "q4t/audio/aligner_postprocess.h"

namespace q4t::server {
// Word-only verbose output uses actual duration and retains the full text.
// Segment confidence fields need their separate real evidence producer.
Status AlignedAsrWordsJson(std::string_view text, std::string_view language,
                           int64_t duration_ms,
                           std::span<const audio::AlignerWord> words,
                           const audio::AlignerTimes& times, std::string* out);

// Cues use actual first/last word boundaries. Punctuation, gaps and a 42-scalar
// target group words; no word's timestamp is synthesized or evenly spaced.
// The original text stays intact in verbose JSON. Subtitle markup and line
// framing are escaped/normalized for safe display of that text.
Status AlignedAsrSubtitle(std::string_view text, int64_t duration_ms,
                          std::span<const audio::AlignerWord> words,
                          const audio::AlignerTimes& times, bool vtt,
                          std::string* out);
}  // namespace q4t::server
