#include "q4t/server/responses_tool_search.h"

#include <algorithm>
#include <deque>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/output_schema.h"
#include "q4t/server/responses_input.h"
#include "q4t/server/responses_item_shape.h"
#include "responses_json.h"

namespace q4t::server {
using namespace responses_detail;
namespace {
Status Invalid(ChatPlanError* error, const std::string& path,
               const std::string& message, bool resource = false) {
  *error = {resource ? "tool_search_resource_exhausted" : "invalid_request",
            path, resource ? 503 : 400};
  return Status::Fail(message);
}
bool Named(const Json& tool) {
  const auto type = tool.GetString("type");
  return type == "function" || type == "custom" || type == "namespace";
}
bool SameSlot(const Json& first, const Json& second) {
  if (first.GetString("type") == "namespace" ||
      second.GetString("type") == "namespace")
    return first.GetString("type") == second.GetString("type") &&
           first.GetString("name") == second.GetString("name");
  if (Named(first) || Named(second))
    return Named(first) && Named(second) &&
           first.GetString("name") == second.GetString("name");
  return first.GetString("type") == second.GetString("type");
}
void Merge(Json* active, const Json& additions) {
  for (const auto& tool : additions.array) {
    auto found = std::find_if(active->array.begin(), active->array.end(),
                              [&](const auto& old) { return SameSlot(old, tool); });
    if (found == active->array.end()) {
      active->array.push_back(tool);
    } else if (tool.GetString("type") == "namespace") {
      auto children = *found->Find("tools");
      Merge(&children, *tool.Find("tools"));
      *found = tool;
      Set(&*found, "tools", std::move(children));
    } else {
      *found = tool;
    }
  }
}
}  // namespace

Status ResolveResponsesToolHistory(const Json& tools, const Json& history,
                                   ResponsesToolHistory* result,
                                   ChatPlanError* error) {
  *result = {};
  *error = {};
  if (!history.IsArray() || history.array.size() > 4096)
    return Invalid(error, "input", "Expected bounded resolved input history");
  ResponsesToolHistory plan;
  auto status = PlanResponsesToolCatalog(tools, &plan.initial, error);
  if (!status) return status;
  plan.active = plan.initial.eager;
  if (!plan.initial.search.IsNull())
    plan.active.array.push_back(plan.initial.search);
  std::unordered_set<std::string> identities;
  std::unordered_map<std::string, size_t> clients;
  std::deque<size_t> servers;
  constexpr size_t kHistoryBytes = 8 * 1024 * 1024;
  size_t loaded_bytes = 0;
  for (size_t i = 0; i < history.array.size(); ++i) {
    const auto& item = history.array[i];
    const auto type = item.GetString("type");
    if (type != "tool_search_call" && type != "tool_search_output" &&
        type != "additional_tools")
      continue;
    const auto path = "input[" + std::to_string(i) + ']';
    auto projected = item;
    ResponsesToolCatalog checked;
    if (const auto* definitions = item.GetArray("tools")) {
      status = PlanResponsesToolCatalog(*definitions, &checked, error);
      if (!status) {
        error->param = path + '.' + error->param;
        return status;
      }
      // REST definitions inherit the same defaults as top-level tools.
      // Validate the SDK's fully specified projection without changing input.
      Set(&projected, "tools", checked.definitions);
    }
    status = ValidateResponsesItemShape(projected, ResponsesItemShape::kInput,
                                        error, path);
    if (!status) return status;
    const auto execution = item.GetString("execution", "client");
    const auto call_id = item.GetString("call_id");
    const auto terminal = item.GetString("status", "completed");
    if (type == "tool_search_call") {
      if (execution == "server") {
        if (Nonnull(item, "call_id"))
          return Invalid(error, path + ".call_id", "Server call_id must be null");
        if (terminal == "completed") servers.push_back(i);
      } else {
        if (call_id.empty() || call_id.size() > 256 ||
            !identities.insert(call_id).second)
          return Invalid(error, path + ".call_id", "Invalid/duplicate search call");
        if (terminal == "completed") clients.emplace(call_id, i);
      }
      continue;
    }
    if (type == "tool_search_output") {
      if (execution == "server") {
        if (Nonnull(item, "call_id") || servers.empty())
          return Invalid(error, path + ".call_id", "Orphan server search output");
        if (terminal == "in_progress") continue;
        servers.pop_front();
      } else {
        if (call_id.empty() || !clients.contains(call_id))
          return Invalid(error, path + ".call_id",
                         "Orphan, duplicate or mismatched client search output");
        if (terminal == "in_progress") continue;
        clients.erase(call_id);
      }
      // Incomplete results remain history; their definitions are not callable.
      if (terminal != "completed") continue;
    }
    const auto& definitions = checked.definitions;
    const auto bytes = OutputJson(definitions).size();
    if (plan.activations.size() >= 128 || bytes > kHistoryBytes - loaded_bytes)
      return Invalid(error, path + ".tools", "Loaded history exceeds budget", true);
    auto active = plan.active;
    Merge(&active, definitions);
    if (OutputJson(active).size() > kResponsesToolCatalogBytes)
      return Invalid(error, path + ".tools", "Active tool catalog exceeds budget",
                     true);
    std::vector<ResponsesToolIdentity> names;
    status = PlanResponsesToolIdentities(Object({{"tools", active}}), &names,
                                         error);
    if (!status) {
      error->param = path + '.' + error->param;
      return status;
    }
    loaded_bytes += bytes;
    plan.active = std::move(active);
    plan.activations.push_back({i, definitions});
  }
  if (!clients.empty() || !servers.empty())
    return Invalid(error, "input", "Missing search result for completed call");
  *result = std::move(plan);
  return {};
}
}  // namespace q4t::server
