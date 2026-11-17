#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/status.h"

namespace q4t::audio {
struct AlignerWord {
  std::string text;
  size_t source_begin = 0;
  size_t source_end = 0;
};
struct AlignerWordLimits {
  size_t max_bytes = 64 * 1024;
  size_t max_words = 2048;
};

// Retain source byte offsets while cleaning only the alignment model's text.
// Ordinary languages match the trained space/CJK policy. ja/ko use the linked
// ICU dictionary word boundaries, an explicit native segmentation policy.
Status SplitAlignerWords(std::string_view text, std::string_view language,
                         const AlignerWordLimits& limits,
                         std::vector<AlignerWord>* out,
                         const std::function<bool()>& cancelled = {});

struct AlignerTimes {
  std::vector<int64_t> raw_ms;
  std::vector<int64_t> fixed_ms;
  size_t repaired = 0;
  size_t clipped = 0;
};

// Convert actual 5000-class predictions to 80ms units; repair nonmonotonic
// predictions using the reference's stable longest-subsequence anchors.
// Never invent raw predictions. Only a final one-class quantization overrun
// may be clipped to duration; larger overruns fail. Output is atomic.
Status RepairAlignerTimestamps(std::span<const uint32_t> classes, size_t words,
                               int64_t duration_ms, AlignerTimes* out,
                               const std::function<bool()>& cancelled = {});
}  // namespace q4t::audio
