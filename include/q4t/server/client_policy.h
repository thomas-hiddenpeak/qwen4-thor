// Atomic gateway policy snapshots and stable request ownership. No CUDA state.
#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/client_identity.h"
#include "q4t/status.h"

namespace q4t::server {

inline constexpr size_t kClientPolicyBytes = 64 * 1024;
inline constexpr size_t kMaxClients = 64;
// Optional request-owned classifiers have a separate, fixed internal ceiling;
// they never increase the public generation budget or escape TPM accounting.
inline constexpr uint64_t kMaxAuxiliaryOutputTokens = 1024;
// Server-owned document preparation has its own finite reservation. It cannot
// enlarge the caller's answer budget or bypass the client's TPM limit.
inline constexpr uint64_t kMaxPreparationOutputTokens = 65536;
inline constexpr uint64_t kMaxClientRevision = (uint64_t{1} << 53) - 1;
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
  // Zero only for v1-v3: retain their max_body_bytes limit on file uploads.
  uint32_t max_file_body_bytes = 0;
};

struct GatewayClientPolicy {
  uint32_t version = 1;
  uint64_t revision = 0;
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
// Stable policy identity across model generations; excludes the ephemeral
// gateway token and raw JSON formatting. Empty for an invalid policy.
std::string ClientPolicyDigest(const GatewayClientPolicy& policy);

class ClientAdmission;
class ClientLease;
class ClientRateLease;

// Valid only while its ClientAdmission lives. Default identities deny access;
// only Authenticate can create a legacy or authenticated identity.
class ClientIdentity {
 public:
  bool Allowed() const { return owner_ && (index_ == kMaxClients || Limits()); }
  bool Scoped() const { return Limits() != nullptr; }
  const ClientLimits* Limits() const {
    return policy_ && index_ < policy_->clients.size()
               ? &policy_->clients[index_]
               : nullptr;
  }
  size_t Index() const { return index_; }
  std::string_view Id() const {
    const auto* limits = Limits();
    return limits ? std::string_view(limits->id) : std::string_view();
  }

 private:
  friend class ClientAdmission;
  friend class ClientLease;
  friend class ClientRateLease;
  const ClientAdmission* owner_ = nullptr;
  std::shared_ptr<const GatewayClientPolicy> policy_;
  size_t index_ = kMaxClients;
};

enum class ClientLane { kData, kControl, kCancel };

struct ClientUsage {
  uint32_t data_active = 0;
  uint32_t control_active = 0;
  uint64_t data_rejected = 0;
  uint64_t control_rejected = 0;
  uint32_t cancel_active = 0;
  uint64_t cancel_rejected = 0;
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

// A sealed snapshot has no outstanding reservations. Monotonic times are
// meaningful only in the same boot, verified by the recovery coordinator.
struct ClientRateRecord {
  std::string id;
  bool window_started = false;
  int64_t window_ns = 0;
  ClientRateUsage usage;
};
struct ClientRateState {
  int64_t captured_ns = 0;
  uint64_t policy_revision = 0;
  std::string policy_sha256;
  std::vector<ClientRateRecord> clients;
};

// Configure once. v3+ permits explicit revisioned updates; client slots and
// counter storage never move. Global limits still apply. HTTP headers cannot
// allocate clients or change policy.
class ClientAdmission {
 public:
  Status Configure(GatewayClientPolicy policy);
  Status Publish(GatewayClientPolicy policy, uint64_t expected_revision,
                 uint32_t global_inflight, uint32_t global_queued);
  std::shared_ptr<const GatewayClientPolicy> Snapshot() const {
    return policy_.load(std::memory_order_acquire);
  }
  bool Enabled() const { return bool(Snapshot()); }
  size_t Size() const {
    const auto policy = Snapshot();
    return policy ? policy->clients.size() : 0;
  }
  uint32_t Version() const {
    const auto policy = Snapshot();
    return policy ? policy->version : 1;
  }
  bool FairQueue() const {
    const auto policy = Snapshot();
    return policy && policy->fair_queue;
  }
  bool FairCapacity(int inflight, int queued) const;
  std::string PolicySha256() const {
    const auto policy = Snapshot();
    return policy ? policy->sha256 : std::string();
  }
  ClientIdentity Authenticate(std::string_view id,
                              std::string_view gateway_token) const;
  // Trusted recovery of a current server snapshot, never a request-supplied
  // identity. It still must acquire the normal client data/rate permits.
  ClientIdentity RecoveryIdentity(size_t index) const;
  // Verify an existing authenticated identity against this admission's current
  // policy without taking another concurrency permit.
  bool OwnsIdentity(const ClientIdentity& identity) const {
    if (identity.owner_ != this || !identity.Allowed()) return false;
    const auto current = RecoveryIdentity(identity.Index());
    return current.Allowed() && current.Id() == identity.Id();
  }
  ClientLimits Limits(size_t index) const;
  ClientUsage Usage(size_t index) const;
  ClientRateUsage RateUsage(
      size_t index, ClientClock::time_point now = ClientClock::now()) const;
  // Only a private controller-owned admin enables the single startup restore.
  // Self-check accounting may be replaced before public admission opens.
  Status EnableRateRecovery();
  Status RestoreRates(const ClientRateState* state, std::string_view receipt,
                      ClientClock::time_point now = ClientClock::now());
  // Permanently closes new rate reservations for this process. Busy means
  // existing leases must settle; retrying never reopens admission.
  Status FreezeRates(ClientRateState* state,
                     ClientClock::time_point now = ClientClock::now());
  bool RatesFrozen() const { return rates_frozen_.load(); }
  bool RatesRecovered() const { return rates_recovered_.load(); }
  std::string RateRestoreReceipt() const;

