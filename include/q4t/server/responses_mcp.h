#pragma once

#include "q4t/server/chat_plan.h"
#include "q4t/server/mcp_transport.h"

namespace q4t::server {
struct ResponsesMcpDefinition {
  // Public declaration, with ALL credentials removed. Request-local secrets
  // must be supplied again on continuation, never reconstructed from storage.
  io::Json declaration;
  McpEndpoint credentials;
  std::string target_kind;
  std::string target_id;
  // Private resolved deployment identity; never a public request field.
  std::string target_binding;
  bool required = false;
};
Status PlanResponsesMcpDefinition(const io::Json& input,
                                  ResponsesMcpDefinition* output,
                                  ChatPlanError* error,
                                  const std::string& path = "tools");
// Before any durable request/prompt/conversation publication, extract secrets
// only from actual tool declarations (including discovery input items). Never
// walk arbitrary tool arguments or user JSON by matching a field name.
Status CaptureResponsesMcpCredentials(
    io::Json* request, std::vector<ResponsesMcpDefinition>* credentials,
    ChatPlanError* error);
bool ResponsesMcpHasCredentials(const ResponsesMcpDefinition& definition);
void BindResponsesMcpCredentials(
    ResponsesMcpDefinition* definition,
    const std::vector<ResponsesMcpDefinition>& credentials);
// Apply only validated declarations and catalog entries; these predicates do
// not establish stored-item authority or approve a dispatch.
bool ResponsesMcpToolAllowed(const io::Json& declaration,
                             const io::Json& remote_tool);
bool ResponsesMcpApprovalRequired(const io::Json& declaration,
                                  const io::Json& remote_tool);
// Preserve complete invocation-time remote schemas privately; public SDK
// projection uses input_schema, not the MCP transport's inputSchema spelling.
Status ProjectResponsesMcpCatalog(const ResponsesMcpDefinition& definition,
                                  const io::Json& native_tools,
                                  io::Json* public_tools, io::Json* authority,
                                  ChatPlanError* error);
}  // namespace q4t::server
