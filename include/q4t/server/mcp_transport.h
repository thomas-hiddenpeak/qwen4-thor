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
// Private, authenticated session state. Persist only inside sealed MCP
// authority, never in public items, model input, metrics or ordinary logs.
// A remote session is not permission to dispatch any tool or resource.
struct McpSession {
  io::Json state;
  // Called before remote work with newly initialized/restored private state.
  // A failed or unknown commit prevents dispatch. No callback is retained.
  std::function<ObjectReply(const io::Json&)> checkpoint;
  // Resources returned by a call belong to that call's original session.
  // They must not silently migrate to a replacement session.
  bool allow_new = true;
  // Native callers may tighten the fixed ceilings; not public request fields.
  int idle_ms = 300000;
  int lifetime_ms = 3600000;
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
// optional private session reuses a bounded native owner across operations.
// Without it, the negotiated session is closed before returning. The execution
// layer binds each operation to its authenticated catalog/approval. A
// successful response is not evidence of side-effect-free remote execution.
ObjectReply InvokeMcp(const BinaryAccess& access, const McpEndpoint& endpoint,
                      const FileFetchOptions& deployment,
                      std::string_view method, const io::Json& params,
                      const std::function<ObjectReply()>& before_call,
                      McpTransportReceipt* receipt, GrammarControl control = {},
                      McpSession* session = nullptr);
// Nonblocking collection of an already-exited native session owner.
void PollMcpSessions();
// Explicit bounded shutdown of this process's idle MCP owner only. Active
// exchanges are never interrupted by this function.
bool CloseMcpSessions();
int MaybeRunMcpWorker(int argc, char** argv);
}  // namespace q4t::server
