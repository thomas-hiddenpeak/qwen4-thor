// Bounded private model-policy publication. No filesystem I/O in Step.
#pragma once

#include <array>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include "q4t/server/client_policy.h"
#include "q4t/service/guardian_process.h"

namespace q4t::server {

class ClientPolicyAdmin {
 public:
  using Publish = std::function<Status(GatewayClientPolicy, uint64_t)>;
  ClientPolicyAdmin(ClientAdmission& admission, uint32_t inflight,
                    uint32_t queued, Publish publish);
  ~ClientPolicyAdmin();
  bool Open(const std::string& address, service::ProcessIdentity owner);
  // Start owns Step until destruction; callers must not concurrently Step.
  // The callback must be bounded and must never wait for a model/queue lock.
  void Start();
  void Step() noexcept;
  std::string Describe() const;

 private:
  struct Peer {
    service::OwnedFd fd;
    std::chrono::steady_clock::time_point deadline;
    bool writer = false;
    size_t bytes = 0;
    uint64_t expected_revision = 0;
    std::string digest;
    std::string payload;
  };
  bool OwnerAlive() const noexcept;
  void Receive(Peer& peer);
  void Reply(Peer& peer, bool ok, const char* error = "");
  ClientAdmission& admission_;
  const uint32_t inflight_, queued_;
  Publish publish_;
  service::ProcessIdentity owner_;
  service::OwnedFd owner_pidfd_, listener_;
  std::array<Peer, 2> peers_;
  std::mutex wait_mutex_;
  std::condition_variable wake_;
  std::jthread worker_;
};

class SequenceAdmissionQueue;
Status PublishClientPolicy(ClientAdmission& admission,
                           SequenceAdmissionQueue& queue, std::mutex& mutex,
                           GatewayClientPolicy candidate, uint64_t expected,
                           uint32_t inflight, uint32_t queued);

// Requires a controller-pinned exec environment, independently of progress
// observation. Failure leaves existing policy and core serving untouched.
std::unique_ptr<ClientPolicyAdmin> StartClientPolicyAdmin(
    ClientAdmission& admission, uint32_t inflight, uint32_t queued,
    ClientPolicyAdmin::Publish publish) noexcept;

}  // namespace q4t::server
