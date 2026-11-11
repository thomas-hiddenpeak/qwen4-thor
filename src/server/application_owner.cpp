#include "q4t/server/application_owner.h"

#include <algorithm>

namespace q4t::server {
int64_t ApplicationStartup::Now() noexcept {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}
void ApplicationStartup::Failure() noexcept {
  const auto now = Now();
  const auto window =
      std::chrono::duration_cast<std::chrono::nanoseconds>(options_.window)
          .count();
  const auto delay =
      std::chrono::duration_cast<std::chrono::nanoseconds>(options_.delay)
          .count();
  size_t kept = 0;
  for (size_t i = 0; i < count_; ++i)
    if (now - recent_[i] < window) recent_[kept++] = recent_[i];
  count_ = kept;
  recent_[count_++] = now;
  auto retry = now + delay;
  if (count_ == recent_.size()) retry = std::max(retry, recent_[0] + window);
  retry_at_.store(retry);
  failures_.fetch_add(1);
}
ApplicationStartupHealth ApplicationStartup::Health() const noexcept {
  const auto remaining = std::max<int64_t>(0, retry_at_.load() - Now());
  return {ready_.load(),
          starting_.test(),
          stopping_.load(),
          attempts_.load(),
          failures_.load(),
          recoveries_.load(),
          (remaining + 999999) / 1000000,
          last_failure_.load()};
}
std::string ApplicationStartupJson(const ApplicationStartupHealth& h) {
  const char* state = h.stopping   ? "stopping"
                      : h.ready    ? "ready"
                      : h.starting ? "starting"
                      : h.failures ? "backoff"
                                   : "uninitialized";
  return std::string("{\"state\":\"") + state +
         "\",\"readiness_dependency\":false,\"attempts\":" +
         std::to_string(h.attempts) +
         ",\"failures\":" + std::to_string(h.failures) +
         ",\"recoveries\":" + std::to_string(h.recoveries) +
         ",\"retry_after_ms\":" + std::to_string(h.retry_after_ms) +
         ",\"last_failure\":\"" + h.last_failure + "\"}";
}
}  // namespace q4t::server