 private:
  friend class ClientLease;
  friend class ClientRateLease;
  struct Counters {
    mutable std::mutex mutex;
    std::condition_variable changed;
    uint32_t data_finishing = 0;  // Protected by mutex; still charged.
    uint32_t control_finishing = 0;
    uint32_t cancel_finishing = 0;
    std::atomic<uint32_t> data{0};
    std::atomic<uint32_t> control{0};
    std::atomic<uint32_t> cancel{0};
    std::atomic<uint64_t> data_rejected{0};
    std::atomic<uint64_t> control_rejected{0};
    std::atomic<uint64_t> cancel_rejected{0};
    bool window_started = false;
    ClientClock::time_point window;
    ClientRateUsage rate;
    uint64_t rate_active = 0;
    void Advance(ClientClock::time_point now);
  };
  mutable std::mutex policy_mutex_;
  std::atomic<std::shared_ptr<const GatewayClientPolicy>> policy_;
  std::array<Counters, kMaxClients> counters_;
  std::atomic<bool> rates_frozen_{false}, rates_recovered_{false};
  bool rate_recovery_enabled_ = false;
  std::string rate_restore_receipt_;
};

enum class ClientRateResult { kAllowed, kRpm, kTpm, kInvalid, kFrozen };

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
                           ClientClock::time_point now = ClientClock::now(),
                           uint64_t auxiliary_output = 0,
                           uint64_t preparation_output = 0);
  // Reserve actual additional input for a subsequent internal model step.
  // Keeps the original RPM admission and shared output reservation; cumulative
  // input must still fit the client's prompt limit. StartPrompt charges only
  // after the new step is admitted, including cancellation before admission.
  ClientRateResult ExtendPrompt(
      ClientAdmission& admission, const ClientIdentity& identity,
      uint64_t additional, ClientClock::time_point now = ClientClock::now());
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
  const ClientAdmission* admission_ = nullptr;
  size_t client_ = kMaxClients;
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
  bool Holds(const ClientIdentity& identity, ClientLane lane) const {
    return admitted_ && identity_.owner_ == identity.owner_ &&
           identity_.index_ == identity.index_ && identity_.Id() == identity.Id() &&
           lane_ == lane;
  }
  // Transfer an admitted request into owned background work. The permit stays
  // charged continuously; the source cannot acquire another permit.
  bool TransferTo(ClientLease& destination) noexcept;
  // Publish before terminal bytes; retain the permit until destruction. A
  // successor may wait at most 100 ms for finishing work, never active work.
  void BeginHandoff() noexcept;

 private:
  std::atomic<uint32_t>* active_ = nullptr;
  ClientAdmission::Counters* counters_ = nullptr;
  uint32_t* finishing_ = nullptr;
  bool handing_off_ = false;
  bool attempted_ = false;
  bool admitted_ = false;
  ClientIdentity identity_;
  ClientLane lane_ = ClientLane::kData;
};

// Bounded labels come exclusively from the private, validated policy.
std::string ClientPolicyMetrics(const ClientAdmission& admission);

}  // namespace q4t::server
