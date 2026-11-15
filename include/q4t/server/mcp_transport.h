// Native remote MCP transport. Authorization/approval belongs to the caller;
// no model-provided value may supply deployment policy or before_call.
#pragma once

#include <map>

#include "q4t/server/file_fetch.h"

namespace q4t::server {
struct McpEndpoint {
  std::string url;
  // Private request credentials. Never place these in model inputs or logs.
  std::map<std::string, std::string> headers;
  std::string authorization;
};
struct McpTransportReceipt {
  io::Json result;
  io::Json error;
  std::string protocol;
  // Private before_call permission identity, never sent to the remote server.
  // Completion must belong to the request that won the durable dispatch CAS.
  std::string dispatch_id;
  bool legacy_sse = false;
  // Conservatively true after the parent permits the call, even if the child
  // dies before sending. False proves that this invocation did not call it.
  bool call_permitted = false;
  bool outcome_unknown = false;
};

// tools/list collects bounded pages; tools/call and resources/read send at
// most once, after before_call grants the exact invocation/resource. Each
// operation owns a negotiated session, closed before returning. The execution
// layer binds a fresh operation to its authenticated catalog/approval. A
// successful response is not evidence of side-effect-free remote execution.
ObjectReply InvokeMcp(const BinaryAccess& access, const McpEndpoint& endpoint,
                      const FileFetchOptions& deployment,
                      std::string_view method, const io::Json& params,
                      const std::function<ObjectReply()>& before_call,
                      McpTransportReceipt* receipt,
                      GrammarControl control = {});
int MaybeRunMcpWorker(int argc, char** argv);
}  // namespace q4t::server
