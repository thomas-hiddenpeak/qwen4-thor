// Conservative protection for the engine's process-local minute quotas.
#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>

namespace q4t::service {

class QuotaGate {
 public:
  explicit QuotaGate(bool enabled = false) : enabled_(enabled) {}
  void Retired(int64_t now_ns) {
    retired_at_ = std::max(retired_at_, now_ns);
    have_retirement_ = true;
  }
  void Granted(uint64_t generation, int64_t now_ns) {
    if (!enabled_ || generation == 1) {
      not_before_ = 0;
      return;
    }
    // A new Guardian cannot recover an unrecorded old exit timestamp. Waiting
    // a full window from this grant cannot create a free quota reset.
    const int64_t base = have_retirement_ ? retired_at_ : now_ns;
    constexpr int64_t window = 60'000'000'000;
    not_before_ = base > std::numeric_limits<int64_t>::max() - window
                      ? std::numeric_limits<int64_t>::max()
                      : base + window;
  }
  bool Open(int64_t now_ns) const { return now_ns >= not_before_; }
  int64_t NotBefore() const { return not_before_; }

 private:
  bool enabled_ = false, have_retirement_ = false;
  int64_t retired_at_ = 0, not_before_ = 0;
};

}  // namespace q4t::service
