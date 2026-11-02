#include "q4t/server/thinking_budget.h"

#include <algorithm>
#include <limits>

namespace q4t::server {
namespace {
constexpr std::string_view kOpen = "<think>";
constexpr std::string_view kClose = "</think>";
constexpr std::string_view kToolOpen = "<tool_call>";
constexpr std::string_view kToolClose = "</tool_call>";
constexpr std::array<unsigned char, 3> kContinuation = {0x80, 0x90, 0xa0};
}  // namespace

void ThinkingBudget::State::Utf8(unsigned char byte) {
  if (utf8_left && byte >= utf8_min && byte <= utf8_max) {
    --utf8_left;
    utf8_min = 0x80;
    utf8_max = 0xbf;
    return;
  }
  // Malformed maximal subparts are normalized by the response decoder. Track
  // only the current incomplete, potentially valid scalar for forced closure.
  utf8_left = 0;
  utf8_min = 0x80;
  utf8_max = 0xbf;
  if (byte >= 0xc2 && byte <= 0xdf) utf8_left = 1;
  if (byte >= 0xe0 && byte <= 0xef) {
    utf8_left = 2;
    if (byte == 0xe0) utf8_min = 0xa0;
    if (byte == 0xed) utf8_max = 0x9f;
  }
  if (byte >= 0xf0 && byte <= 0xf4) {
    utf8_left = 3;
    if (byte == 0xf0) utf8_min = 0x90;
    if (byte == 0xf4) utf8_max = 0x8f;
  }
}

bool ThinkingBudget::State::Feed(std::string_view bytes) {
  bool entered = false;
  for (const unsigned char byte : bytes) {
    Utf8(byte);
    if (region == Region::kContent && json_output) continue;
    pending += static_cast<char>(byte);
    const auto marker = region == Region::kReasoning ? kClose
                        : region == Region::kTool    ? kToolClose
                                                     : kOpen;
    const bool tools = region == Region::kContent && tool_regions;
    if (pending == marker) {
      if (region == Region::kContent) {
        region = Region::kReasoning;
        entered = true;
      } else {
        region = Region::kContent;
      }
      pending.clear();
    } else if (tools && pending == kToolOpen) {
      region = Region::kTool;
      pending.clear();
    } else {
      while (!pending.empty() && !marker.starts_with(pending) &&
             !(tools && kToolOpen.starts_with(pending)))
        pending.erase(0, 1);
    }
  }
  return entered;
}

Status ThinkingBudget::Init(int64_t limit, std::string_view prompt,
                            bool json_output, bool tool_regions) {
  if (initialized_) return Status::Fail("thinking budget already initialized");
  initialized_ = true;
  if (limit < -1 || limit > std::numeric_limits<int32_t>::max())
    return Status::Fail("invalid thinking budget");
  limit_ = limit;
  if (!Enabled()) return Status();
  state_.json_output = json_output;
  state_.tool_regions = tool_regions;
  if (prompt.ends_with("<think>\n"))
    state_.region = Region::kReasoning;
  else if (!prompt.ends_with("<think>\n\n</think>\n\n"))
    return Status::Fail("unsupported thinking budget generation prefix");
  return Status();
}

Status ThinkingBudget::PrepareTokens(const Encode& encode, const Decode& decode,
                                     int vocab,
                                     std::span<const int32_t> stop_ids) {
  if (!initialized_ || !Enabled() || prepared_ || vocab <= 0 || vocab > 300000)
    return Status::Fail("invalid thinking budget token preparation");
  for (size_t offset = 0; offset < kClose.size(); ++offset) {
    auto& ids = close_suffixes_[offset];
    auto status = encode(kClose.substr(offset), &ids);
    if (!status) return status;
    if (ids.empty() || ids.size() > kClose.size() - offset)
      return Status::Fail("unsupported thinking closure encoding");
    std::string text;
    for (const auto id : ids) {
      if (id < 0 || id >= vocab ||
          std::find(stop_ids.begin(), stop_ids.end(), id) != stop_ids.end())
        return Status::Fail("invalid thinking closure token");
      std::string piece;
      status = decode(id, &piece);
      if (!status) return status;
      if (piece.empty()) return Status::Fail("empty thinking closure token");
      text += piece;
    }
    if (text != kClose.substr(offset))
      return Status::Fail("thinking closure bytes differ from tokenization");
  }
  for (int32_t id = 0; id < vocab; ++id) {
    if (std::find(stop_ids.begin(), stop_ids.end(), id) != stop_ids.end())
      continue;
    std::string piece;
    auto status = decode(id, &piece);
    if (!status) return status;
    if (piece.size() == 1) {
      for (size_t i = 0; i < kContinuation.size(); ++i)
        if (continuation_ids_[i] < 0 &&
            static_cast<unsigned char>(piece[0]) == kContinuation[i])
          continuation_ids_[i] = id;
    }
    if (std::all_of(continuation_ids_.begin(), continuation_ids_.end(),
                    [](int32_t value) { return value >= 0; })) {
      prepared_ = true;
      return Status();
    }
  }
  return Status::Fail("thinking closure requires byte continuation tokens");
}

bool ThinkingBudget::CanSpeculate(int max_accepted) const {
  return !Enabled() || (max_accepted > 0 && limit_ - consumed_ > max_accepted);
}

bool ThinkingBudget::Allows(std::string_view bytes) const {
  if (!Enabled() || consumed_ < limit_) return true;
  if (Thinking()) return false;  // Only ForcedToken is admissible here.
  auto next = state_;
  return !next.Feed(bytes);
}

int32_t ThinkingBudget::ForcedToken() {
  if (!Enabled() || !prepared_) return -1;
  if (forced_index_ < forced_.size()) return forced_[forced_index_];
  if (consumed_ < limit_ || !Thinking()) return -1;
  forced_.clear();
  forced_index_ = 0;
  auto utf8 = state_;
  while (utf8.utf8_left) {
    const auto byte = utf8.utf8_min;
    const auto found =
        std::find(kContinuation.begin(), kContinuation.end(), byte);
    forced_.push_back(continuation_ids_[found - kContinuation.begin()]);
    utf8.Utf8(byte);
  }
  const auto& suffix = close_suffixes_[state_.pending.size()];
  forced_.insert(forced_.end(), suffix.begin(), suffix.end());
  return forced_[0];
}

Status ThinkingBudget::Observe(int32_t token, std::string_view bytes,
                               bool stop) {
  if (!Enabled()) return Status();
  if (!prepared_) return Status::Fail("thinking budget tokens not prepared");
  const int32_t forced = ForcedToken();
  if (forced >= 0 && token != forced)
    return Status::Fail("thinking closure token was not selected");
  if (stop) return Status();
  const bool before = Thinking();
  auto next = state_;
  const bool entered = next.Feed(bytes);
  if (forced < 0 && (before || entered)) {
    if (consumed_ >= limit_)
      return Status::Fail("thinking budget exceeded before token admission");
    ++consumed_;
  }
  state_ = std::move(next);
  if (forced >= 0) {
    ++forced_tokens_;
    ++forced_index_;
    if (forced_index_ == forced_.size() && Thinking())
      return Status::Fail("thinking closure did not leave reasoning");
  }
  return Status();
}

std::string ThinkingBudget::UsageFields() const {
  if (!Enabled()) return {};
  return ",\"completion_tokens_details\":{\"reasoning_tokens\":" +
         std::to_string(ReasoningTokens()) +
         "},\"q4t_thinking_budget\":{\"limit\":" + std::to_string(limit_) +
         ",\"consumed\":" + std::to_string(consumed_) +
         ",\"forced_closure_tokens\":" + std::to_string(forced_tokens_) + '}';
}

}  // namespace q4t::server
