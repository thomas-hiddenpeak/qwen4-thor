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
    generation_ = generation;
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
  // The current controller has durably consumed a sealed same-boot immediate
  // predecessor and verified the native restore receipt before readiness.
  bool Restored(uint64_t generation, uint64_t predecessor) {
    if (!enabled_ || generation <= 1 || generation != generation_ ||
        predecessor != generation - 1)
      return false;
    not_before_ = 0;
    return true;
  }

 private:
  bool enabled_ = false, have_retirement_ = false;
  int64_t retired_at_ = 0, not_before_ = 0;
  uint64_t generation_ = 0;
};

// Planned shutdown only. A small cooperative window stays within the already
// configured TERM deadline; recovery/device-fault cleanup never uses it.
class QuotaHandoffGrace {
 public:
  void Start(int64_t now, int64_t cleanup_started, int64_t term_ns) {
    if (started_) return;
    started_ = true;
    const auto remaining =
        std::max<int64_t>(0, term_ns - (now - cleanup_started));
    const auto duration = std::min<int64_t>(remaining, 6'000'000'000);
    until_ = now > std::numeric_limits<int64_t>::max() - duration
                 ? std::numeric_limits<int64_t>::max()
                 : now + duration;
  }
  bool Waiting(int64_t now) const { return started_ && now < until_; }

 private:
  bool started_ = false;
  int64_t until_ = 0;
};

}  // namespace q4t::service
