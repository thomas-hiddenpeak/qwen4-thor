#pragma once

#include <cstddef>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/chat_plan.h"
#include "q4t/status.h"

namespace q4t::server {

// A request-owned inventory, never a process-global or cross-client cache.
// This component does not execute client tools or authorize model calls.
struct ResponsesToolCatalog {
  io::Json definitions;  // Original order with existing Responses defaults.
  io::Json search;       // Validated search configuration, null if absent.
  io::Json eager;        // Definitions callable before discovery.
  io::Json deferred;     // Exact definitions eligible for hosted discovery.
  io::Json summaries;    // Names/descriptions only; no deferred schemas.
};

inline constexpr size_t kResponsesToolCatalogBytes = 2 * 1024 * 1024;
inline constexpr size_t kResponsesToolSearchPaths = 16;
inline constexpr std::string_view kResponsesToolSearchModelName =
    "q4t_builtin_tool_search";

Status PlanResponsesToolCatalog(const io::Json& tools,
                                ResponsesToolCatalog* result,
                                ChatPlanError* error);

// Resolve validated model-selected public paths against this request's
// inventory. Unknown paths return no definitions, never a fabricated tool.
// Returns original definition attributes, including defer_loading; activation
// is owned by the ordered search-result history, not by clearing this flag.
Status DiscoverResponsesTools(const ResponsesToolCatalog& catalog,
                              const io::Json& arguments, io::Json* tools,
                              ChatPlanError* error);

io::Json ResponsesToolSearchArgumentsSchema(
    const ResponsesToolCatalog& catalog);
// One JSON-text argument transports arbitrary public JSON. The isolated
// validator enforces the original search schema before publishing the call.
Status NativeResponsesToolSearchDefinition(const ResponsesToolCatalog& catalog,
                                           io::Json* definition,
                                           ChatPlanError* error);
Status DecodeResponsesToolSearchArguments(std::string_view model_arguments,
                                          io::Json* arguments);

struct ResponsesToolActivation {
  size_t item_index = 0;
  io::Json definitions;
};
struct ResponsesToolHistory {
  ResponsesToolCatalog initial;
  io::Json active;
  std::vector<ResponsesToolActivation> activations;
};

bool HasResponsesToolDiscovery(const io::Json& tools, const io::Json& history);
// Storage shape defaults only. Does not resolve/execute tools or reject other
// builtins that may be stored by Conversations without a local executor.
io::Json ResponsesToolItemDefaults(io::Json item);
// Normalize an already activated set, retaining the request's original hosted
// directory. Public callers must resolve deferred visibility before using it.
Status NormalizeActiveResponsesTools(const io::Json& request,
                                     const ResponsesToolCatalog& catalog,
                                     io::Json* tools, io::Json* choice,
                                     ChatPlanError* error);

// Resolve client-scoped, already-dereferenced history. Definitions are kept at
// their actual input position; later redefinition never mutates an earlier
// activation. The state owner still binds each generated call's original
// output contract and owns persistence/tenant authorization.
Status ResolveResponsesToolHistory(const io::Json& tools,
                                   const io::Json& history,
                                   ResponsesToolHistory* result,
                                   ChatPlanError* error);

}  // namespace q4t::server
