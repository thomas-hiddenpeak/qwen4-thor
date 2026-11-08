#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/status.h"

namespace q4t::server {

// One external completion, with serial independently generated choices.
// It neither registers requests nor owns quota/model resources.
class ChatChoices {
 public:
  static constexpr size_t kResponseLimit = 32 * 1024 * 1024;
  ChatChoices(int count, bool stream) : count_(count), stream_(stream) {}
  Status Add(int index, std::string_view message, std::string_view finish,
             std::string_view logprobs, uint64_t prompt, uint64_t completion,
             uint64_t cached, bool report_cached, int64_t reasoning_limit,
             uint64_t reasoning, uint64_t forced, bool prediction = false,
             uint64_t prediction_accepted = 0,
             uint64_t prediction_rejected = 0);
  uint64_t CompletionTokens() const { return completion_; }
  std::string UsageJson() const;
  Status Json(std::string_view id, std::string_view model, int64_t created,
              std::string* output) const;
  bool Complete() const { return accepted_ == count_; }

 private:
  int count_;
  bool stream_;
  int accepted_ = 0;
  uint64_t prompt_ = 0;
  uint64_t completion_ = 0;
  uint64_t cached_ = 0;
  bool report_cached_ = false;
  int64_t reasoning_limit_ = -1;
  uint64_t reasoning_ = 0;
  uint64_t forced_ = 0;
  bool prediction_ = false;
  uint64_t prediction_accepted_ = 0;
  uint64_t prediction_rejected_ = 0;
  std::string choices_;
};

}  // namespace q4t::server
