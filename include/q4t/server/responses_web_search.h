#pragma once

#include "q4t/server/web_execution.h"

namespace q4t::server {
struct ResponsesWebBinding {
  size_t tool_index = 0;
  std::string model_name;
  WebSearchOptions options;
};
Status PlanResponsesWebBindings(const io::Json& request,
                                std::vector<ResponsesWebBinding>* output,
                                ChatPlanError* error);
Status MapResponsesWebChoice(const io::Json& choice,
                             const std::vector<ResponsesWebBinding>& bindings,
                             io::Json* mapped, ChatPlanError* error);
io::Json NativeWebSearchDefinition(std::string name);
Status PlanResponsesWebAction(const io::Json& arguments, WebAction* output);
io::Json ResponsesWebAction(const WebAction& action,
                            std::span<const WebPage> sources = {});
// Exact source-bound, clickable Markdown markers, without mutating generated
// text. These are instructions to the model, never assertions of a citation.
std::string WebCitationMarker(std::string_view url);

// Private, inline context bindings. Only authenticated stored data or an
// actual executor can supply them. Public items contain no private passages.
// Merge selects exact matching item identities and fails on conflicting data.
ObjectReply MergeWebContexts(const io::Json& incoming, const io::Json& items,
                             io::Json* output);
ObjectReply BindWebContext(const io::Json& item,
                           const WebExecutionResult& result,
                           io::Json* bindings);
const io::Json* FindWebContext(const io::Json& bindings, const io::Json& item);
}  // namespace q4t::server
