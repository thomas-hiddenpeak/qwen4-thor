#include "q4t/server/realtime_client_secrets.h"

#include "object_protocol.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/client_identity.h"
#include "q4t/server/native_crypto.h"
#include "q4t/server/realtime_client_ticket.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kSettingsBytes = 4ULL << 20;
constexpr size_t kStoreBytes = 64ULL << 20;
constexpr size_t kOwnerBytes = 16ULL << 20;
constexpr size_t kMaxOwnerEntries = 64;
constexpr size_t kMaxEntries = 4096;
struct Wipe {
  std::string& data;
  ~Wipe() { CryptoWipe(&data); }
};
bool PrivateToolFields(const Json& resource) {
  const auto* tools = resource.GetArray("tools");
  if (!tools) return false;
  for (const auto& tool : tools->array) {
    if (tool.GetString("type") == "mcp" &&
        (tool.Has("authorization") || tool.Has("headers")))
      return true;
  }
  return false;
}
ObjectReply Unauthorized() {
  return Error(401, "invalid_api_key",
               "Invalid or expired Realtime client secret");
}
}  // namespace

RealtimeSecretTime RealtimeSecretTime::Now() {
  using namespace std::chrono;
  return {
      duration_cast<seconds>(system_clock::now().time_since_epoch()).count(),
      steady_clock::now()};
}
RealtimeClientSecrets::Entry::~Entry() { CryptoWipe(&settings); }

void RealtimeClientSecrets::Expire(RealtimeSecretTime now) {
  for (auto it = entries_.begin(); it != entries_.end();) {
    const auto& entry = it->second;
    if (entry.expires.unix_seconds <= now.unix_seconds ||
        entry.expires.monotonic <= now.monotonic) {
      bytes_ -= entry.settings.capacity();
      it = entries_.erase(it);
    } else {
      ++it;
    }
  }
}

ObjectReply RealtimeClientSecrets::Issue(RealtimeSecretIssuer issuer,
                                         const Json& request,
                                         const Json& resource,
                                         RealtimeSecretTime now) {
  const auto owner = issuer.owner;
  if (!owner.empty() && !ValidClientId(owner)) return Unauthorized();
  ChatPlanError error;
  auto valid = ValidateRealtimeWireShape(
      request, RealtimeShape::kClientSecretCreate, &error, "body");
  if (!valid) {
    auto reply = Error(error.http_status, error.code, valid.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  const auto* expiration = request.Find("expires_after");
  const int ttl = expiration ? expiration->GetInt("seconds", 600) : 600;
  const auto lifetime = std::chrono::seconds(ttl);
  if (now.unix_seconds < 0 || now.unix_seconds > 9007199254740991LL - ttl ||
      now.monotonic > decltype(now.monotonic)::max() - lifetime)
    return Error(503, "realtime_clock_unavailable",
                 "Credential clock unavailable");
  const auto expires =
      RealtimeSecretTime{now.unix_seconds + ttl, now.monotonic + lifetime};
  const Json fallback = Object({{"type", String("realtime")}});
  const auto* startup = request.Find("session");
  if (!startup) startup = &fallback;
  size_t storage = 0;
  if (!MeasureRealtimeStorage(*startup, kSettingsBytes, &storage))
    return Error(413, "request_too_large",
                 "Credential settings exceed storage limit");
  if (!MeasureRealtimeStorage(resource, kSettingsBytes, &storage) ||
      PrivateToolFields(resource) ||
      !ValidApplicationId(resource.GetString("id")) ||
      resource.GetString("id").size() <= 5 ||
      resource.GetString("type") != startup->GetString("type") ||
      !resource.GetString("id").starts_with("sess_") ||
      resource.GetString("object") !=
          (startup->GetString("type") == "transcription"
               ? "realtime.transcription_session"
               : "realtime.session"))
    return Error(500, "realtime_session_invalid",
                 "Invalid effective session resource");
  std::string settings = OutputJson(*startup);
  Wipe wipe{settings};

  CryptoKey random;
  if (CryptoRandom(random) != CryptoResult::kOk)
    return Error(503, "realtime_secret_unavailable",
                 "Credential generation unavailable");
  std::string token = "ek_" + trace::HexDigest(random);
  CryptoWipe(random);
  Wipe wipe_token{token};
  if (!owner.empty()) {
    std::string signed_token;
    if (!SignRealtimeTicket(issuer.binding, issuer.key_digest, owner, token,
                            expires.unix_seconds, &signed_token))
      return Error(503, "realtime_secret_unavailable", "Credential signing unavailable");
    token.swap(signed_token);
    CryptoWipe(&signed_token);
  }
  auto key = Digest(token);
  ObjectReply reply;
  reply.value = Object({{"value", String(token)},
                        {"expires_at", Number(expires.unix_seconds)},
                        {"session", resource}});
  if (!ValidateRealtimeWireShape(
          reply.value, RealtimeShape::kClientSecretCreated, &error, "response"))
    return Error(500, "realtime_session_invalid",
                 "Invalid credential response");
  std::string scope(owner);
  std::lock_guard lock(mutex_);
  Expire(now);
  size_t count = 0, used = 0;
  for (const auto& [_, entry] : entries_) {
    if (entry.owner == owner) {
      ++count;
      used += entry.settings.capacity();
    }
  }
  if (count >= kMaxOwnerEntries || settings.capacity() > kOwnerBytes - used)
    return Error(429, "client_capacity_exhausted",
                 "Active client secret limit reached");
  if (entries_.size() >= kMaxEntries ||
      settings.capacity() > kStoreBytes - bytes_)
    return Error(503, "realtime_secret_capacity", "Client secret storage full");
  auto [entry, inserted] = entries_.try_emplace(std::move(key));
  if (!inserted)
    return Error(503, "realtime_secret_unavailable",
                 "Credential generation unavailable");
  entry->second.owner.swap(scope);
  entry->second.settings.swap(settings);
  entry->second.expires = expires;
  bytes_ += entry->second.settings.capacity();
  return reply;
}

ObjectReply RealtimeClientSecrets::Resolve(std::string_view token,
                                           RealtimeSecretGrant* grant,
                                           RealtimeSecretTime now) {
  RealtimeClientTicket ticket;
  const bool scoped = ReadRealtimeTicket(token, &ticket);
  if (!scoped && (token.size() != 67 || !token.starts_with("ek_") ||
      token.substr(3).find_first_not_of("0123456789abcdef") != token.npos))
    return Unauthorized();
  const auto key = Digest(token);
  RealtimeSecretGrant next;
  std::string settings;
  Wipe wipe{settings};
  {
    std::lock_guard lock(mutex_);
    const auto it = entries_.find(key);
    if (it == entries_.end()) return Unauthorized();
    if (it->second.expires.unix_seconds <= now.unix_seconds ||
        it->second.expires.monotonic <= now.monotonic) {
      bytes_ -= it->second.settings.capacity();
      entries_.erase(it);
      return Unauthorized();
    }
    next.owner = it->second.owner;
    settings = it->second.settings;
  }
  if (!io::ParseJson(settings, &next.session, true,
                     {64, 65536, kSettingsBytes, true}))
    return Error(500, "realtime_session_invalid",
                 "Invalid stored credential settings");
  *grant = std::move(next);
  return {};
}
}  // namespace q4t::server
