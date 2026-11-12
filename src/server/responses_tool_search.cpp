#include "q4t/server/responses_tool_search.h"

#include <unordered_set>

#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_input.h"
#include "q4t/server/schema_graph.h"
#include "responses_json.h"

namespace q4t::server {
using namespace responses_detail;
namespace {
Status Invalid(ChatPlanError* error, std::string path, std::string text,
               bool capacity = false) {
  *error = {capacity ? "tool_search_resource_exhausted" : "invalid_request",
            std::move(path), capacity ? 503 : 400};
  return Status::Fail(std::move(text));
}

Status DeclarationSchema(const Json& schema, ChatPlanError* error,
                          const std::string& path) {
  // This validates an input contract, including open/non-strict schemas.
  // Generation guarantees are selected later by the shared constraint planner.
  OutputSchemaGraph graph;
  auto status = graph.Init(schema, false, &error->param, path,
                            OutputSchemaPurpose::kToolResult);
  if (!status) {
    error->code = "invalid_tool_search_schema";
    if (status.message().starts_with("schema_resource_exhausted:")) {
      error->code = "tool_search_resource_exhausted";
      error->http_status = 503;
    }
  }
  return status;
}

Status SearchDefinition(const Json& value, ChatPlanError* error,
                         const std::string& path) {
  auto status = InputFields(value, {"type", "execution", "description",
                                    "parameters"},
                            &error->param, path);
  if (!status) return status;
  if (const auto* execution = value.Find("execution");
      execution && (!execution->IsString() ||
                    (execution->str != "server" && execution->str != "client")))
    return Invalid(error, path + ".execution", "Invalid tool search execution");
  if (const auto* description = value.Find("description");
      description && !description->IsNull() && !description->IsString())
    return Invalid(error, path + ".description", "Expected text or null");
  if (const auto* parameters = value.Find("parameters");
      parameters && !parameters->IsNull()) {
    status = DeclarationSchema(*parameters, error, path + ".parameters");
    if (!status) return status;
  }
  return {};
}

Status ValidationCopy(Json* tool, ChatPlanError* error,
                       const std::string& path) {
  if (tool->GetString("type") == "namespace") {
    if (const auto* children = tool->GetArray("tools")) {
      auto normalized = *children;
      for (size_t i = 0; i < normalized.array.size(); ++i) {
        auto status = ValidationCopy(&normalized.array[i], error,
                                     path + ".tools[" + std::to_string(i) + ']');
        if (!status) return status;
      }
      Set(tool, "tools", std::move(normalized));
    }
    return {};
  }
  if (const auto* flag = tool->Find("defer_loading")) {
    if (!flag->IsBool())
      return Invalid(error, path + ".defer_loading", "Expected boolean");
    // The shared validator checks all existing callable/schema contracts.
    // Do not change the public definition retained in the inventory.
    Set(tool, "defer_loading", Boolean(false));
  }
  if (tool->GetString("type") == "function") {
    if (const auto* parameters = tool->Find("parameters");
        parameters && !parameters->IsNull()) {
      auto status = DeclarationSchema(*parameters, error, path + ".parameters");
      if (!status) return status;
    }
  }
  return {};
}

Json Summary(const Json& tool) {
  auto result = Object({{"type", *tool.Find("type")},
                        {"path", *tool.Find("name")},
                        {"name", *tool.Find("name")}});
  if (const auto* description = tool.Find("description"))
    Set(&result, "description", *description);
  return result;
}
}  // namespace

Status PlanResponsesToolCatalog(const Json& tools,
                                ResponsesToolCatalog* result,
                                ChatPlanError* error) {
  *result = {};
  *error = {};
  if (!tools.IsArray()) return Invalid(error, "tools", "Expected tool array");
  if (tools.array.size() > 128 ||
      OutputJson(tools).size() > kResponsesToolCatalogBytes)
    return Invalid(error, "tools", "Tool catalog exceeds request budget", true);
  ResponsesToolCatalog plan;
  plan.eager = plan.deferred = plan.summaries = Array();
  auto validating = Array();
  std::vector<size_t> source_indices;
  for (size_t i = 0; i < tools.array.size(); ++i) {
    const auto& tool = tools.array[i];
    const auto path = "tools[" + std::to_string(i) + ']';
    if (tool.GetString("type") == "tool_search") {
      if (!plan.search.IsNull())
        return Invalid(error, path, "Only one tool search configuration allowed");
      auto status = SearchDefinition(tool, error, path);
      if (!status) return status;
      plan.search = tool;
      if (!plan.search.Has("execution"))
        Set(&plan.search, "execution", String("server"));
      continue;
    }
    auto copy = tool;
    auto status = ValidationCopy(&copy, error, path);
    if (!status) return status;
    validating.array.push_back(std::move(copy));
    source_indices.push_back(i);
  }
  Json normalized, choice, effective;
  auto status = NormalizeResponsesTools(Object({{"tools", validating}}),
                                        &normalized, &choice, error, &effective);
  if (!status) {
    for (size_t i = 0; i < source_indices.size(); ++i) {
      const auto prefix = "tools[" + std::to_string(i) + ']';
      if (error->param.starts_with(prefix)) {
        error->param.replace(0, prefix.size(),
                             "tools[" + std::to_string(source_indices[i]) + ']');
        break;
      }
    }
    return status;
  }
  plan.definitions = tools;
  for (size_t i = 0; i < source_indices.size(); ++i) {
    const auto& original = tools.array[source_indices[i]];
    auto value = effective.array[i];
    const auto restore_flag = [](const Json& source, Json* target) {
      if (const auto* flag = source.Find("defer_loading"))
        Set(target, "defer_loading", *flag);
    };
    if (original.GetString("type") == "namespace") {
      auto& children = Field(value, "tools").array;
      const auto& originals = original.GetArray("tools")->array;
      for (size_t j = 0; j < children.size(); ++j)
        restore_flag(originals[j], &children[j]);
    } else {
      restore_flag(original, &value);
    }
    plan.definitions.array[source_indices[i]] = std::move(value);
  }
  for (const auto& tool : plan.definitions.array) {
    if (tool.GetString("type") == "tool_search") continue;
    if (tool.GetString("type") == "namespace") {
      auto eager = Array(), deferred = Array();
      for (const auto& child : tool.GetArray("tools")->array)
        (child.GetBool("defer_loading") ? deferred : eager)
            .array.push_back(child);
      if (!eager.array.empty()) {
        auto part = tool;
        Set(&part, "tools", std::move(eager));
        plan.eager.array.push_back(std::move(part));
      }
      if (!deferred.array.empty()) {
        auto part = tool;
        Set(&part, "tools", std::move(deferred));
        plan.deferred.array.push_back(std::move(part));
        plan.summaries.array.push_back(Summary(tool));
      }
    } else if (tool.GetBool("defer_loading")) {
      plan.deferred.array.push_back(tool);
      plan.summaries.array.push_back(Summary(tool));
    } else {
      plan.eager.array.push_back(tool);
    }
  }
  *result = std::move(plan);
  return {};
}

Status DiscoverResponsesTools(const ResponsesToolCatalog& catalog,
                              const Json& arguments, Json* tools,
                              ChatPlanError* error) {
  *tools = Array();
  *error = {};
  if (catalog.search.GetString("execution") != "server")
    return Invalid(error, "tool_search", "Hosted discovery requires server mode");
  auto status = InputFields(arguments, {"paths"}, &error->param,
                            "tool_search.arguments");
  if (!status) return status;
  const auto* values = arguments.GetArray("paths");
  if (!values || values->array.empty())
    return Invalid(error, "tool_search.arguments.paths", "Expected public paths");
  if (values->array.size() > kResponsesToolSearchPaths)
    return Invalid(error, "tool_search.arguments.paths",
                   "Search path working set exceeds request budget", true);
  std::unordered_set<std::string> paths;
  for (const auto& value : values->array) {
    if (!value.IsString() || value.str.empty() || value.str.size() > 256)
      return Invalid(error, "tool_search.arguments.paths", "Invalid public path");
    paths.insert(value.str);
  }
  auto result = Array();
  for (const auto& tool : catalog.deferred.array) {
    if (paths.contains(tool.GetString("name"))) {
      result.array.push_back(tool);
      continue;
    }
    if (tool.GetString("type") != "namespace") continue;
    auto children = Array();
    for (const auto& child : tool.GetArray("tools")->array)
      if (paths.contains(tool.GetString("name") + '.' + child.GetString("name")))
        children.array.push_back(child);
    if (!children.array.empty()) {
      auto subset = tool;
      Set(&subset, "tools", std::move(children));
      result.array.push_back(std::move(subset));
    }
  }
  *tools = std::move(result);
  return {};
}
}  // namespace q4t::server
