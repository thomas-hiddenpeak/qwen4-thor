#pragma once

#include "q4t/server/responses_mcp.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server {
// Private owned state. Only public items returned by these methods may be
// exposed. Credentials never enter this store, including encrypted payloads.
class ResponsesMcpState {
 public:
  ResponsesMcpState(ObjectStore& objects, const ChatPlanConfig& config)
      : objects_(objects), cipher_(objects), model_(config.model) {}
  ObjectReply Catalog(const ResponsesMcpDefinition& definition,
                      const io::Json& native_tools, const StateAccess& access,
                      int64_t lifetime_seconds = 30 * 24 * 3600);
  ObjectReply Propose(const io::Json& catalog,
                      const ResponsesMcpDefinition& current, std::string name,
                      io::Json arguments, std::string caller,
                      const StateAccess& access);
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
};
}  // namespace q4t::server
