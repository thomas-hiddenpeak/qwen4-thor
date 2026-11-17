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

Status ChatChoices::SetAudioPreparation(const ActualUsage& usage,
                                        uint64_t audio_tokens) {
  if (accepted_ || audio_prepared_ || !ValidActualUsage(usage) ||
      !usage.input || !audio_tokens || audio_tokens > usage.input ||
      usage.cached || usage.cache_write || usage.reasoning)
    return Status::Fail("invalid audio preparation usage");
  audio_preparation_ = usage;
  audio_tokens_ = audio_tokens;
  audio_prepared_ = true;
  return {};
}

Status ChatChoices::Add(int index, std::string_view message,
                        std::string_view finish, std::string_view logprobs,
                        uint64_t prompt, uint64_t completion, uint64_t cached,
                        bool report_cached, int64_t reasoning_limit,
                        uint64_t reasoning, uint64_t forced, bool prediction,
                        uint64_t prediction_accepted,
                        uint64_t prediction_rejected) {
  if (count_ < 1 || count_ > 128 || index != accepted_ || index >= count_ ||
      (index && prompt != prompt_) || cached > prompt ||
      !Adds(reasoning, forced) || reasoning + forced > completion ||
      !Adds(completion_, completion) ||
      !Adds(prompt, completion_ + completion) || !Adds(reasoning_, reasoning) ||
      !Adds(prompt + completion_ + completion,
             audio_preparation_.input + audio_preparation_.output) ||
      !Adds(forced_, forced) ||
      (!prediction && (prediction_accepted || prediction_rejected)) ||
      (index && prediction != prediction_) ||
      !Adds(prediction_accepted, prediction_rejected) ||
      prediction_accepted + prediction_rejected > completion ||
      !Adds(prediction_accepted_, prediction_accepted) ||
      !Adds(prediction_rejected_, prediction_rejected))
    return Status::Fail("invalid completion choice accounting");
  if (finish != "stop" && finish != "length" && finish != "tool_calls" &&
      finish != "function_call" &&
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
    prediction_ = prediction;
  }
  completion_ += completion;
  reasoning_ += reasoning;
  forced_ += forced;
  prediction_accepted_ += prediction_accepted;
  prediction_rejected_ += prediction_rejected;
  ++accepted_;
  return Status();
}

std::string ChatChoices::UsageJson() const {
  std::string json =
      "{\"prompt_tokens\":" +
      std::to_string(prompt_ + audio_preparation_.input) +
      ",\"completion_tokens\":" +
      std::to_string(completion_ + audio_preparation_.output) +
      ",\"total_tokens\":" +
      std::to_string(prompt_ + completion_ + audio_preparation_.input +
                     audio_preparation_.output);
  if (report_cached_ || full_usage_ || audio_prepared_) {
    json += ",\"prompt_tokens_details\":{\"cached_tokens\":" +
            std::to_string(cached_);
    if (audio_prepared_)
      json += ",\"audio_tokens\":" + std::to_string(audio_tokens_);
    json += '}';
  }
  if (reasoning_limit_ >= 0 || prediction_ || full_usage_) {
    json += ",\"completion_tokens_details\":{\"reasoning_tokens\":" +
            std::to_string(reasoning_ + forced_);
    if (prediction_)
      json += ",\"accepted_prediction_tokens\":" +
              std::to_string(prediction_accepted_) +
              ",\"rejected_prediction_tokens\":" +
              std::to_string(prediction_rejected_);
    json += '}';
  }
  if (reasoning_limit_ >= 0) {
    json += ",\"q4t_thinking_budget\":{\"limit\":" +
            std::to_string(reasoning_limit_) +
            ",\"consumed\":" + std::to_string(reasoning_) +
            ",\"forced_closure_tokens\":" + std::to_string(forced_) + '}';
  }
  if (audio_prepared_)
    json += ",\"q4t_audio_preparation\":{\"input_tokens\":" +
            std::to_string(audio_preparation_.input) +
            ",\"output_tokens\":" +
            std::to_string(audio_preparation_.output) +
            ",\"audio_tokens\":" + std::to_string(audio_tokens_) + '}';
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
      "],\"service_tier\":\"default\",\"usage\":" + UsageJson() + '}';
  if (output->size() > kResponseLimit) {
    output->clear();
    return Status::Fail("completion exceeds 32 MiB response storage");
  }
  return Status();
}

}  // namespace q4t::server
