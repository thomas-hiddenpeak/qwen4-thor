// Standard Responses file_search definitions and native invocation contract.
#pragma once

#include "q4t/server/vector_search.h"

namespace q4t::server {
inline constexpr size_t kResponsesSearchStores = kVectorSearchStores;
struct ResponsesFileSearchTool {
  std::vector<std::string> store_ids;
  VectorSearchPlan search;
};

struct ResponsesFileSearchBinding {
  size_t tool_index = 0;
  std::string model_name;
  ResponsesFileSearchTool tool;
};

// Deterministic private model names cannot collide with client functions.
// Public declarations and tool_choice remain in their original SDK shape.
Status PlanResponsesFileSearchBindings(
    const io::Json& request, std::vector<ResponsesFileSearchBinding>* bindings,
    ChatPlanError* error);
Status MapResponsesFileSearchChoice(
    const io::Json& choice,
    const std::vector<ResponsesFileSearchBinding>& bindings, io::Json* mapped,
    ChatPlanError* error);

// Native search pages contain verified actual passages. This is a shape
// adapter, not a source authorizer and never a generator of search results.
Status ResponsesFileSearchResults(const io::Json& page, io::Json* results);
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
