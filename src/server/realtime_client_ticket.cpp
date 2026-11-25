#include "q4t/server/realtime_client_ticket.h"

#include <charconv>

#include "q4t/server/client_identity.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/native_crypto.h"

namespace q4t::server {
namespace {
bool Hex64(std::string_view value) {
  return value.size() == 64 &&
         value.find_first_not_of("0123456789abcdef") == value.npos;
}
std::string_view Prefix(ClientTicketAudience audience) {
  if (audience == ClientTicketAudience::kChatKitPreview) return "ckpv_q4t1.";
  if (audience == ClientTicketAudience::kChatKitUpload) return "ckup_q4t1.";
  return audience == ClientTicketAudience::kChatKit ? kChatKitTicketPrefix
                                                   : kRealtimeTicketPrefix;
}
std::string Payload(ClientTicketAudience audience, std::string_view owner, std::string_view nonce,
                    int64_t expiry) {
  return std::string(Prefix(audience)) + std::string(owner) + "." +
         std::to_string(expiry) + "." + std::string(nonce);
}
bool Sign(std::string_view binding, std::string_view issuer,
          std::string_view payload, std::string* signature) {
  if (!Hex64(binding) || !Hex64(issuer)) return false;
  const std::string message = "q4t.realtime.client_secret.v1/" +
                              std::string(issuer) + "/" + std::string(payload);
  if (CryptoHmacSha256(binding, message, signature) != CryptoResult::kOk ||
      signature->size() != 44 || signature->back() != '=')
    return false;
  signature->pop_back();
  for (auto& c : *signature) {
    if (c == '+')
      c = '-';
    else if (c == '/')
      c = '_';
  }
  return true;
}
}  // namespace

std::string_view BearerCredential(std::string_view auth) {
  constexpr std::string_view prefix = "bearer ";
  if (auth.size() <= prefix.size()) return {};
  for (size_t i = 0; i < prefix.size(); ++i) {
    char c = auth[i];
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    if (c != prefix[i]) return {};
  }
  return auth.substr(prefix.size());
}

bool RealtimeGrantTarget(std::string_view method, std::string_view path) {
  if (method == "POST") return IsRealtimeOffer(path);
  if (method != "GET") return false;
  if (ValidRealtimeTranslationTarget(path)) return true;
  if (path == "/v1/realtime" || path == "/v1/realtime?intent=transcription")
    return true;
  constexpr std::string_view prefix = "/v1/realtime?";
  if (!path.starts_with(prefix)) return false;
  path.remove_prefix(prefix.size());
  bool model = false, intent = false;
  while (!path.empty()) {
    const auto amp = path.find('&');
    const auto item = path.substr(0, amp);
    if (!intent && item == "intent=realtime") intent = true;
    else if (!model && item.starts_with("model=") && item.size() > 6 &&
             item.find_first_of("#?") == item.npos) model = true;
    else return false;
    if (amp == path.npos) return true;
    path.remove_prefix(amp + 1);
  }
  return false;
}

bool ReadRealtimeTicket(std::string_view token, RealtimeClientTicket* result,
                        ClientTicketAudience audience) {
  const auto prefix = Prefix(audience);
  if (!token.starts_with(prefix) || token.size() > 176 + prefix.size() - kRealtimeTicketPrefix.size())
    return false;
  token.remove_prefix(prefix.size());
  const auto split = [&]() -> std::string_view {
    const auto dot = token.find('.');
    if (dot == token.npos) return {};
    auto field = token.substr(0, dot);
    token.remove_prefix(dot + 1);
    return field;
  };
  const auto owner = split(), seconds = split(), nonce = split();
  if (!ValidClientId(owner) || seconds.empty() || seconds.front() == '0' ||
      seconds.size() > 16 || !Hex64(nonce) || token.size() != 43 ||
      token.find_first_not_of(
          "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_") !=
          token.npos)
    return false;
  int64_t expiry = 0;
  const auto end = seconds.data() + seconds.size();
  const auto parsed = std::from_chars(seconds.data(), end, expiry);
  if (parsed.ec != std::errc{} || parsed.ptr != end || expiry < 1 ||
      expiry > 9007199254740991LL)
    return false;
  *result = {owner, nonce, token, expiry, audience};
  return true;
}

bool SignRealtimeTicket(std::string_view binding, std::string_view issuer,
                        std::string_view owner, std::string_view opaque,
                        int64_t expiry, std::string* token,
                        ClientTicketAudience audience) {
  const auto signed_prefix = Prefix(audience);
  const auto prefix =
      signed_prefix.substr(0, signed_prefix.size() - 5);  // drop "q4t1."
  if (!ValidClientId(owner) || !opaque.starts_with(prefix) ||
      !Hex64(opaque.substr(prefix.size())) || expiry < 1 || expiry > 9007199254740991LL)
    return false;
  auto payload = Payload(audience, owner, opaque.substr(prefix.size()), expiry);
  std::string signature;
  if (!Sign(binding, issuer, payload, &signature)) return false;
  *token = std::move(payload) + "." + signature;
  return true;
}

bool VerifyRealtimeTicket(const RealtimeClientTicket& ticket,
                          std::string_view binding, std::string_view issuer,
                          int64_t now) {
  if (now < 0 || now >= ticket.expires_at || ticket.signature.size() != 43 ||
      !ValidClientId(ticket.owner) || !Hex64(ticket.nonce))
    return false;
  std::string expected;
  if (!Sign(binding, issuer,
            Payload(ticket.audience, ticket.owner, ticket.nonce, ticket.expires_at), &expected))
    return false;
  unsigned difference = 0;
  for (size_t i = 0; i < expected.size(); ++i)
    difference |= static_cast<unsigned char>(expected[i]) ^
                  static_cast<unsigned char>(ticket.signature[i]);
  return difference == 0;
}
bool ResolveForwardedChatKit(std::string_view authorization,
                             std::string_view owner, std::string_view binding,
                             int64_t now, std::string* bearer,
                             ClientTicketAudience audience) {
  constexpr std::string_view prefix = "Q4T-ChatKit ";
  if (!bearer || !authorization.starts_with(prefix)) return false;
  authorization.remove_prefix(prefix.size());
  const auto split = authorization.find(' ');
  if (split != 64) return false;
  const auto issuer = authorization.substr(0, split);
  const auto original = authorization.substr(split + 1);
  RealtimeClientTicket ticket;
  if (audience == ClientTicketAudience::kRealtime ||
      !ReadRealtimeTicket(BearerCredential(original), &ticket, audience) ||
      ticket.owner != owner ||
      !VerifyRealtimeTicket(ticket, binding, issuer, now))
    return false;
  *bearer = std::string(original);
  return true;
}
}  // namespace q4t::server
