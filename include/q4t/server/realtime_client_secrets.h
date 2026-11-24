// Process-owned short-lived grants. An established session owns its settings
// independently of subsequent credential expiry.
#pragma once

#include <chrono>
#include <map>
#include <mutex>
#include <optional>

#include "q4t/server/object_store.h"

namespace q4t::server {
struct RealtimeSecretTime {
  int64_t unix_seconds;
  std::chrono::steady_clock::time_point monotonic;
  static RealtimeSecretTime Now();
};

enum class RealtimeSecretAudience { kRealtime, kTranslation };

struct RealtimeSecretIssuer {
  std::string_view owner, binding, key_digest;
  std::optional<double> legacy_temperature = std::nullopt;
  RealtimeSecretAudience audience = RealtimeSecretAudience::kRealtime;
};

struct RealtimeSecretGrant {
  std::string owner;
  io::Json session;
  std::optional<double> legacy_temperature = std::nullopt;
};

class RealtimeClientSecrets {
 public:
  // Caller supplies an authenticated owner and its resolved public session
  // resource. Only the private startup options are retained for redemption.
  ObjectReply Issue(RealtimeSecretIssuer issuer, const io::Json& request,
                    const io::Json& resource,
                    RealtimeSecretTime now = RealtimeSecretTime::Now());
  ObjectReply Resolve(
      std::string_view token, RealtimeSecretGrant* grant,
      RealtimeSecretTime now = RealtimeSecretTime::Now(),
      RealtimeSecretAudience audience = RealtimeSecretAudience::kRealtime);

 private:
  struct Entry {
    std::string owner, settings;
    RealtimeSecretTime expires;
    RealtimeSecretAudience audience = RealtimeSecretAudience::kRealtime;
    std::optional<double> legacy_temperature;
    ~Entry();
  };
  void Expire(RealtimeSecretTime now);
  std::mutex mutex_;
  std::map<std::string, Entry, std::less<>> entries_;
  size_t bytes_ = 0;
};
}  // namespace q4t::server
