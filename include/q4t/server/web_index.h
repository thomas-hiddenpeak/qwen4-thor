// Scoped native page snapshots. This owner never performs network/model work.
#pragma once

#include "q4t/server/web_search.h"

namespace q4t::server {
struct WebIndexSnapshot {
  std::string id, policy_sha256;
  int64_t revision = 0, observed_at = 0, expires_at = 0;
  std::vector<WebPage> pages;
};
class WebIndexStore {
 public:
  explicit WebIndexStore(ObjectStore& objects) : objects_(objects) {}
  ObjectReply Read(const BinaryAccess& access, std::string_view policy_sha256,
                   WebIndexSnapshot* output);
  // A private configuration identity includes seeds, network policy, request
  // restrictions and parser profile. No caller field grants this identity.
  // CAS publishes the full acquired set atomically. A lost receipt returns
  // the original ID, never performs a second publication or repeats a fetch.
  ObjectReply Publish(const BinaryAccess& access,
                      std::string_view policy_sha256,
                      std::span<const WebPageReceipt> pages,
                      int64_t expected_revision, int ttl_seconds,
                      WebIndexSnapshot* output);
  ObjectReply Erase(const BinaryAccess& access, std::string_view policy_sha256,
                    int64_t expected_revision);

 private:
  ObjectStore& objects_;
};
}  // namespace q4t::server
