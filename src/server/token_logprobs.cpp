#include "q4t/server/token_logprobs.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>

namespace q4t::server {
namespace {
std::string Wrap(std::string_view entries) {
  return "{\"content\":[" + std::string(entries) + "],\"refusal\":null}";
}

Status Record(double logprob, const std::string& bytes, std::string* json) {
  if (!std::isfinite(logprob) || logprob > 0)
    return Status::Fail("invalid token log probability");
  char number[64];
  const auto result = std::to_chars(number, number + sizeof(number), logprob,
                                    std::chars_format::general,
                                    std::numeric_limits<double>::max_digits10);
  if (result.ec != std::errc())
    return Status::Fail("cannot serialize token probability");
  *json = "{\"token\":\"" + detail::JsonEscape(detail::NormalizeUtf8(bytes)) +
          "\",\"logprob\":" + std::string(number, result.ptr) + ",\"bytes\":[";
  bool first = true;
  for (const unsigned char byte : bytes) {
    if (!first) *json += ',';
    first = false;
    *json += std::to_string(byte);
  }
  *json += ']';
  return Status();
}
}  // namespace

Status TokenLogprobs::Push(const SampledToken& sample,
                           std::string_view decoded_bytes,
                           const Decode& decode) {
  // Skip non-emitting special tokens, but never pretend they were sampled
  // from content. Their accounting is still in completion_tokens.
  if (decoded_bytes.empty()) return Status();
  Entry entry;
  entry.raw_begin = raw_bytes_;
  entry.raw_end = raw_bytes_ + decoded_bytes.size();
  if (completion_) {
    auto status = CompletionProbability(sample.token, &sample, decode,
                                         &entry.completion);
    if (!status) return status;
  }
  std::string token;
  auto status = completion_ ? Status() : decode(sample.token, &token);
  if (status && !completion_)
    status = Record(exact_selected_ ? sample.raw_logprob : sample.logprob,
                      token, &entry.json);
  if (!status) return status;
  if (!completion_) entry.json += ",\"top_logprobs\":[";
  for (size_t i = 0; !completion_ && i < sample.top.size(); ++i) {
    std::string item;
    status = decode(sample.top[i].token, &token);
    if (status) status = Record(sample.top[i].logprob, token, &item);
    if (!status) return status;
    if (i) entry.json += ',';
    entry.json += item + '}';
  }
  if (!completion_) entry.json += "]}";
  const auto bytes = entry.json.size() + entry.completion.Bytes() +
                     sizeof(Entry);
  if (bytes > kStorageLimit || queued_bytes_ > kStorageLimit - bytes)
    return Status::Fail("token logprobs exceed 16 MiB pending storage");
  raw_bytes_ = entry.raw_end;
  queued_bytes_ += bytes;
  pending_.push_back(std::move(entry));
  return Status();
}

void TokenLogprobs::Map(std::span<const detail::Utf8Span> spans) {
  for (const auto& span : spans) {
    auto it =
        std::lower_bound(pending_.begin(), pending_.end(), span.input_begin,
                         [](const Entry& entry, size_t begin) {
                           return entry.raw_end <= begin;
                         });
    for (; it != pending_.end() && it->raw_begin < span.input_end; ++it) {
      if (!it->mapped) it->begin = span.output_begin;
      it->end = span.output_end;
      it->mapped_end = std::min(it->raw_end, span.input_end);
      it->mapped = true;
    }
  }
}

Status TokenLogprobs::Emit(const ResponseDelta& delta, std::string* json) {
  *json = "null";
  if (delta.kind != ResponseDelta::Kind::kContent) return Status();
  if (completion_ && (delta.source_begin != emitted_bytes_ ||
                      delta.source_end < delta.source_begin ||
                      delta.source_end - delta.source_begin != delta.text.size()))
    return Status::Fail("completion probability text offsets are discontinuous");
  CompletionProbabilityRows rows;
  size_t offset = 0;
  size_t characters = emitted_characters_;
  std::string entries;
  for (auto& entry : pending_) {
    if (!entry.mapped || entry.emitted || entry.end <= delta.source_begin ||
        entry.begin >= delta.source_end)
      continue;
    if (completion_) {
      const size_t begin = entry.begin <= delta.source_begin
                               ? 0 : entry.begin - delta.source_begin;
      if (begin < offset || begin > delta.text.size())
        return Status::Fail("completion token offsets are out of order");
      characters += Utf8Characters(std::string_view(delta.text).substr(
          offset, begin - offset));
      offset = begin;
      auto status = rows.Add(entry.completion, characters);
      if (!status) return status;
      if (collect_) {
        status = completion_rows_.Add(entry.completion, characters);
        if (!status) return status;
      }
      entry.emitted = true;
      continue;
    }
    if (collect_) {
      const auto added = entry.json.size() + (collected_.empty() ? 0 : 1);
      if (added > kStorageLimit || collected_.size() > kStorageLimit - added)
        return Status::Fail("token logprobs exceed 16 MiB collected storage");
      if (!collected_.empty()) collected_ += ',';
      collected_ += entry.json;
    }
    if (!entries.empty()) entries += ',';
    entries += entry.json;
    entry.emitted = true;
  }
  if (completion_) {
    emitted_characters_ += Utf8Characters(delta.text);
    emitted_bytes_ = delta.source_end;
    *json = rows.Json();
  } else {
    *json = Wrap(entries);
  }
  return Status();
}

void TokenLogprobs::DiscardBefore(size_t offset) {
  while (!pending_.empty()) {
    const auto& entry = pending_.front();
    if (!entry.mapped || entry.mapped_end < entry.raw_end || entry.end > offset)
      break;
    queued_bytes_ -= entry.json.size() + entry.completion.Bytes() + sizeof(Entry);
    pending_.pop_front();
  }
}

std::string TokenLogprobs::Json() const {
  return completion_ ? completion_rows_.Json() : Wrap(collected_);
}

}  // namespace q4t::server
