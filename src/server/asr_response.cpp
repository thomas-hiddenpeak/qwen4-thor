#include "q4t/server/asr_response.h"

#include <algorithm>
#include <cmath>

#include "q4t/audio/asr_config.h"
#include "q4t/server/asr_request.h"

namespace q4t::server {
void AsrTemperature::Observe(double logprob) {
  if (!adaptive_ || increases_ == 5) return;
  sum_ += logprob;
  if (++count_ != 8) return;
  if (sum_ / count_ < -1.0) temperature_ = ++increases_ * 0.2;
  sum_ = 0;
  count_ = 0;
}
namespace {
constexpr size_t kMaxTextBytes = 4ULL << 20;
constexpr std::string_view kTextMarker = "<asr_text>";
constexpr std::string_view kLanguagePrefix = "language ";
bool Space(char c) {
  return c == ' ' || c == '\n' || c == '\r' || c == '\t' || c == '\v' ||
         c == '\f';
}
std::string_view Strip(std::string_view s) {
  while (!s.empty() && Space(s.front())) s.remove_prefix(1);
  while (!s.empty() && Space(s.back())) s.remove_suffix(1);
  return s;
}
std::string Quote(const std::string& s) {
  return '"' + detail::JsonEscape(s) + '"';
}
}  // namespace

AsrTranscript::AsrTranscript(bool logprobs, std::string forced_language,
                             bool statistics)
    : logprobs_(logprobs),
      body_(!forced_language.empty()),
      statistics_(statistics),
      language_(std::move(forced_language)),
      probabilities_(true, false, 0, true, TokenLogprobFormat::kTranscription) {
}

Status AsrTranscript::Fail(std::string message) {
  if (status_) status_ = Status::Fail(std::move(message));
  return status_;
}
Status AsrTranscript::Append(const SampledToken& token, std::string_view bytes,
                             AsrTextDelta* delta, double selected_temperature) {
  if (!delta) return Fail("Missing transcription delta owner");
  *delta = {};
  if (!status_) return status_;
  if (complete_) return Fail("Transcription already complete");
  if (bytes.size() > kMaxTextBytes - raw_bytes_)
    return Fail("Transcription exceeds byte budget");
  if (statistics_) {
    if (selected_tokens_ >= 8192 || token.token < 0 ||
        token.token >= audio::AsrConfig::kVocab ||
        !std::isfinite(token.raw_logprob) || token.raw_logprob > 0 ||
        !std::isfinite(selected_temperature) || selected_temperature < 0 ||
        selected_temperature > 1)
      return Fail("Invalid or excessive transcription token statistics");
    ++selected_tokens_;
    if (!bytes.empty()) {
      PendingToken item;
      item.value = {token.token, token.raw_logprob, selected_temperature};
      item.raw_begin = raw_bytes_;
      item.raw_end = raw_bytes_ + bytes.size();
      pending_tokens_.push_back(item);
    }
  }
  raw_bytes_ += bytes.size();
  if (logprobs_) {
    auto status =
        probabilities_.Push(token, bytes, [&](int32_t, std::string* output) {
          output->assign(bytes);
          return Status{};
        });
    if (!status) return Fail(status.message());
  }
  std::vector<detail::Utf8Span> spans;
  pending_ += utf8_.Append(bytes, logprobs_ || statistics_ ? &spans : nullptr);
  if (pending_.size() + text_.size() > kMaxTextBytes)
    return Fail("Normalized transcription exceeds byte budget");
  if (logprobs_) probabilities_.Map(spans);
  if (statistics_) MapStatistics(spans);
  return Consume(false, delta);
}

Status AsrTranscript::Consume(bool final, AsrTextDelta* delta) {
  const auto discard = [&](size_t n) {
    pending_.erase(0, n);
    offset_ += n;
    if (logprobs_) probabilities_.DiscardBefore(offset_);
    if (statistics_) DiscardStatistics(offset_);
  };
  if (!body_) {
    size_t leading = 0;
    while (leading < pending_.size() && Space(pending_[leading])) ++leading;
    discard(leading);
    const std::string_view s = pending_;
    const auto marker = s.find(kTextMarker);
    if (marker != s.npos) {
      auto meta = Strip(s.substr(0, marker));
      if (meta.starts_with(kLanguagePrefix)) {
        meta.remove_prefix(kLanguagePrefix.size());
        const auto name = Strip(meta);
        language_ = AsrLanguageCode(name);
        if (name != "None" && language_.empty())
          return Fail("Unknown language in native ASR header");
      } else if (!meta.empty()) {
        return Fail("Invalid native ASR header");
      }
      discard(marker + kTextMarker.size());
      body_ = true;
    } else if (kLanguagePrefix.starts_with(s) || kTextMarker.starts_with(s)) {
      if (!final) return {};
      body_ = true;  // Ordinary short text with no header delimiter.
    } else if (s.starts_with(kLanguagePrefix)) {
      if (s.size() > 512 || final) return Fail("Incomplete native ASR header");
      return {};
    } else {
      body_ = true;  // Official text-only output is also valid.
    }
  }
  if (!text_started_) {
    size_t leading = 0;
    while (leading < pending_.size() && Space(pending_[leading])) ++leading;
    discard(leading);
  }
  size_t visible = pending_.size();
  while (visible && Space(pending_[visible - 1])) --visible;
  if (visible) {
    ResponseDelta item;
    item.text = pending_.substr(0, visible);
    item.source_begin = offset_;
    item.source_end = offset_ + visible;
    if (logprobs_) {
      const auto status = probabilities_.Emit(item, &delta->logprobs);
      if (!status) return Fail(status.message());
    }
    delta->text = std::move(item.text);
    if (statistics_)
      EmitStatistics(item.source_begin, item.source_end, text_.size());
    text_ += delta->text;
    text_started_ = true;
    discard(visible);
  }
  if (final) discard(pending_.size());
  return {};
}

Status AsrTranscript::Finish(AsrTextDelta* delta) {
  if (!delta) return Fail("Missing transcription delta owner");
  *delta = {};
  if (!status_) return status_;
  if (complete_) return Fail("Transcription already complete");
  std::vector<detail::Utf8Span> spans;
  pending_ += utf8_.Finish(logprobs_ || statistics_ ? &spans : nullptr);
  if (logprobs_) probabilities_.Map(spans);
  if (statistics_) MapStatistics(spans);
  const auto status = Consume(true, delta);
  if (status) complete_ = true;
  return status;
}

Status AsrTranscript::SetNoSpeechProbability(double probability) {
  if (!status_) return status_;
  if (!statistics_ || complete_ || no_speech_ || !std::isfinite(probability) ||
      probability < 0 || probability > 1)
    return Fail("Invalid or duplicate transcription no-speech posterior");
  no_speech_ = probability;
  return {};
}

void AsrTranscript::MapStatistics(std::span<const detail::Utf8Span> spans) {
  for (const auto& span : spans) {
    auto it = std::lower_bound(
        pending_tokens_.begin(), pending_tokens_.end(), span.input_begin,
        [](const PendingToken& t, size_t begin) { return t.raw_end <= begin; });
    for (; it != pending_tokens_.end() && it->raw_begin < span.input_end;
         ++it) {
      if (!it->mapped) it->begin = span.output_begin;
      it->end = span.output_end;
      it->mapped_end = std::min(it->raw_end, span.input_end);
      it->mapped = true;
    }
  }
}

void AsrTranscript::EmitStatistics(size_t begin, size_t end,
                                   size_t text_offset) {
  for (auto& item : pending_tokens_) {
    if (!item.mapped || item.end <= begin || item.begin >= end) continue;
    const size_t last = text_offset + std::min(item.end, end) - begin;
    if (item.emitted != std::numeric_limits<size_t>::max()) {
      text_tokens_[item.emitted].end = last;
      continue;
    }
    item.emitted = text_tokens_.size();
    auto value = item.value;
    value.begin = text_offset + std::max(item.begin, begin) - begin;
    value.end = last;
    text_tokens_.push_back(value);
  }
}

void AsrTranscript::DiscardStatistics(size_t offset) {
  while (!pending_tokens_.empty()) {
    const auto& item = pending_tokens_.front();
    if (!item.mapped || item.mapped_end < item.raw_end || item.end > offset)
      break;
    pending_tokens_.pop_front();
  }
}

std::string AsrTranscript::DeltaJson(const AsrTextDelta& delta) const {
  return R"({"type":"transcript.text.delta","delta":)" + Quote(delta.text) +
         (logprobs_ ? R"(,"logprobs":)" + delta.logprobs : "") + "}";
}
Status AsrTranscript::ResultJson(const ActualUsage& usage, uint64_t audio,
                                 bool stream, std::string* json) const {
  if (!json || !status_ || !complete_ || !ValidActualUsage(usage) ||
      audio > usage.input)
    return Status::Fail("Incomplete or unaccounted transcription response");
  std::string result =
      stream ? R"({"type":"transcript.text.done","text":)" : R"({"text":)";
  result += Quote(text_);
  if (!language_.empty())
    result += R"(,"languages":[{"code":)" + Quote(language_) + "}]";
  if (logprobs_) result += R"(,"logprobs":)" + probabilities_.Json();
  result += R"(,"usage":{"type":"tokens","input_tokens":)" +
            std::to_string(usage.input) + R"(,"output_tokens":)" +
            std::to_string(usage.output) + R"(,"total_tokens":)" +
            std::to_string(usage.input + usage.output) +
            R"(,"input_token_details":{"audio_tokens":)" +
            std::to_string(audio) + R"(,"text_tokens":)" +
            std::to_string(usage.input - audio) + "}}}";
  *json = std::move(result);
  return {};
}
}  // namespace q4t::server
