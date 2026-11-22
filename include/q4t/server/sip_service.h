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

// Exclusive ownership of an incoming call. Releasing a ringing reservation
// permits retry. Releasing a submitted answer ends the confirmed dialog.
class SipInvitation {
 public:
  virtual ~SipInvitation();
  virtual ObjectReply Answer(
      std::string_view sdp, std::chrono::steady_clock::time_point deadline) = 0;
  virtual bool Ended() const = 0;
  virtual bool Lost() const = 0;
  std::string offer, local_address, peer_address;
};

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
  ObjectReply Reserve(std::string_view scope, std::string_view id,
                      std::unique_ptr<SipInvitation>* invitation);
  ObjectReply Hangup(std::string_view scope, std::string_view id,
                     std::chrono::steady_clock::time_point deadline);
  ObjectReply Refer(std::string_view scope, std::string_view id,
                    std::string_view target,
                    std::chrono::steady_clock::time_point deadline);
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
