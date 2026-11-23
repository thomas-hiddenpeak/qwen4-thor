#pragma once

#include "q4t/server/agent_vault.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/conversation_state.h"

namespace q4t::server::vault_detail {
struct ListOptions {
  ItemViewOptions paging;
  io::Json metadata;
  bool active = true;
  bool archived = true;
  bool Matches(bool is_archived, const io::Json& metadata) const;
};
ObjectReply ParseListOptions(std::string_view raw, ListOptions* options);
ObjectReply ListPage(io::Json rows, bool more);
std::string_view TrimName(std::string_view name);
ObjectReply CredentialRequest(ObjectStore& store, const VaultRoute& route,
                              std::string_view method, std::string_view query,
                              const std::string& body,
                              const StateAccess& access);
ObjectReply ArchiveCredentials(io::Json* vault);
// Resolve an attached credential for one HTTPS target. Only the transport calls
// this function; the returned token is never a field of the public resource.
ObjectReply ResolveMcpToken(ObjectStore& store, const StateAccess& access,
                            const io::Json& vaults, std::string_view id,
                            const std::string& destination, std::string* token);
enum class CredentialShape { kCreate, kAuthCreate, kAuthRotate, kResource };
Status ValidateCredential(const io::Json& value, CredentialShape shape,
                          ChatPlanError* error);
// The public projection contains no secret fields. Seal the entire validated
// auth configuration so later rotation retains the original destination.
ObjectReply SealCredentialAuth(ObjectStore& store, const StateAccess& access,
                               const std::string& vault, const std::string& id,
                               const io::Json& auth, io::Json* public_auth,
                               std::string* sealed);
ObjectReply RotateCredentialAuth(ObjectStore& store, const StateAccess& access,
                                 const std::string& vault,
                                 const std::string& id,
                                 const std::string& previous,
                                 const io::Json& patch, io::Json* public_auth,
                                 std::string* sealed);
}  // namespace q4t::server::vault_detail
