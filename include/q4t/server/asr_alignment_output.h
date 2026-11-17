#pragma once

#include <span>
#include <string>
#include <string_view>

#include "q4t/audio/aligner_postprocess.h"
#include "q4t/server/asr_response.h"
#include "q4t/server/asr_statistics.h"

namespace q4t::server {
// Word-only verbose output uses actual duration and retains the full text.
// Segment confidence fields need their separate real evidence producer.
Status AlignedAsrWordsJson(std::string_view text, std::string_view language,
                           int64_t duration_ms,
                           std::span<const audio::AlignerWord> words,
                           const audio::AlignerTimes& times, std::string* out);

// One independently decoded clip is one segment. Its extent uses actual
// first/last word edges (the entire clip for punctuation-only text). seek is
// the decoder's real 10ms input-frame offset, zero for this single-clip path.
// No synthetic tokens, confidence or averaged temperature is permitted.
Status AlignedAsrSegmentsJson(const AsrTranscript& transcript,
                              int64_t duration_ms,
                              std::span<const audio::AlignerWord> words,
                              const audio::AlignerTimes& times,
                              const AsrCompression& compression,
                              bool include_words, std::string* out);

// Cues use actual first/last word boundaries. Punctuation, gaps and a 42-scalar
// target group words; no word's timestamp is synthesized or evenly spaced.
// The original text stays intact in verbose JSON. Subtitle markup and line
// framing are escaped/normalized for safe display of that text.
Status AlignedAsrSubtitle(std::string_view text, int64_t duration_ms,
                          std::span<const audio::AlignerWord> words,
                          const audio::AlignerTimes& times, bool vtt,
                          std::string* out, int64_t origin_ms = 0,
                          size_t first_cue = 1, bool header = true,
                          size_t* cue_count = nullptr);
}  // namespace q4t::server
