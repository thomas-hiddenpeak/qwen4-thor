#pragma once

#include "q4t/server/responses_mcp.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server {
// Private owned state. Only public items returned by these methods may be
// exposed. Credentials never enter this store, including encrypted payloads.
class ResponsesMcpState {
 public:
  ResponsesMcpState(ObjectStore& objects, const ChatPlanConfig& config,
                    std::string response_id = "")
      : objects_(objects),
        cipher_(objects),
        model_(config.model),
        response_id_(std::move(response_id)) {}
  ObjectReply Catalog(const ResponsesMcpDefinition& definition,
                      const io::Json& native_tools, const StateAccess& access,
                      int64_t lifetime_seconds = 30 * 24 * 3600,
                      std::string reserved_id = "",
                      std::string listing_error = "");
  ObjectReply Propose(const io::Json& catalog,
                      const ResponsesMcpDefinition& current, std::string name,
                      io::Json arguments, std::string caller,
                      const StateAccess& access, std::string reserved_id = "",
                      std::string original_arguments = "",
                      const io::Json* program_origin = nullptr);
  // Authenticated reads: OpenCatalog returns PRIVATE original metadata;
  // Authenticate returns the canonical public item, filling omitted optional
  // fields from evidence without trusting caller-supplied results.
  ObjectReply OpenCatalog(const io::Json& catalog,
                          const ResponsesMcpDefinition& current,
                          const StateAccess& access);
  ObjectReply CatalogById(const std::string& id, const StateAccess& access);
  ObjectReply Authenticate(const io::Json& item, const StateAccess& access);
  ObjectReply Decide(const std::string& approval_id, bool approve,
                     std::string reason, const ResponsesMcpDefinition& current,
                     const StateAccess& access);
  // Callback for InvokeMcp's before_call. CAS ready -> dispatching is the
  // sole permission to send. Its lost receipt is NOT permission to send.
  ObjectReply Permit(const std::string& authority_id,
                     const ResponsesMcpDefinition& current,
                     const StateAccess& access);
  ObjectReply Finish(const std::string& authority_id,
                     const McpTransportReceipt& receipt,
                     const ObjectReply& transport, const StateAccess& access);
  // Internal recovery inspection. An unresolved dispatch stays unknown;
  // there is no method to reset it to ready or to replay the remote call.
  ObjectReply Inspect(const std::string& authority_id,
                      const StateAccess& access);
  // Private preparation of a resource_link returned by this exact completed
  // invocation. The URI is never a local path or a new network destination.
  // Ready/dispatching states, receipts and frozen results share the call's
  // encrypted lifetime/capacity. Neither failure nor recovery permits replay.
  ObjectReply ProposeResource(const std::string& authority_id,
                              const std::string& uri,
                              const ResponsesMcpDefinition& current,
                              const StateAccess& access);
  ObjectReply Resource(const std::string& authority_id, const std::string& uri,
                       const StateAccess& access);
  ObjectReply PermitResource(const std::string& authority_id,
                             const std::string& uri,
                             const ResponsesMcpDefinition& current,
                             const StateAccess& access);
  ObjectReply FinishResource(const std::string& authority_id,
                             const std::string& uri,
                             const McpTransportReceipt& receipt,
                             const ObjectReply& transport,
                             const StateAccess& access);
  // Private typed source parts and unresolved URIs. This read grants no remote
  // operation, performs no codec work, and is not yet a ready model request.
  ObjectReply ModelContent(const std::string& authority_id,
                           const StateAccess& access);
  // Publish FileExpansion bindings before releasing its preparation holds.
  // expected_revision comes from ModelContent; a concurrent winner is never
  // overwritten. Unknown publication requires retaining preparation holds.
  ObjectReply BindModelSnapshots(const std::string& authority_id,
                                 int64_t expected_revision,
                                 const io::Json& bindings, bool complete,
                                 const StateAccess& access);
  // Only a proven dead process may be recovered automatically. ended is an
  // internal cleanup proof, supplied only after that exact Response execution
  // has drained; both its response ID and original process identity must match
  // the sealed invocation. No caller input grants this authority. Never retry
  // external work; dispatched work becomes unknown, ready work not executed.
  ObjectReply Recover(const std::string& authority_id,
                      const StateAccess& access,
                      const ResponseStatePlan* ended = nullptr);
  ObjectReply Reconcile(io::Json* items, const ResponseStatePlan& ended,
                        const StateAccess& access);

 private:
  ObjectReply Read(const std::string& id, std::string_view kind,
                   const StateAccess& access, io::Json* record,
                   io::Json* payload);
  ObjectReply Write(const std::string& id, std::string_view kind,
                    io::Json payload, int64_t revision, int64_t expires,
                    const StateAccess& access);
  ObjectStore& objects_;
  StateCipher cipher_;
  std::string model_;
  std::string response_id_;
};
}  // namespace q4t::server
