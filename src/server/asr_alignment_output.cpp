#include "q4t/server/asr_alignment_output.h"

#include <algorithm>
#include <array>
#include <cstdio>

#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
Status Validate(std::string_view text, int64_t duration,
                std::span<const audio::AlignerWord> words,
                const audio::AlignerTimes& times, const std::string* out) {
  if (!out || text.size() > 64 * 1024 || words.size() > 2048 || duration <= 0 ||
      duration > 400000 || times.fixed_ms.size() != words.size() * 2 ||
      times.raw_ms.size() != times.fixed_ms.size() ||
      detail::NormalizeUtf8(text) != text)
    return Status::Fail("alignment output shape or text invalid");
  size_t end = 0;
  int64_t previous = 0;
  const auto boundary = [&](size_t at) {
    return at == text.size() ||
           (static_cast<unsigned char>(text[at]) & 0xc0) != 0x80;
  };
  for (size_t i = 0; i < words.size(); ++i) {
    const auto& word = words[i];
    if (word.text.empty() || word.source_begin < end ||
        word.source_begin >= word.source_end || word.source_end > text.size() ||
        !boundary(word.source_begin) || !boundary(word.source_end) ||
        detail::NormalizeUtf8(word.text) != word.text)
      return Status::Fail("alignment word source span invalid");
    end = word.source_end;
    for (size_t j = 2 * i; j < 2 * i + 2; ++j) {
      if (times.fixed_ms[j] < previous || times.fixed_ms[j] > duration ||
          times.raw_ms[j] < 0 || times.raw_ms[j] > 399920)
        return Status::Fail("alignment output time range invalid");
      previous = times.fixed_ms[j];
    }
  }
  // A punctuation-only or empty transcript legitimately has no word slots.
  return {};
}
std::string Quote(std::string_view value) {
  return '"' + detail::JsonEscape(std::string(value)) + '"';
}
std::string Seconds(int64_t ms) {
  char value[32];
  std::snprintf(value, sizeof(value), "%lld.%03lld",
                static_cast<long long>(ms / 1000),
                static_cast<long long>(ms % 1000));
  return value;
}
std::string Stamp(int64_t ms, bool vtt) {
  char value[32];
  std::snprintf(value, sizeof(value), "%02lld:%02lld:%02lld%c%03lld",
                static_cast<long long>(ms / 3600000),
                static_cast<long long>((ms / 60000) % 60),
                static_cast<long long>((ms / 1000) % 60), vtt ? '.' : ',',
                static_cast<long long>(ms % 1000));
  return value;
}
std::string SubtitleText(std::string_view source) {
  std::string text;
  for (unsigned char c : source) {
    if (c == '<')
      text += "&lt;";
    else if (c == '>')
      text += "&gt;";
    else if (c == '&')
      text += "&amp;";
    else if (c < 0x20 || c == 0x7f)
      text += ' ';
    else
      text += static_cast<char>(c);
  }
  return text;
}
bool SentenceEnd(std::string_view gap) {
  for (const auto mark :
       std::array<std::string_view, 6>{".", "!", "?", "。", "！", "？"})
    if (gap.find(mark) != gap.npos) return true;
  return false;
}
}  // namespace

Status AlignedAsrWordsJson(std::string_view text, std::string_view language,
                           int64_t duration_ms,
                           std::span<const audio::AlignerWord> words,
                           const audio::AlignerTimes& times, std::string* out) {
  if (auto s = Validate(text, duration_ms, words, times, out); !s) return s;
  if (language.size() > 64 || detail::NormalizeUtf8(language) != language)
    return Status::Fail("alignment output language invalid");
  std::string result = R"({"text":)" + Quote(text) + R"(,"language":)" +
                       Quote(language) + R"(,"duration":)" +
                       Seconds(duration_ms) + R"(,"words":[)";
  for (size_t i = 0; i < words.size(); ++i) {
    if (i) result += ',';
    result += R"({"word":)" + Quote(words[i].text) + R"(,"start":)" +
              Seconds(times.fixed_ms[i * 2]) + R"(,"end":)" +
              Seconds(times.fixed_ms[i * 2 + 1]) + "}";
  }
  result += R"(],"usage":{"type":"duration","seconds":)" +
            Seconds(duration_ms) + "}}";
  *out = std::move(result);
  return {};
}

Status AlignedAsrSubtitle(std::string_view text, int64_t duration_ms,
                          std::span<const audio::AlignerWord> words,
                          const audio::AlignerTimes& times, bool vtt,
                          std::string* out) {
  if (auto s = Validate(text, duration_ms, words, times, out); !s) return s;
  struct Cue {
    size_t first, end, source_begin, source_end;
  };
  std::vector<Cue> cues;
  size_t first = 0, source_begin = 0, characters = 0;
  for (size_t i = 0; i < words.size(); ++i) {
    const size_t source_end =
        i + 1 == words.size() ? text.size() : words[i + 1].source_begin;
    const size_t from = i == first ? source_begin : words[i].source_begin;
    for (unsigned char c : text.substr(from, source_end - from))
      characters += (c & 0xc0) != 0x80;
    const bool last = i + 1 == words.size();
    const bool gap =
        !last && times.fixed_ms[(i + 1) * 2] - times.fixed_ms[i * 2 + 1] >= 600;
    if (last || gap || characters >= 42 ||
        SentenceEnd(text.substr(words[i].source_end,
                                source_end - words[i].source_end))) {
      // A zero-duration group can only borrow real neighboring word edges.
      if (times.fixed_ms[i * 2 + 1] > times.fixed_ms[first * 2]) {
        cues.push_back({first, i + 1, source_begin, source_end});
        first = i + 1;
        source_begin = source_end;
        characters = 0;
      }
    }
  }
  if (first < words.size()) {
    if (cues.empty())
      return Status::Fail("alignment has no positive-duration subtitle cue");
    auto& last = cues.back();
    last.end = words.size();
    last.source_end = text.size();
  }
  std::string result = vtt ? "WEBVTT\n\n" : "";
  for (size_t i = 0; i < cues.size(); ++i) {
    const auto& cue = cues[i];
    result += std::to_string(i + 1) + '\n' +
              Stamp(times.fixed_ms[cue.first * 2], vtt) + " --> " +
              Stamp(times.fixed_ms[cue.end * 2 - 1], vtt) + '\n' +
              SubtitleText(text.substr(cue.source_begin,
                                       cue.source_end - cue.source_begin)) +
              "\n\n";
  }
  *out = std::move(result);
  return {};
}
}  // namespace q4t::server
