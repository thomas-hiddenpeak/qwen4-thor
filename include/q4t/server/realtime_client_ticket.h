// Owner and expiry can be parsed before authentication solely to select the
// active gateway binding. Only a verified MAC authorizes a principal.
#pragma once

#include <cstdint>
#include <string>
#include <string_view>

namespace q4t::server {
enum class ClientTicketAudience { kRealtime, kChatKit };
struct RealtimeClientTicket {
  std::string_view owner, nonce, signature;
  int64_t expires_at = 0;
  ClientTicketAudience audience = ClientTicketAudience::kRealtime;
};
inline constexpr std::string_view kRealtimeTicketPrefix = "ek_q4t1.";
inline constexpr std::string_view kChatKitTicketPrefix = "cksec_q4t1.";
std::string_view BearerCredential(std::string_view authorization);
bool RealtimeGrantTarget(std::string_view method, std::string_view path);
bool ReadRealtimeTicket(std::string_view token, RealtimeClientTicket* result,
                        ClientTicketAudience audience = ClientTicketAudience::kRealtime);
bool SignRealtimeTicket(std::string_view binding, std::string_view issuer,
                        std::string_view owner, std::string_view opaque,
                        int64_t expires_at, std::string* token,
                        ClientTicketAudience audience = ClientTicketAudience::kRealtime);
bool VerifyRealtimeTicket(const RealtimeClientTicket& ticket,
                          std::string_view binding, std::string_view issuer,
                          int64_t now);
}  // namespace q4t::server
