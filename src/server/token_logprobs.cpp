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
  std::string token;
  auto status = decode(sample.token, &token);
  if (status) status = Record(sample.logprob, token, &entry.json);
  if (!status) return status;
  entry.json += ",\"top_logprobs\":[";
  for (size_t i = 0; i < sample.top.size(); ++i) {
    std::string item;
    status = decode(sample.top[i].token, &token);
    if (status) status = Record(sample.top[i].logprob, token, &item);
    if (!status) return status;
    if (i) entry.json += ',';
    entry.json += item + '}';
  }
  entry.json += "]}";
  const auto bytes = entry.json.size() + sizeof(Entry);
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
  std::string entries;
  for (auto& entry : pending_) {
    if (!entry.mapped || entry.emitted || entry.end <= delta.source_begin ||
        entry.begin >= delta.source_end)
      continue;
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
  *json = Wrap(entries);
  return Status();
}

void TokenLogprobs::DiscardBefore(size_t offset) {
  while (!pending_.empty()) {
    const auto& entry = pending_.front();
    if (!entry.mapped || entry.mapped_end < entry.raw_end || entry.end > offset)
      break;
    queued_bytes_ -= entry.json.size() + sizeof(Entry);
    pending_.pop_front();
  }
}

std::string TokenLogprobs::Json() const { return Wrap(collected_); }

}  // namespace q4t::server
