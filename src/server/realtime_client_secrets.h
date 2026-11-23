// Process-owned short-lived grants. An established session owns its settings
// independently of subsequent credential expiry.
#pragma once

#include <chrono>
#include <map>
#include <mutex>

#include "q4t/server/object_store.h"

namespace q4t::server {
struct RealtimeSecretTime {
  int64_t unix_seconds;
  std::chrono::steady_clock::time_point monotonic;
  static RealtimeSecretTime Now();
};

struct RealtimeSecretGrant {
  std::string owner;
  io::Json session;
};

class RealtimeClientSecrets {
 public:
  // Caller supplies an authenticated owner and its resolved public session
  // resource. Only the private startup options are retained for redemption.
  ObjectReply Issue(std::string_view owner, const io::Json& request,
                    const io::Json& resource,
                    RealtimeSecretTime now = RealtimeSecretTime::Now());
  ObjectReply Resolve(std::string_view token, RealtimeSecretGrant* grant,
                      RealtimeSecretTime now = RealtimeSecretTime::Now());

 private:
  struct Entry {
    std::string owner, settings;
    RealtimeSecretTime expires;
    ~Entry();
  };
  void Expire(RealtimeSecretTime now);
  std::mutex mutex_;
  std::map<std::string, Entry, std::less<>> entries_;
  size_t bytes_ = 0;
};
}  // namespace q4t::server
