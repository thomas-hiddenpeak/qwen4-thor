// Frozen connection-local tool selection; no network or dispatch permission.
#pragma once

#include <memory>

#include "q4t/server/realtime_mcp_catalog.h"
#include "q4t/server/realtime_mcp_definitions.h"
#include "q4t/server/responses_mcp_binding.h"

namespace q4t::server {
struct RealtimeMcpContext {
  io::Json tools;
  std::vector<ResponsesMcpDefinition> definitions;
  std::vector<RealtimeMcpCatalog> catalogs;
  std::vector<ResponsesMcpBinding> bindings;
  size_t retained_bytes = 0;
};
// Only successful catalogs selected by the actual import owner may be passed.
// A missing import grants no callable. Full target, policy and credentials
// must match the selected definition, even when the public label is reused.
Status CaptureRealtimeMcpContext(
    const RealtimeMcpSelection& selection,
    const std::vector<RealtimeMcpCatalog>& catalogs,
    std::shared_ptr<const RealtimeMcpContext>* context, ChatPlanError* error);
// Input has the original public Realtime tools and selector. Output uses
// ordinary Responses function adapters with the exact native MCP model names.
// definitions_only is for accepting session settings before tools/list has
// completed; its output MUST NOT be executed or supply output bindings.
Status MapRealtimeMcpTools(const RealtimeMcpContext& context, io::Json* request,
                           bool definitions_only, ChatPlanError* error);
}  // namespace q4t::server
