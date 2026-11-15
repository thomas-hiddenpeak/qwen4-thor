#pragma once

#include <vector>

#include "q4t/server/responses_mcp.h"

namespace q4t::server {
std::string ResponsesMcpModelName(std::string_view server_label,
                                  std::string_view name);
struct ResponsesMcpBinding {
  size_t declaration_index = 0;
  std::string server_label;
  std::string name;
  std::string model_name;
  std::string catalog_id;
  io::Json tool;
  io::Json function;
  bool direct = false;
  bool programmatic = false;
  bool approval = true;
};

// Pure model projection, NOT execution authority. The execution owner must
// authenticate every catalog and original schema before passing it here.
// Public server/tool names remain intact; private aliases cannot expose URLs
// or credentials and cannot collide between servers with equal tool names.
Status PlanResponsesMcpBindings(
    const std::vector<ResponsesMcpDefinition>& declarations,
    const io::Json& catalogs, std::vector<ResponsesMcpBinding>* bindings,
    ChatPlanError* error);

// Resolve the most recent catalog per server from already authenticated
// history. Definition-only validation grants no callable and does no I/O.
Status PlanResponsesMcpRequestBindings(
    const io::Json& request, std::vector<ResponsesMcpBinding>* bindings,
    ChatPlanError* error, bool definitions_only = false);

// Maps both direct MCP selectors and MCP members of allowed_tools. An absent
// callable is an error; a forced call must never silently become plain text.
Status MapResponsesMcpChoice(const io::Json& choice,
                             const std::vector<ResponsesMcpBinding>& bindings,
                             io::Json* mapped, ChatPlanError* error);
}  // namespace q4t::server
