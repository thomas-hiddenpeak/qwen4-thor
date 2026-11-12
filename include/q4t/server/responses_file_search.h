// Standard Responses file_search definitions and native invocation contract.
#pragma once

#include "q4t/server/vector_search.h"

namespace q4t::server {
inline constexpr size_t kResponsesSearchStores = 16;
struct ResponsesFileSearchTool {
  std::vector<std::string> store_ids;
  VectorSearchPlan search;
};
Status PlanResponsesFileSearchTool(const io::Json& definition,
                                   ResponsesFileSearchTool* plan,
                                   ChatPlanError* error,
                                   std::string_view path = "tools");
// Native function selection supplies only query strings. Store IDs, filters,
// ranking and result limits always come from the original validated tool.
Status PlanResponsesFileSearchCall(const ResponsesFileSearchTool& tool,
                                   const io::Json& arguments,
                                   VectorSearchPlan* plan,
                                   ChatPlanError* error);
io::Json NativeFileSearchDefinition(std::string name);
}  // namespace q4t::server
