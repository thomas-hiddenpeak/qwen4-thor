// Shared chat/preflight ownership, including preparation and response cleanup.
#pragma once

#include <atomic>

namespace q4t::server {

class RequestAdmissionLease {
 public:
  RequestAdmissionLease(std::atomic<int>& active, int limit)
      : active_(active), limit_(limit) {}
  ~RequestAdmissionLease() {
    if (owned_) active_.fetch_sub(1, std::memory_order_release);
  }
  RequestAdmissionLease(const RequestAdmissionLease&) = delete;
  RequestAdmissionLease& operator=(const RequestAdmissionLease&) = delete;
  bool TryAcquire() {
    if (owned_) return true;
    int current = active_.load(std::memory_order_relaxed);
    while (current < limit_) {
      if (active_.compare_exchange_weak(current, current + 1,
                                        std::memory_order_acquire,
                                        std::memory_order_relaxed)) {
        owned_ = true;
        return true;
      }
    }
    return false;
  }

 private:
  std::atomic<int>& active_;
  const int limit_;
  bool owned_ = false;
};

}  // namespace q4t::server
