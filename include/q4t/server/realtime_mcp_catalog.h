// Native import and SDK projection; session publication belongs to its owner.
#pragma once

#include "q4t/server/responses_mcp_state.h"

namespace q4t::server {
struct RealtimeMcpCatalog {
  ResponsesMcpDefinition definition;
  // Canonical authenticated Responses state, including any listing failure.
  // Never accept this field from a caller as evidence of imported authority.
  io::Json authority;
  io::Json item;
  bool loaded = false;
};
// Pure projection of a validated native catalog, NOT authentication. Realtime
// reports import errors through mcp_list_tools.failed, not an extra item field.
// loaded is true for a successful empty list; a failed list grants no callable.
Status ProjectRealtimeMcpCatalog(const io::Json& catalog, io::Json* item,
                                 bool* loaded, ChatPlanError* error);
// Blocking, finite worker operation. A session must run it outside its reader/
// event owner and publish only after checking the original import ticket.
// Success means a catalog record was committed; inspect loaded for import
// success. Failure/unknown commit leaves output unchanged and grants nothing.
ObjectReply ImportRealtimeMcpCatalog(ObjectStore& objects,
                                     const ChatPlanConfig& config,
                                     const ResponsesMcpDefinition& definition,
                                     const StateAccess& access,
                                     std::string reserved_id,
                                     RealtimeMcpCatalog* output);
}  // namespace q4t::server
