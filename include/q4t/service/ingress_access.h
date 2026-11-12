// Immutable native credential snapshots; stable principals across rotations.
#pragma once

#include <atomic>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::service::ingress {

inline constexpr size_t kAccessClients = 64;
inline constexpr size_t kAccessKeysPerClient = 4;
inline constexpr size_t kAccessHistory = 4096;
inline constexpr size_t kAccessPolicyBytes = 1024 * 1024;
inline constexpr uint64_t kAccessRevisionLimit = (uint64_t{1} << 53) - 1;

struct AccessClient {
  std::string id;
  std::vector<std::string> key_sha256;
};

struct AccessPolicy {
  uint64_t revision = 0;
  // Slots are permanent. Revocation empties keys; it never reuses a slot.
  std::vector<AccessClient> clients;
  std::map<std::string, std::string> key_owners;
};

// Reuses the project's SHA256 implementation. Empty means an invalid key.
std::string AccessKeyDigest(std::string_view key);
Status ValidateAccessPolicy(const AccessPolicy& policy);
Status ParseAccessPolicy(const std::string& raw, AccessPolicy* output);
std::string AccessPolicyDigest(const AccessPolicy& policy);

class AccessIdentity {
 public:
  bool Allowed() const { return policy_ != nullptr; }
  size_t Index() const { return index_; }
  std::string_view Id() const {
    return policy_ ? policy_->clients[index_].id : std::string_view{};
  }
  uint64_t Revision() const { return policy_ ? policy_->revision : 0; }

 private:
  friend class ClientAccess;
  std::shared_ptr<const AccessPolicy> policy_;
  size_t index_ = kAccessClients;
};

// No filesystem, model or quota authority. The durable control plane must
// supply a validated committed policy, including historical key ownership.
class ClientAccess {
 public:
  Status Configure(AccessPolicy policy);
  Status Replace(AccessPolicy policy, uint64_t expected_revision);
  // A durable journal reader may catch up several committed revisions. Stable
  // slots and the complete historical ownership map must still extend ours.
  Status Synchronize(AccessPolicy policy, uint64_t expected_revision);
  bool Configured() const { return current_.load() != nullptr; }
  AccessIdentity Authenticate(std::string_view authorization) const;
  io::Json Describe() const;

 private:
  Status Publish(AccessPolicy policy, uint64_t expected_revision,
                 bool contiguous);
  std::atomic<std::shared_ptr<const AccessPolicy>> current_;
  std::mutex writer_;
};

}  // namespace q4t::service::ingress
