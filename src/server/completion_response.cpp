#include "q4t/server/completion_response.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <map>

#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
std::string Quote(std::string_view text) {
  return '"' + detail::JsonEscape(std::string(text)) + '"';
}
Status Probability(double value, std::string* out) {
  if (!std::isfinite(value) || value > 0)
    return Status::Fail("invalid completion probability");
  char bytes[64];
  const auto result = std::to_chars(
      bytes, bytes + sizeof(bytes), value, std::chars_format::general,
      std::numeric_limits<double>::max_digits10);
  if (result.ec != std::errc())
    return Status::Fail("cannot serialize completion probability");
  out->assign(bytes, result.ptr);
  return Status();
}
std::string Envelope(std::string_view id, std::string_view model,
                      int64_t created) {
  return "{\"id\":" + Quote(id) +
         ",\"object\":\"text_completion\",\"created\":" +
         std::to_string(created) + ",\"model\":" + Quote(model);
}
std::string Choice(int index, std::string_view text, std::string_view finish,
                    std::string_view logprobs) {
  return "{\"index\":" + std::to_string(index) + ",\"text\":" + Quote(text) +
         ",\"finish_reason\":" + (finish.empty() ? "null" : Quote(finish)) +
         ",\"logprobs\":" + std::string(logprobs) + '}';
}
std::string Stream(std::string body, bool obfuscate) {
  if (obfuscate) {
    // Randomized padding to the next 128-byte bucket plus one random bucket.
    // This is optional transport padding, not an encryption replacement.
    const auto random = detail::NewRequestId("");
    const size_t padding = 128 - ((body.size() + 18) % 128) +
                           ((random[0] & 1) ? 128 : 0);
    std::string value;
    while (value.size() < padding) value += detail::NewRequestId("");
    value.resize(padding);
    body += ",\"obfuscation\":" + Quote(value);
  }
  return "data: " + body + "}\r\n\r\n";
}
size_t Storage(const CompletionCandidate& candidate) {
  return candidate.text.size() + candidate.logprobs.size() +
         candidate.finish.size() + sizeof(candidate);
}
}  // namespace

size_t Utf8Characters(std::string_view text) {
  return std::count_if(text.begin(), text.end(), [](unsigned char c) {
    return (c & 0xc0) != 0x80;
  });
}

Status CompletionEchoOffsets(std::string_view bytes,
                              std::span<const size_t> token_bytes,
                              std::string* text,
                              std::vector<size_t>* character_offsets) {
  character_offsets->clear();
  detail::Utf8Stream utf8;
  std::vector<detail::Utf8Span> spans, tail;
  *text = utf8.Append(bytes, &spans);
  *text += utf8.Finish(&tail);
  spans.insert(spans.end(), tail.begin(), tail.end());
  size_t span = 0, characters = 0, previous = 0;
  for (size_t start : token_bytes) {
    if (start < previous || start >= bytes.size())
      return Status::Fail("echo token byte offset outside prompt");
    previous = start;
    while (span < spans.size() && spans[span].input_end <= start) {
      characters += Utf8Characters(std::string_view(*text).substr(
          spans[span].output_begin,
          spans[span].output_end - spans[span].output_begin));
      ++span;
    }
    if (span == spans.size() || spans[span].input_begin > start)
      return Status::Fail("echo token offset is not mapped");
    character_offsets->push_back(characters);
  }
  return Status();
}

Status CompletionProbability(int32_t token, const SampledToken* sample,
                              const CompletionTokenDecoder& decode,
                              CompletionProbabilityRow* row) {
  *row = {};
  std::string bytes;
  auto status = decode(token, &bytes);
  if (!status) return status;
  row->token = Quote(detail::NormalizeUtf8(bytes));
  row->logprob = row->top = "null";
  if (!sample) return Status();
  if (sample->token != token)
    return Status::Fail("completion probability token does not match");
  status = Probability(sample->raw_logprob, &row->logprob);
  if (!status) return status;
  std::map<std::string, double> top;
  // Include the selected token even when it is outside the requested top-k.
  top[row->token] = sample->raw_logprob;
  for (const auto& value : sample->top) {
    status = decode(value.token, &bytes);
    if (!status) return status;
    std::string number;
    status = Probability(value.logprob, &number);
    if (!status) return status;
    const auto label = Quote(detail::NormalizeUtf8(bytes));
    const auto [position, inserted] = top.emplace(label, value.logprob);
    if (!inserted)
      position->second = std::max(position->second, value.logprob);
  }
  row->top = "{";
  for (const auto& [label, value] : top) {
    std::string number;
    status = Probability(value, &number);
    if (!status) return status;
    if (row->top.size() != 1) row->top += ',';
    row->top += label + ':' + number;
  }
  row->top += '}';
  return Status();
}

Status CompletionProbabilityRows::Add(const CompletionProbabilityRow& row,
                                       size_t character_offset) {
  if (row.token.empty() || row.logprob.empty() || row.top.empty())
    return Status::Fail("missing completion probability value");
  const auto offset = std::to_string(character_offset);
  const size_t added = row.Bytes() + offset.size() + (count_ ? 4 : 0);
  if (added > kLimit || bytes_ > kLimit - added)
    return Status::Fail("completion probabilities exceed 16 MiB storage");
  const std::string* values[] = {&row.token, &row.logprob, &row.top, &offset};
  for (size_t i = 0; i < columns_.size(); ++i) {
    if (count_) columns_[i] += ',';
    columns_[i] += *values[i];
  }
  bytes_ += added;
  ++count_;
  return Status();
}

