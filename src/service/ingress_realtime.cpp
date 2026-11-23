#include "q4t/service/ingress_access.h"

namespace q4t::service::ingress {
AccessIdentity ClientAccess::AuthenticateRealtime(
    const server::RealtimeClientTicket& ticket, std::string_view binding,
    int64_t now) const {
  AccessIdentity result;
  const auto policy = current_.load();
  if (!policy) return result;
  for (size_t i = 0; i < policy->clients.size(); ++i) {
    const auto& row = policy->clients[i];
    if (row.id != ticket.owner) continue;
    for (size_t n = 0; n < row.key_sha256.size(); ++n) {
      if (server::VerifyRealtimeTicket(ticket, binding, row.key_sha256[n], now)) {
        result.policy_ = policy;
        result.index_ = i;
        result.key_ = n;
        return result;
      }
    }
  }
  return result;
}
}  // namespace q4t::service::ingress
