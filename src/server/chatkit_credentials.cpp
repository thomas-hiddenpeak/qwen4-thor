#include "chatkit_credentials.h"

#include "object_protocol.h"
#include "q4t/server/realtime_client_ticket.h"

namespace q4t::server::chatkit_detail {
namespace {
using namespace object_detail;
constexpr std::string_view kPrefix = "cksec_";
StateCipherBinding Stored(const std::string& session) {
  return {"q4t.chatkit", "session", "stored_client_secret", session};
}
ObjectReply Denied() {
  return Error(401, "invalid_client_secret", "Invalid or expired ChatKit session");
}
}  // namespace

ObjectReply IssueSecret(ObjectStore& objects, const StateAccess& issuer,
                        const ChatKitIssuer& authority, int64_t expires_at,
                        std::string* session, std::string* token,
                        std::string* sealed_token) {
  CryptoKey entropy;
  if (CryptoRandom(entropy) != CryptoResult::kOk)
    return Error(503, "chatkit_secret_unavailable", "No session entropy");
  auto nonce = trace::HexDigest(entropy);
  CryptoWipe(entropy);
  *session = "cksess_" + Digest(nonce);
  auto opaque = std::string(kPrefix) + nonce;
  CryptoWipe(&nonce);
  if (issuer.scope == "local:anonymous") {
    *token = std::move(opaque);
  } else {
    if (!issuer.scope.starts_with("client:") ||
        !SignRealtimeTicket(authority.binding, authority.key_digest,
                            std::string_view(issuer.scope).substr(7), opaque,
                            expires_at, token, ClientTicketAudience::kChatKit)) {
      CryptoWipe(&opaque);
      return Error(503, "chatkit_issuer_unavailable", "Invalid session issuer");
    }
    CryptoWipe(&opaque);
  }
  StateCipher cipher(objects);
  auto sealed = cipher.Seal(*token, Stored(*session), issuer, sealed_token);
  if (!sealed.ok()) CryptoWipe(token);
  return sealed;
}

ObjectReply Redeem(ObjectStore& objects, const StateAccess& access,
                   std::string_view token, int64_t now, Grant* grant) {
  RealtimeClientTicket ticket;
  std::string_view nonce;
  if (ReadRealtimeTicket(token, &ticket, ClientTicketAudience::kChatKit)) {
    if (access.scope != "client:" + std::string(ticket.owner) ||
        now >= ticket.expires_at) return Denied();
    nonce = ticket.nonce;
  } else {
    if (access.scope != "local:anonymous" || !token.starts_with(kPrefix))
      return Denied();
    nonce = token.substr(kPrefix.size());
    if (nonce.size() != 64 || nonce.find_first_not_of("0123456789abcdef") != nonce.npos)
      return Denied();
  }
  ObjectRequest request;
  request.scope = access.scope;
  request.kind = ObjectKind::kChatKitSession;
  request.id = "cksess_" + Digest(nonce);
  for (int attempt = 0; attempt != 4; ++attempt) {
    request.operation = ObjectOperation::kGet;
    auto row = objects.Execute(request, access.deadline, access.cancelled);
    if (!row.ok()) return row.http_status == 404 ? Denied() : row;
    request.data = Field(row.value, "data");
    const auto* session = request.data.Find("session");
    if (!session || session->GetString("id") != request.id ||
        session->GetString("status") != "active" ||
        session->GetInt("expires_at") <= now ||
        request.data.GetString("token_sha256") != Digest(token)) return Denied();
    const auto expires_at = session->GetInt("expires_at");
    const auto limit = session->GetInt("max_requests_per_1_minute");
    if (limit < 1) return Denied();
    // One entry per second bounds the sliding minute at 60 buckets.
    auto buckets = Array();
    int64_t used = 0;
    if (const auto* prior = request.data.GetArray("requests")) {
      if (prior->array.size() > 60)
        return Error(503, "chatkit_record_invalid", "Invalid session usage");
      for (const auto& bucket : prior->array) {
        const auto second = bucket.GetInt("second", -1);
        const auto count = bucket.GetInt("count", -1);
        if (second < 0 || count < 1 || count > limit || used > limit - count)
          return Error(503, "chatkit_record_invalid", "Invalid session usage");
        if (second > now)
          return Error(503, "chatkit_clock_unavailable", "Session clock moved backward");
        if (second <= now - 60) continue;
        used += count;
        buckets.array.push_back(bucket);
      }
    }
    if (used >= limit)
      return Error(429, "rate_limit_exceeded", "ChatKit session rate limit reached");
    if (!buckets.array.empty() && buckets.array.back().GetInt("second") == now) {
      auto& last = buckets.array.back();
      Set(&last, "count", Number(last.GetInt("count") + 1));
    } else {
      buckets.array.push_back(Object({{"second", Number(now)}, {"count", Number(1)}}));
    }
    Set(&request.data, "requests", std::move(buckets));
    request.operation = ObjectOperation::kPut;
    request.expected_revision = row.value.GetInt("revision");
    request.expires_at = row.value.GetInt("expires_at");
    request.mutate_before = expires_at;
    auto applied = objects.Execute(request, access.deadline, access.cancelled);
    if (applied.ok()) {
      *grant = {request.id, std::move(request.data)};
      return {};
    }
    if (applied.code != "object_revision_conflict") return applied;
  }
  return Error(409, "chatkit_session_busy", "Session changed during admission");
}

ObjectReply RestoreSecret(StateCipher& cipher, const StateAccess& issuer,
                          const std::string& session, std::string_view sealed,
                          std::string* token) {
  auto reply = cipher.Open(sealed, Stored(session), issuer, token);
  if (reply.http_status == 400)
    return Error(503, "chatkit_record_invalid", "Invalid stored ChatKit session");
  return reply;
}
}  // namespace q4t::server::chatkit_detail
