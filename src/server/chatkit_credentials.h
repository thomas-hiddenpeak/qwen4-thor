#pragma once

#include "q4t/server/chatkit.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server::chatkit_detail {
struct Grant {
  std::string session;
  io::Json data;
};
// The native access policy supplies the active issuing key and gateway binding.
// Persist the digest and encrypted copy. A public session ID cannot redeem it.
ObjectReply IssueSecret(ObjectStore& objects, const StateAccess& issuer,
                        const ChatKitIssuer& authority, int64_t expires_at,
                        std::string* session, std::string* token,
                        std::string* sealed_token);
ObjectReply Redeem(ObjectStore& objects, const StateAccess& access,
                   std::string_view token, int64_t now, Grant* grant);
ObjectReply RestoreSecret(StateCipher& cipher, const StateAccess& issuer,
                          const std::string& session, std::string_view sealed,
                          std::string* token);
}  // namespace q4t::server::chatkit_detail
