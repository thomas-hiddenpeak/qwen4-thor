// Immutable gateway policy and per-client request ownership. No CUDA state.
#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/client_identity.h"
#include "q4t/status.h"

namespace q4t::server {

inline constexpr size_t kClientPolicyBytes = 64 * 1024;
inline constexpr size_t kMaxClients = 64;
inline constexpr auto kClientHandoffWait = std::chrono::milliseconds(100);

struct ClientLimits {
  std::string id;
  uint32_t max_inflight = 0;
  uint32_t max_control_inflight = 0;
  uint32_t max_prompt_tokens = 0;
  uint32_t max_output_tokens = 0;
  uint32_t max_body_bytes = 0;
};

struct GatewayClientPolicy {
  std::string gateway_token;
  std::string sha256;
  std::vector<ClientLimits> clients;
};

// Transactional. Generic errors never echo keys, credentials or file content.
Status ParseGatewayClientPolicy(std::string_view raw,
                                GatewayClientPolicy* output);
// Regular, single-link, owner-only file. No symlink following at the leaf;
// parent directories are part of the trusted deployment configuration.
Status LoadGatewayClientPolicy(const std::string& path,
                               GatewayClientPolicy* output);

class ClientAdmission;
class ClientLease;

// Valid only while its ClientAdmission lives. Default identities deny access;
// only Authenticate can create a legacy or authenticated identity.
class ClientIdentity {
 public:
  bool Allowed() const { return owner_ != nullptr; }
  bool Scoped() const { return limits_ != nullptr; }
  const ClientLimits* Limits() const { return limits_; }
  std::string_view Id() const {
    return limits_ ? std::string_view(limits_->id) : std::string_view();
  }

 private:
  friend class ClientAdmission;
  friend class ClientLease;
  const ClientAdmission* owner_ = nullptr;
  const ClientLimits* limits_ = nullptr;
  size_t index_ = kMaxClients;
};

enum class ClientLane { kData, kControl };

struct ClientUsage {
  uint32_t data_active = 0;
  uint32_t control_active = 0;
  uint64_t data_rejected = 0;
  uint64_t control_rejected = 0;
};

// Configure at most once, before publishing to handlers. Global server limits
// still apply. Clients cannot allocate new quota keys through request headers.
class ClientAdmission {
 public:
  Status Configure(GatewayClientPolicy policy);
  bool Enabled() const { return !policy_.clients.empty(); }
  size_t Size() const { return policy_.clients.size(); }
  const std::string& PolicySha256() const { return policy_.sha256; }
  ClientIdentity Authenticate(std::string_view id,
                              std::string_view gateway_token) const;
  const ClientLimits& Limits(size_t index) const;
  ClientUsage Usage(size_t index) const;

 private:
  friend class ClientLease;
  struct Counters {
    std::mutex mutex;
    std::condition_variable changed;
    uint32_t data_finishing = 0;  // Protected by mutex; still charged.
    uint32_t control_finishing = 0;
    std::atomic<uint32_t> data{0};
    std::atomic<uint32_t> control{0};
    std::atomic<uint64_t> data_rejected{0};
    std::atomic<uint64_t> control_rejected{0};
  };
  GatewayClientPolicy policy_;
  std::array<Counters, kMaxClients> counters_;
};

// Holds a client's quota through headers/body/preparation/queue/response.
// Noncopyable/nonmovable; any failure or exception leaves cleanup to the scope.
class ClientLease {
 public:
  ClientLease() = default;
  ~ClientLease();
  ClientLease(const ClientLease&) = delete;
  ClientLease& operator=(const ClientLease&) = delete;
  ClientLease(ClientLease&&) = delete;
  ClientLease& operator=(ClientLease&&) = delete;
  bool Acquire(ClientAdmission& admission, const ClientIdentity& identity,
               ClientLane lane);
  // Publish before terminal bytes; retain the permit until destruction. A
  // successor may wait at most 100 ms for finishing work, never active work.
  void BeginHandoff() noexcept;

 private:
  std::atomic<uint32_t>* active_ = nullptr;
  ClientAdmission::Counters* counters_ = nullptr;
  uint32_t* finishing_ = nullptr;
  bool handing_off_ = false;
  bool attempted_ = false;
};

// Bounded labels come exclusively from the private, validated policy.
std::string ClientPolicyMetrics(const ClientAdmission& admission);

}  // namespace q4t::server
