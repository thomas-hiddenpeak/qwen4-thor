#include "q4t/service/ingress_access.h"

#include <algorithm>
#include <set>
#include <span>
#include <utility>

#include "q4t/server/client_identity.h"
#include "q4t/service/guardian_wire.h"
#include "q4t/trace/sha256.h"

namespace q4t::service::ingress {
namespace {
Status Invalid() { return Status::Fail("invalid client access policy"); }

bool HexDigest(std::string_view text) {
  return text.size() == 64 && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

std::string Digest(std::string_view raw) {
  return trace::HexDigest(trace::Sha256(
      std::span(reinterpret_cast<const uint8_t*>(raw.data()), raw.size())));
}
}  // namespace

std::string AccessKeyDigest(std::string_view key) {
  if (key.size() < 32 || key.size() > 128 ||
      !std::all_of(key.begin(), key.end(), [](unsigned char c) {
        return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
               (c >= '0' && c <= '9') || c == '-' || c == '_';
      }))
    return {};
  return Digest(key);
}

Status ValidateAccessPolicy(const AccessPolicy& policy) {
  if (policy.revision > kAccessRevisionLimit || policy.clients.empty() ||
      policy.clients.size() > kAccessClients ||
      policy.key_owners.size() > kAccessHistory)
    return Invalid();
  std::set<std::string> ids, keys;
  for (const auto& client : policy.clients) {
    if (!server::ValidClientId(client.id) || !ids.insert(client.id).second ||
        client.key_sha256.size() > kAccessKeysPerClient)
      return Invalid();
    for (const auto& key : client.key_sha256) {
      const auto owner = policy.key_owners.find(key);
      if (!HexDigest(key) || !keys.insert(key).second ||
          owner == policy.key_owners.end() || owner->second != client.id)
        return Invalid();
    }
  }
  for (const auto& [key, id] : policy.key_owners)
    if (!HexDigest(key) || !ids.contains(id)) return Invalid();
  return Status();
}

Status ParseAccessPolicy(const std::string& raw, AccessPolicy* output) {
  if (!output || raw.empty() || raw.size() > kAccessPolicyBytes)
    return Invalid();
  io::Json value;
  const io::JsonParseLimits limits{.max_depth = 6,
                                   .max_values = 32768,
                                   .max_string_bytes = kAccessPolicyBytes,
                                   .reject_duplicate_keys = true};
  if (!io::ParseJson(raw, &value, true, limits).ok() ||
      !wire::HasKeys(value, {"version", "revision", "clients", "key_owners"}))
    return Invalid();
  uint64_t version = 0;
  AccessPolicy policy;
  if (!wire::GetUnsigned(value, "version", &version).ok() || version != 1 ||
      !wire::GetUnsigned(value, "revision", &policy.revision).ok())
    return Invalid();
  const auto* clients = value.GetArray("clients");
  const auto* owners = value.Find("key_owners");
  if (!clients || clients->array.size() > kAccessClients || !owners ||
      !owners->IsObject() || owners->object.size() > kAccessHistory)
    return Invalid();
  for (const auto& row : clients->array) {
    if (!wire::HasKeys(row, {"id", "key_sha256"})) return Invalid();
    AccessClient client{row.GetString("id"), {}};
    const auto* keys = row.GetArray("key_sha256");
    if (!keys || keys->array.size() > kAccessKeysPerClient) return Invalid();
    for (const auto& key : keys->array) {
      if (!key.IsString()) return Invalid();
      client.key_sha256.push_back(key.str);
    }
    policy.clients.push_back(std::move(client));
  }
  for (const auto& [key, id] : owners->object) {
    if (!id.IsString()) return Invalid();
    policy.key_owners.emplace(key, id.str);
  }
  if (!ValidateAccessPolicy(policy).ok()) return Invalid();
  *output = std::move(policy);
  return Status();
}

std::string AccessPolicyDigest(const AccessPolicy& policy) {
  if (!ValidateAccessPolicy(policy).ok()) return {};
  // Restricted IDs/hex cannot contain separators. Sort keys because their
  // order is immaterial; retain client order because it fixes stable slots.
  std::string raw =
      "q4t-client-access-v1\n" + std::to_string(policy.revision) + "\n";
  for (const auto& client : policy.clients) {
    raw += client.id + ':';
    auto keys = client.key_sha256;
    std::sort(keys.begin(), keys.end());
    for (const auto& key : keys) raw += key + ',';
    raw += '\n';
  }
  raw += "history\n";
  for (const auto& [key, id] : policy.key_owners) raw += key + ':' + id + '\n';
  return Digest(raw);
}

Status ClientAccess::Configure(AccessPolicy policy) {
  if (!ValidateAccessPolicy(policy).ok()) return Invalid();
  std::lock_guard guard(writer_);
  if (current_.load()) return Status::Fail("client access already configured");
  current_.store(std::make_shared<const AccessPolicy>(std::move(policy)));
  return Status();
}

Status ClientAccess::Replace(AccessPolicy policy, uint64_t expected_revision) {
  if (!ValidateAccessPolicy(policy).ok()) return Invalid();
  std::lock_guard guard(writer_);
  const auto before = current_.load();
  if (!before || before->revision != expected_revision ||
      expected_revision >= kAccessRevisionLimit ||
      policy.revision != expected_revision + 1)
    return Status::Fail("client access revision conflict");
  if (policy.clients.size() < before->clients.size()) return Invalid();
  for (size_t i = 0; i < before->clients.size(); ++i)
    if (policy.clients[i].id != before->clients[i].id) return Invalid();
  for (const auto& [key, id] : before->key_owners) {
    const auto found = policy.key_owners.find(key);
    if (found == policy.key_owners.end() || found->second != id)
      return Invalid();
  }
  current_.store(std::make_shared<const AccessPolicy>(std::move(policy)));
  return Status();
}

AccessIdentity ClientAccess::Authenticate(
    std::string_view authorization) const {
  AccessIdentity result;
  constexpr std::string_view prefix = "bearer ";
  if (authorization.size() < prefix.size()) return result;
  for (size_t i = 0; i < prefix.size(); ++i) {
    char c = authorization[i];
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    if (c != prefix[i]) return result;
  }
  const auto digest = AccessKeyDigest(authorization.substr(prefix.size()));
  if (digest.empty()) return result;
  const auto policy = current_.load();
  if (!policy) return result;
  size_t match = kAccessClients;
  for (size_t i = 0; i < policy->clients.size(); ++i) {
    for (const auto& key : policy->clients[i].key_sha256) {
      unsigned difference = 0;
      for (size_t j = 0; j < 64; ++j)
        difference |= static_cast<unsigned char>(key[j]) ^
                      static_cast<unsigned char>(digest[j]);
      if (difference == 0) match = i;
    }
  }
  if (match != kAccessClients) {
    result.policy_ = policy;
    result.index_ = match;
  }
  return result;
}

io::Json ClientAccess::Describe() const {
  const auto policy = current_.load();
  size_t active = 0, revoked = 0;
  if (policy)
    for (const auto& client : policy->clients) {
      active += client.key_sha256.size();
      revoked += client.key_sha256.empty();
    }
  return wire::Object(
      {{"configured", wire::Boolean(policy != nullptr)},
       {"revision", wire::Unsigned(policy ? policy->revision : 0)},
       {"sha256", wire::String(policy ? AccessPolicyDigest(*policy) : "")},
       {"clients", wire::Unsigned(policy ? policy->clients.size() : 0)},
       {"active_keys", wire::Unsigned(active)},
       {"revoked_clients", wire::Unsigned(revoked)},
       {"history_keys", wire::Unsigned(policy ? policy->key_owners.size() : 0)},
       {"credential_values_redacted", wire::Boolean(true)}});
}
}  // namespace q4t::service::ingress
