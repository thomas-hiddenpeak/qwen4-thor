// Request-local generation admission. No model, CUDA, or tokenizer dependency.
#pragma once

#include <array>
#include <cstdint>
#include <functional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/status.h"

namespace q4t::server {

inline constexpr std::string_view kThinkingBudgetProfile = "q4t-thinking-v1";

class ThinkingBudget {
 public:
  using Encode = std::function<Status(std::string_view, std::vector<int32_t>*)>;
  using Decode = std::function<Status(int32_t, std::string*)>;

  // -1 means absent/null. Prefix and region flags follow ChatResponseParser.
  Status Init(int64_t limit, std::string_view prompt, bool json_output,
              bool tool_regions, bool observe_unbounded = false);
  Status PrepareTokens(const Encode& encode, const Decode& decode, int vocab,
                       std::span<const int32_t> stop_ids);
  bool Enabled() const { return limit_ >= 0; }
  bool Tracking() const { return Enabled() || observe_unbounded_; }
  bool Thinking() const { return state_.region == Region::kReasoning; }
  int64_t Consumed() const { return consumed_; }
  int64_t ForcedTokens() const { return forced_tokens_; }
  int64_t ReasoningTokens() const { return consumed_ + forced_tokens_; }

  // Keep one natural token for the MTP pending correction on plain fallback.
  bool CanSpeculate(int max_accepted) const;
  // Outside exhausted thinking, mask tokens that would enter a new block.
  bool Allows(std::string_view bytes) const;
  // The next actual closure/boundary token, or -1 for normal selection.
  // At most 3 UTF-8 continuation + 1 LF + 8 close + 2 LF byte tokens.
  // PrepareTokens must have succeeded. This does not consume any token.
  int32_t ForcedToken();
  Status Observe(int32_t token, std::string_view bytes, bool stop = false);
  std::string UsageFields() const;

 private:
  enum class Region { kContent, kReasoning, kTool };
  struct State {
    Region region = Region::kContent;
    bool json_output = false;
    bool tool_regions = false;
    std::string pending;  // Only an incomplete delimiter, at most 11 bytes.
    int utf8_left = 0;
    unsigned char utf8_min = 0x80;
    unsigned char utf8_max = 0xbf;
    void Utf8(unsigned char byte);
    bool Feed(std::string_view bytes);  // True if any reasoning block opened.
  };

  int64_t limit_ = -1;
  int64_t consumed_ = 0;
  int64_t forced_tokens_ = 0;
  State state_;
  bool initialized_ = false;
  bool observe_unbounded_ = false;
  bool prepared_ = false;
  std::array<std::vector<int32_t>, 8> close_suffixes_;
  // Minimal valid UTF-8 completions only need these three byte values.
  std::array<int32_t, 3> continuation_ids_ = {-1, -1, -1};
  std::vector<int32_t> forced_;
  size_t forced_index_ = 0;
};

}  // namespace q4t::server
