// Unicode cleaning and timestamp repair adapted from Qwen3ForceAlignProcessor.
// Copyright 2026 The Alibaba Qwen team. Apache-2.0; source, license and native
// segmentation differences are recorded in third_party/licenses/qwen3-asr.
#include "q4t/audio/aligner_postprocess.h"

#include <unicode/ubrk.h>
#include <unicode/uchar.h>
#include <unicode/utext.h>
#include <unicode/utf8.h>

#include <algorithm>
#include <limits>
#include <memory>
#include <utility>

#include "q4t/audio/aligner_config.h"

namespace q4t::audio {
namespace {
struct Character {
  UChar32 value;
  size_t first, end;
};
bool Kept(UChar32 c) {
  const auto type = u_charType(c);
  return c == '\'' || (type >= U_UPPERCASE_LETTER && type <= U_OTHER_LETTER) ||
         (type >= U_DECIMAL_DIGIT_NUMBER && type <= U_OTHER_NUMBER);
}
bool Cjk(UChar32 c) {
  return (c >= 0x4e00 && c <= 0x9fff) || (c >= 0x3400 && c <= 0x4dbf) ||
         (c >= 0x20000 && c <= 0x2a6df) || (c >= 0x2a700 && c <= 0x2b73f) ||
         (c >= 0x2b740 && c <= 0x2b81f) || (c >= 0x2b820 && c <= 0x2ceaf) ||
         (c >= 0xf900 && c <= 0xfaff);
}
bool Space(UChar32 c) { return u_isUWhiteSpace(c) || (c >= 0x1c && c <= 0x1f); }
Status Cancelled() { return Status::Fail("alignment processing cancelled"); }
}  // namespace

Status SplitAlignerWords(std::string_view text, std::string_view language,
                         const AlignerWordLimits& limits,
                         std::vector<AlignerWord>* out,
                         const std::function<bool()>& cancelled) {
  if (!out || text.size() > limits.max_bytes || text.size() > 64 * 1024 ||
      !limits.max_words || limits.max_words > 2048)
    return Status::Fail("alignment word/byte budget exceeded");
  if (cancelled && cancelled()) return Cancelled();
  std::vector<Character> characters;
  characters.reserve(text.size());
  const auto bytes = reinterpret_cast<const uint8_t*>(text.data());
  const int length = static_cast<int>(text.size());
  for (int i = 0; i < length;) {
    const int first = i;
    UChar32 value;
    U8_NEXT(bytes, i, length, value);
    if (value < 0) return Status::Fail("invalid alignment UTF-8 text");
    characters.push_back(
        {value, static_cast<size_t>(first), static_cast<size_t>(i)});
    if ((characters.size() & 63) == 0 && cancelled && cancelled())
      return Cancelled();
  }
  std::vector<AlignerWord> words;
  AlignerWord current;
  bool full = false;
  const auto flush = [&] {
    if (current.text.empty()) return;
    if (words.size() == limits.max_words) {
      full = true;
      return;
    }
    words.push_back(std::move(current));
    current = {};
  };
  const auto append = [&](const Character& c) {
    if (current.text.empty()) current.source_begin = c.first;
    current.source_end = c.end;
    current.text.append(text.substr(c.first, c.end - c.first));
  };
  const bool dictionary = language == "ja" || language == "Japanese" ||
                          language == "ko" || language == "Korean";
  if (dictionary && !text.empty()) {
    const char* locale =
        language == "ja" || language == "Japanese" ? "ja" : "ko";
    UErrorCode error = U_ZERO_ERROR;
    std::unique_ptr<UBreakIterator, decltype(&ubrk_close)> iterator(
        ubrk_open(UBRK_WORD, locale, nullptr, 0, &error), &ubrk_close);
    UText source = UTEXT_INITIALIZER;
    utext_openUTF8(&source, text.data(), length, &error);
    if (U_SUCCESS(error)) ubrk_setUText(iterator.get(), &source, &error);
    utext_close(&source);  // Iterator owns a shallow clone; text stays alive.
    if (U_FAILURE(error) || !iterator)
      return Status::Fail("native alignment word iterator unavailable");
    size_t at = 0;
    (void)ubrk_first(iterator.get());
    for (int end = ubrk_next(iterator.get()); end != UBRK_DONE;
         end = ubrk_next(iterator.get())) {
      if (cancelled && cancelled()) return Cancelled();
      while (at < characters.size() &&
             characters[at].end <= static_cast<size_t>(end)) {
        if (Kept(characters[at].value)) append(characters[at]);
        ++at;
      }
      flush();
      if (full) break;
    }
  } else {
    for (const auto& c : characters) {
      if (Space(c.value))
        flush();
      else if (Cjk(c.value)) {
        flush();
        append(c);
        flush();
      } else if (Kept(c.value))
        append(c);
      if (full) break;
    }
    flush();
  }
  if (full) return Status::Fail("alignment word count exceeds budget");
  if (cancelled && cancelled()) return Cancelled();
  *out = std::move(words);
  return {};
}

Status RepairAlignerTimestamps(std::span<const uint32_t> classes, size_t words,
                               int64_t duration_ms, AlignerTimes* out,
                               const std::function<bool()>& cancelled) {
  if (!out || words > 2048 || classes.size() != words * 2 || duration_ms <= 0)
    return Status::Fail("alignment timestamp shape/duration differs");
  if (cancelled && cancelled()) return Cancelled();
  AlignerTimes result;
  for (const auto value : classes) {
    if (value >= AlignerConfig::kClasses)
      return Status::Fail("alignment timestamp class outside trained head");
    const int64_t ms = value * AlignerConfig::kTimestampMilliseconds;
    if (ms > duration_ms &&
        ms - duration_ms > AlignerConfig::kTimestampMilliseconds)
      return Status::Fail("alignment prediction exceeds audio duration");
    result.raw_ms.push_back(ms);
  }
  result.fixed_ms = result.raw_ms;
  const int n = static_cast<int>(classes.size());
  std::vector<int> length(n, 1), parent(n, -1);
  int longest = 0;
  for (int i = 1; i < n; ++i) {
    if (cancelled && cancelled()) return Cancelled();
    for (int j = 0; j < i; ++j)
      if (result.raw_ms[j] <= result.raw_ms[i] && length[j] + 1 > length[i]) {
        length[i] = length[j] + 1;
        parent[i] = j;
      }
    if (length[i] > length[longest]) longest = i;
  }
  std::vector<bool> anchor(n, false);
  if (n)
    for (int i = longest; i != -1; i = parent[i]) anchor[i] = true;
  for (int first = 0; first < n;) {
    if (cancelled && cancelled()) return Cancelled();
    if (anchor[first]) {
      ++first;
      continue;
    }
    int end = first;
    while (end < n && !anchor[end]) ++end;
    const bool left = first > 0, right = end < n;
    const auto a = left ? result.fixed_ms[first - 1] : result.fixed_ms[end];
    const auto b = right ? result.fixed_ms[end] : a;
    for (int i = first; i < end; ++i) {
      if (!left)
        result.fixed_ms[i] = b;
      else if (!right)
        result.fixed_ms[i] = a;
      else if (end - first <= 2)
        result.fixed_ms[i] = i - first + 1 <= end - i ? a : b;
      else
        result.fixed_ms[i] = static_cast<int64_t>(
            a +
            (static_cast<double>(b - a) / (end - first + 1)) * (i - first + 1));
    }
    first = end;
  }
  for (int i = 0; i < n; ++i) {
    if (result.fixed_ms[i] != result.raw_ms[i]) ++result.repaired;
    if (result.fixed_ms[i] > duration_ms) {
      result.fixed_ms[i] = duration_ms;
      ++result.clipped;
    }
  }
  *out = std::move(result);
  return {};
}
}  // namespace q4t::audio
