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
  uint32_t requests_per_minute = 0;
  uint32_t tokens_per_minute = 0;
};

struct GatewayClientPolicy {
  uint32_t version = 1;
  bool fair_queue = false;
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
class ClientRateLease;

// Valid only while its ClientAdmission lives. Default identities deny access;
// only Authenticate can create a legacy or authenticated identity.
class ClientIdentity {
 public:
  bool Allowed() const { return owner_ != nullptr; }
  bool Scoped() const { return limits_ != nullptr; }
  const ClientLimits* Limits() const { return limits_; }
  size_t Index() const { return index_; }
  std::string_view Id() const {
    return limits_ ? std::string_view(limits_->id) : std::string_view();
  }

 private:
  friend class ClientAdmission;
  friend class ClientLease;
  friend class ClientRateLease;
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

using ClientClock = std::chrono::steady_clock;
struct ClientRateUsage {
  uint64_t requests = 0;
  uint64_t tokens = 0;
  uint64_t reserved = 0;
  uint64_t rpm_rejected = 0;
  uint64_t tpm_rejected = 0;
  uint64_t admitted = 0;
  uint64_t settled_tokens = 0;
  uint64_t window_remaining_ms = 0;
};

// Configure at most once, before publishing to handlers. Global server limits
// still apply. Clients cannot allocate new quota keys through request headers.
class ClientAdmission {
 public:
  Status Configure(GatewayClientPolicy policy);
  bool Enabled() const { return !policy_.clients.empty(); }
  size_t Size() const { return policy_.clients.size(); }
  uint32_t Version() const { return policy_.version; }
  bool FairQueue() const { return policy_.fair_queue; }
  bool FairCapacity(int inflight, int queued) const;
  const std::string& PolicySha256() const { return policy_.sha256; }
  ClientIdentity Authenticate(std::string_view id,
                              std::string_view gateway_token) const;
  const ClientLimits& Limits(size_t index) const;
  ClientUsage Usage(size_t index) const;
  ClientRateUsage RateUsage(
      size_t index, ClientClock::time_point now = ClientClock::now()) const;

 private:
  friend class ClientLease;
  friend class ClientRateLease;
  struct Counters {
    mutable std::mutex mutex;
    std::condition_variable changed;
    uint32_t data_finishing = 0;  // Protected by mutex; still charged.
    uint32_t control_finishing = 0;
    std::atomic<uint32_t> data{0};
    std::atomic<uint32_t> control{0};
    std::atomic<uint64_t> data_rejected{0};
    std::atomic<uint64_t> control_rejected{0};
    bool window_started = false;
    ClientClock::time_point window;
    ClientRateUsage rate;
    void Advance(ClientClock::time_point now);
  };
  GatewayClientPolicy policy_;
  std::array<Counters, kMaxClients> counters_;
};

enum class ClientRateResult { kAllowed, kRpm, kTpm, kInvalid };

// Fixed-window request/token accounting. Outstanding reservations survive
// window rollover; actual logical usage is settled in the completion window.
// Handler-thread owned; settlement is idempotent and runs on scope unwind.
class ClientRateLease {
 public:
  ~ClientRateLease() { Settle(); }
  ClientRateLease() = default;
  ClientRateLease(const ClientRateLease&) = delete;
  ClientRateLease& operator=(const ClientRateLease&) = delete;
  ClientRateResult Reserve(ClientAdmission& admission,
                           const ClientIdentity& identity, uint64_t prompt,
                           uint64_t output,
                           ClientClock::time_point now = ClientClock::now());
  void Start() noexcept { StartPrompt(prompt_); }
  // Cumulative distinct prompt tokens admitted from a pre-reserved batch.
  // Repeated candidates do not charge the same input again.
  void StartPrompt(uint64_t count) noexcept {
    started_ = true;
    if (count >= admitted_prompt_ && count <= prompt_) admitted_prompt_ = count;
  }
  void Completion(uint64_t count) noexcept { completion_ = count; }
  void Settle(ClientClock::time_point now = ClientClock::now()) noexcept;

 private:
  ClientAdmission::Counters* counters_ = nullptr;
  uint64_t prompt_ = 0;
  uint64_t admitted_prompt_ = 0;
  uint64_t reserved_ = 0;
  uint64_t completion_ = 0;
  bool started_ = false;
  bool attempted_ = false;
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