Status CompletionProbabilityRows::Append(const CompletionProbabilityRows& other) {
  if (!other.count_) return Status();
  const size_t added = other.bytes_ + (count_ ? 4 : 0);
  if (added > kLimit || bytes_ > kLimit - added)
    return Status::Fail("completion probabilities exceed 16 MiB storage");
  for (size_t i = 0; i < columns_.size(); ++i) {
    if (count_) columns_[i] += ',';
    columns_[i] += other.columns_[i];
  }
  bytes_ += added;
  count_ += other.count_;
  return Status();
}

std::string CompletionProbabilityRows::Json() const {
  return "{\"tokens\":[" + columns_[0] + "],\"token_logprobs\":[" + columns_[1] +
         "],\"top_logprobs\":[" + columns_[2] + "],\"text_offset\":[" +
         columns_[3] + "]}";
}

Status CompletionChoices::Add(int index, uint64_t prompt,
                               CompletionCandidate candidate) {
  if (prompts_ < 1 || n_ < 1 || best_of_ < n_ ||
      uint64_t(prompts_) * best_of_ > 128 || index != accepted_ ||
      index >= prompts_ * best_of_ || !prompt ||
      (index % best_of_ != 0 && prompt != group_prompt_) ||
      (stream_ && best_of_ != n_) ||
      !std::isfinite(candidate.sum_logprob) || candidate.sum_logprob > 0 ||
      candidate.score_tokens > candidate.tokens ||
      (best_of_ > n_ && candidate.score_tokens != candidate.tokens) ||
      (candidate.finish != "stop" && candidate.finish != "length" &&
       candidate.finish != "content_filter") ||
      prompt > std::numeric_limits<uint64_t>::max() - prompt_ ||
      candidate.tokens > std::numeric_limits<uint64_t>::max() - completion_ ||
      prompt_ + prompt >
          std::numeric_limits<uint64_t>::max() - completion_ - candidate.tokens)
    return Status::Fail("invalid completion choice accounting");
  if (!stream_) {
    if (selected_.empty()) selected_.resize(prompts_);
    auto& group = selected_[index / best_of_];
    candidate.ordinal = index % best_of_;
    // For n==best_of there is no selection/reordering, including all n seeds.
    const auto better = [&](const auto& a, const auto& b) {
      if (best_of_ > n_ && a.Score() != b.Score()) return a.Score() > b.Score();
      return a.ordinal < b.ordinal;
    };
    auto position = std::lower_bound(group.begin(), group.end(), candidate,
                                     better);
    if (group.size() < static_cast<size_t>(n_) || position != group.end()) {
      const size_t removed = group.size() == static_cast<size_t>(n_)
                                 ? Storage(group.back()) : 0;
      const size_t added = Storage(candidate);
      if (added > kLimit || bytes_ - removed > kLimit - added)
        return Status::Fail("completion response exceeds 32 MiB storage");
      bytes_ += added;
      group.insert(position, std::move(candidate));
      if (group.size() > static_cast<size_t>(n_)) {
        bytes_ -= Storage(group.back());
        group.pop_back();
      }
    }
  }
  if (index % best_of_ == 0) {
    prompt_ += prompt;
    group_prompt_ = prompt;
  }
  // Integral counters remain intact after moving the text/JSON payload.
  completion_ += candidate.tokens;
  ++accepted_;
  return Status();
}

std::string CompletionChoices::UsageJson() const {
  return "{\"prompt_tokens\":" + std::to_string(prompt_) +
         ",\"completion_tokens\":" + std::to_string(completion_) +
         ",\"total_tokens\":" + std::to_string(prompt_ + completion_) + '}';
}

Status CompletionChoices::Json(std::string_view id, std::string_view model,
                                int64_t created, std::string* output) const {
  output->clear();
  if (stream_ || accepted_ != prompts_ * best_of_ || selected_.empty())
    return Status::Fail("completion candidates are not ready");
  std::string json = Envelope(id, model, created) + ",\"choices\":[";
  int index = 0;
  for (const auto& group : selected_) {
    for (const auto& candidate : group) {
      if (index) json += ',';
      json += Choice(index++, candidate.text, candidate.finish,
                       candidate.logprobs);
      if (json.size() > kLimit)
        return Status::Fail("completion response exceeds 32 MiB storage");
    }
  }
  json += "],\"usage\":" + UsageJson() + '}';
  if (json.size() > kLimit)
    return Status::Fail("completion response exceeds 32 MiB storage");
  *output = std::move(json);
  return Status();
}

std::string CompletionChunk(std::string_view id, std::string_view model,
                             int64_t created, int index, std::string_view text,
                             std::string_view finish, std::string_view logprobs,
                             bool include_usage, bool obfuscate) {
  return Stream(Envelope(id, model, created) + ",\"choices\":[" +
                    Choice(index, text, finish, logprobs) + ']' +
                    (include_usage ? ",\"usage\":null" : ""), obfuscate);
}

std::string CompletionUsageChunk(std::string_view id, std::string_view model,
                                  int64_t created, std::string_view usage,
                                  bool obfuscate) {
  return Stream(Envelope(id, model, created) +
                    ",\"choices\":[],\"usage\":" + std::string(usage),
                obfuscate);
}

}  // namespace q4t::server
