#include "q4t/server/chat_choices.h"

#include <limits>

#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
std::string Quote(std::string_view text) {
  return '"' + detail::JsonEscape(std::string(text)) + '"';
}
bool Adds(uint64_t first, uint64_t second) {
  return second <= std::numeric_limits<uint64_t>::max() - first;
}
}  // namespace

Status ChatChoices::Add(int index, std::string_view message,
                        std::string_view finish, std::string_view logprobs,
                        uint64_t prompt, uint64_t completion, uint64_t cached,
                        bool report_cached, int64_t reasoning_limit,
                        uint64_t reasoning, uint64_t forced) {
  if (count_ < 1 || count_ > 128 || index != accepted_ || index >= count_ ||
      (index && prompt != prompt_) || cached > prompt ||
      !Adds(reasoning, forced) || reasoning + forced > completion ||
      !Adds(completion_, completion) ||
      !Adds(prompt, completion_ + completion) || !Adds(reasoning_, reasoning) ||
      !Adds(forced_, forced))
    return Status::Fail("invalid completion choice accounting");
  if (finish != "stop" && finish != "length" && finish != "tool_calls" &&
      finish != "content_filter")
    return Status::Fail("invalid completion choice finish reason");
  if (!stream_) {
    std::string choice = (index ? "," : "") + std::string("{\"index\":") +
                         std::to_string(index) +
                         ",\"message\":" + std::string(message) +
                         ",\"finish_reason\":" + Quote(finish) +
                         ",\"logprobs\":" + std::string(logprobs) + '}';
    if (choice.size() > kResponseLimit ||
        choices_.size() > kResponseLimit - choice.size())
      return Status::Fail("completion exceeds 32 MiB response storage");
    choices_ += choice;
  }
  if (!index) {
    prompt_ = prompt;
    cached_ = cached;
    report_cached_ = report_cached;
    reasoning_limit_ = reasoning_limit;
  }
  completion_ += completion;
  reasoning_ += reasoning;
  forced_ += forced;
  ++accepted_;
  return Status();
}

std::string ChatChoices::UsageJson() const {
  std::string json =
      "{\"prompt_tokens\":" + std::to_string(prompt_) +
      ",\"completion_tokens\":" + std::to_string(completion_) +
      ",\"total_tokens\":" + std::to_string(prompt_ + completion_);
  if (report_cached_)
    json += ",\"prompt_tokens_details\":{\"cached_tokens\":" +
            std::to_string(cached_) + '}';
  if (reasoning_limit_ >= 0) {
    json += ",\"completion_tokens_details\":{\"reasoning_tokens\":" +
            std::to_string(reasoning_ + forced_) +
            "},\"q4t_thinking_budget\":{\"limit\":" +
            std::to_string(reasoning_limit_) +
            ",\"consumed\":" + std::to_string(reasoning_) +
            ",\"forced_closure_tokens\":" + std::to_string(forced_) + '}';
  }
  return json + '}';
}

Status ChatChoices::Json(std::string_view id, std::string_view model,
                         int64_t created, std::string* output) const {
  output->clear();
  if (!Complete() || stream_)
    return Status::Fail("completion choices are not ready");
  *output =
      "{\"id\":" + Quote(id) +
      ",\"object\":\"chat.completion\",\"created\":" + std::to_string(created) +
      ",\"model\":" + Quote(model) + ",\"choices\":[" + choices_ +
      "],\"usage\":" + UsageJson() + '}';
  if (output->size() > kResponseLimit) {
    output->clear();
    return Status::Fail("completion exceeds 32 MiB response storage");
  }
  return Status();
}

}  // namespace q4t::server
