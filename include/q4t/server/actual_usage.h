// Handler-owned observations of committed input and accepted output. These
// counts never come from an admission reservation or a token budget.
#pragma once

#include <algorithm>
#include <cstdint>
#include <optional>

namespace q4t::server {
struct ActualUsage {
  uint64_t input = 0, output = 0, cached = 0, reasoning = 0, cache_write = 0;
  bool operator==(const ActualUsage&) const = default;
};
inline constexpr uint64_t kActualUsageLimit = (uint64_t{1} << 53) - 1;
inline bool ValidActualUsage(const ActualUsage& value) noexcept {
  return value.input <= kActualUsageLimit &&
         value.output <= kActualUsageLimit - value.input &&
         value.cached <= value.input &&
         value.cache_write <= value.input - value.cached &&
         value.reasoning <= value.output;
}

// Constant space and allocation-free, including exception unwinding. Unknown
// is sticky: a missing producer or an overflow must not become a false zero.
class ActualUsageCounter {
 public:
  void Enable() noexcept { enabled_ = true; }
  void BeginStep(bool input = true, bool include = true) noexcept {
    if (active_) valid_ = false;
    active_ = true;
    include_input_ = input;
    include_ = include;
    step_ = {};
  }
  void Observe(const ActualUsage& value) noexcept {
    if (!active_) {
      valid_ = false;
      return;
    }
    if (!include_) return;
    if (!ValidActualUsage(value)) {
      valid_ = false;
      return;
    }
    if (include_input_) {
      step_.input = std::max(step_.input, value.input);
      step_.cached = std::max(step_.cached, value.cached);
      step_.cache_write = std::max(step_.cache_write, value.cache_write);
    }
    step_.output = std::max(step_.output, value.output);
    step_.reasoning = std::max(step_.reasoning, value.reasoning);
  }
  void EndStep() noexcept {
    if (!active_) return;
    valid_ = Add(&total_, step_) && valid_;
    step_ = {};
    active_ = false;
  }
  void Merge(const std::optional<ActualUsage>& value) noexcept {
    if (!value || !Add(&total_, *value)) valid_ = false;
  }
  std::optional<ActualUsage> Total() const noexcept {
    auto value = total_;
    if (!enabled_ || !valid_ || !Add(&value, step_)) return {};
    return value;
  }

 private:
  static bool Add(ActualUsage* target, const ActualUsage& value) noexcept {
    if (!ValidActualUsage(*target) || !ValidActualUsage(value) ||
        value.input + value.output >
            kActualUsageLimit - target->input - target->output)
      return false;
    target->input += value.input;
    target->output += value.output;
    target->cached += value.cached;
    target->reasoning += value.reasoning;
    target->cache_write += value.cache_write;
    return true;
  }
  ActualUsage total_, step_;
  bool enabled_ = false, valid_ = true, active_ = false;
  bool include_input_ = true, include_ = true;
};
}  // namespace q4t::server
