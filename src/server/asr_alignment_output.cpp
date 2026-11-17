#include "q4t/server/asr_alignment_output.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdio>

#include "q4t/server/asr_request.h"
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
std::string Number(double value) {
  char buffer[64];
  const auto result = std::to_chars(buffer, buffer + sizeof(buffer), value);
  return std::string(buffer, result.ptr);
}
std::string WordRows(std::span<const audio::AlignerWord> words,
                     const audio::AlignerTimes& times) {
  std::string result = "[";
  for (size_t i = 0; i < words.size(); ++i) {
    if (i) result += ',';
    result += R"({"word":)" + Quote(words[i].text) + R"(,"start":)" +
              Seconds(times.fixed_ms[i * 2]) + R"(,"end":)" +
              Seconds(times.fixed_ms[i * 2 + 1]) + "}";
  }
  return result + "]";
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
                       Seconds(duration_ms) + R"(,"words":)" +
                       WordRows(words, times);
  result +=
      R"(,"usage":{"type":"duration","seconds":)" + Seconds(duration_ms) + "}}";
  *out = std::move(result);
  return {};
}

Status AlignedAsrSegmentsJson(const AsrTranscript& transcript,
                              int64_t duration_ms,
                              std::span<const audio::AlignerWord> words,
                              const audio::AlignerTimes& times,
                              const AsrCompression& compression,
                              bool include_words, std::string* out) {
  const auto& text = transcript.Text();
  if (auto s = Validate(text, duration_ms, words, times, out); !s) return s;
  const auto tokens = transcript.TextTokens();
  const auto silence = transcript.NoSpeechProbability();
  if (!transcript.Complete() || !silence ||
      compression.text_bytes != text.size() || !compression.compressed_bytes ||
      compression.library_version.empty() ||
      compression.library_sha256.size() != 64 || tokens.empty() != text.empty())
    return Status::Fail("segment actual statistics unavailable");
  double mean = 0;
  size_t covered = 0;
  std::string ids = "[";
  for (const auto& token : tokens) {
    if (token.id < 0 || !std::isfinite(token.logprob) || token.logprob > 0 ||
        !std::isfinite(token.temperature) || token.temperature < 0 ||
        token.temperature > 1 ||
        token.temperature != tokens.front().temperature ||
        token.begin > covered || token.end <= token.begin ||
        token.end > text.size())
      return Status::Fail("segment token observation invalid");
    mean += token.logprob / tokens.size();
    covered = std::max(covered, token.end);
    if (ids.size() > 1) ids += ',';
    ids += std::to_string(token.id);
  }
  if (covered != text.size())
    return Status::Fail("segment tokens do not cover actual text");
  ids += ']';
  std::string result = R"({"text":)" + Quote(text) + R"(,"language":)" +
                       Quote(AsrLanguageName(transcript.Language())) +
                       R"(,"duration":)" + Seconds(duration_ms) +
                       R"(,"segments":[)";
  if (!text.empty()) {
    const auto start = words.empty() ? 0 : times.fixed_ms.front();
    const auto end = words.empty() ? duration_ms : times.fixed_ms.back();
    result += R"({"id":0,"seek":0,"start":)" + Seconds(start) + R"(,"end":)" +
              Seconds(end) + R"(,"text":)" + Quote(text) + R"(,"tokens":)" +
              ids + R"(,"temperature":)" + Number(tokens.front().temperature) +
              R"(,"avg_logprob":)" + Number(mean) + R"(,"compression_ratio":)" +
              Number(compression.Ratio()) + R"(,"no_speech_prob":)" +
              Number(*silence) + "}";
  }
  result += ']';
  if (include_words) result += R"(,"words":)" + WordRows(words, times);
  result +=
      R"(,"usage":{"type":"duration","seconds":)" + Seconds(duration_ms) + "}}";
  *out = std::move(result);
  return {};
}

Status AlignedAsrSubtitle(std::string_view text, int64_t duration_ms,
                          std::span<const audio::AlignerWord> words,
                          const audio::AlignerTimes& times, bool vtt,
                          std::string* out, int64_t origin_ms, size_t first_cue,
                          bool header, size_t* cue_count) {
  if (auto s = Validate(text, duration_ms, words, times, out); !s) return s;
  if (origin_ms < 0 || origin_ms > 600000 - duration_ms || !first_cue ||
      first_cue > 65536)
    return Status::Fail("subtitle clip origin or cue identity invalid");
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
  if (cues.size() > 65537 - first_cue)
    return Status::Fail("subtitle cue identity budget exceeded");
  std::string result = vtt && header ? "WEBVTT\n\n" : "";
  for (size_t i = 0; i < cues.size(); ++i) {
    const auto& cue = cues[i];
    result += std::to_string(i + first_cue) + '\n' +
              Stamp(origin_ms + times.fixed_ms[cue.first * 2], vtt) + " --> " +
              Stamp(origin_ms + times.fixed_ms[cue.end * 2 - 1], vtt) + '\n' +
              SubtitleText(text.substr(cue.source_begin,
                                       cue.source_end - cue.source_begin)) +
              "\n\n";
  }
  *out = std::move(result);
  if (cue_count) *cue_count = cues.size();
  return {};
}
}  // namespace q4t::server
