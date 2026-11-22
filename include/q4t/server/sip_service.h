#pragma once

#include <atomic>
#include <chrono>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/object_store.h"

namespace q4t::server {

// One private, configured carrier listener per trunk; all application lookups
// use the HTTP-authenticated client scope, never a SIP header or source IP.
class SipService {
 public:
  SipService();
  ~SipService();
  SipService(const SipService&) = delete;
  SipService& operator=(const SipService&) = delete;
  void Start(const std::string& config,
             std::function<bool(std::string_view)> current_scope);
  void Stop();
  std::string HealthJson() const;
  ObjectReply Reject(std::string_view scope, std::string_view id, int status,
                     std::chrono::steady_clock::time_point deadline);

 private:
  class Trunk;
  mutable std::mutex mutex_;
  std::vector<std::shared_ptr<Trunk>> trunks_;
  std::atomic<int> configured_{0}, activation_failures_{0};
  std::jthread owner_;
};
}  // namespace q4t::server
