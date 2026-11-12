#include "q4t/server/responses_tool_search.h"

#include <unordered_set>

#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_input.h"
#include "q4t/server/request_json.h"
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
  auto status =
      InputFields(value, {"type", "execution", "description", "parameters"},
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
        auto status =
            ValidationCopy(&normalized.array[i], error,
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

Status PlanResponsesToolCatalog(const Json& tools, ResponsesToolCatalog* result,
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
        return Invalid(error, path,
                       "Only one tool search configuration allowed");
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
  auto status = NormalizeResponsesCallableTools(
      Object({{"tools", validating}}), &normalized, &choice, error, &effective);
  if (!status) {
    for (size_t i = 0; i < source_indices.size(); ++i) {
      const auto prefix = "tools[" + std::to_string(i) + ']';
      if (error->param.starts_with(prefix)) {
        error->param.replace(
            0, prefix.size(),
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
    return Invalid(error, "tool_search",
                   "Hosted discovery requires server mode");
  auto status =
      InputFields(arguments, {"paths"}, &error->param, "tool_search.arguments");
  if (!status) return status;
  const auto* values = arguments.GetArray("paths");
  if (!values || values->array.empty())
    return Invalid(error, "tool_search.arguments.paths",
                   "Expected public paths");
  if (values->array.size() > kResponsesToolSearchPaths)
    return Invalid(error, "tool_search.arguments.paths",
                   "Search path working set exceeds request budget", true);
  std::unordered_set<std::string> paths;
  for (const auto& value : values->array) {
    if (!value.IsString() || value.str.empty() || value.str.size() > 256)
      return Invalid(error, "tool_search.arguments.paths",
                     "Invalid public path");
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
      if (paths.contains(tool.GetString("name") + '.' +
                         child.GetString("name")))
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

Json ResponsesToolSearchArgumentsSchema(const ResponsesToolCatalog& catalog) {
  if (catalog.search.GetString("execution") == "client") {
    if (const auto* schema = catalog.search.Find("parameters");
        schema && !schema->IsNull())
      return *schema;
    return Object(
        {{"type", String("object")}, {"additionalProperties", Boolean(true)}});
  }
  auto schema = Object(
      {{"type", String("object")},
       {"properties",
        Object({{"paths",
                 Object({{"type", String("array")},
                         {"items", Object({{"type", String("string")},
                                           {"minLength", Number(1)},
                                           {"maxLength", Number(256)}})},
                         {"minItems", Number(1)},
                         {"maxItems", Number(kResponsesToolSearchPaths)}})}})},
       {"required", Array({String("paths")})},
       {"additionalProperties", Boolean(false)}});
  if (const auto* declared = catalog.search.Find("parameters");
      declared && !declared->IsNull()) {
    auto extra = *declared;
    OutputSchemaGraph graph;
    if (graph.Init(extra, false, nullptr, "parameters",
                   OutputSchemaPurpose::kToolResult))
      for (const auto* node : graph.Nodes())
        for (auto& [key, value] : const_cast<Json*>(node)->object)
          if (key == "$ref")
            value.str = "#/$defs/q4t_server_parameters" + value.str.substr(1);
    // A local reference and its sibling constraints both apply. This keeps
    // the original schema intact while requiring bounded hosted paths.
    Set(&schema, "$defs",
        Object({{"q4t_server_parameters", std::move(extra)}}));
    Set(&schema, "$ref", String("#/$defs/q4t_server_parameters"));
  }
  return schema;
}

Status NativeResponsesToolSearchDefinition(const ResponsesToolCatalog& catalog,
                                           Json* definition,
                                           ChatPlanError* error) {
  *definition = {};
  *error = {};
  if (catalog.search.IsNull())
    return Invalid(error, "tools", "Missing tool search configuration");
  auto schema = ResponsesToolSearchArgumentsSchema(catalog);
  OutputSchemaGraph graph;
  auto status =
      graph.Init(schema, false, &error->param, "tool_search.parameters",
                 OutputSchemaPurpose::kToolResult);
  if (!status) return status;
  // The search schema describes arbitrary JSON, not necessarily a schema the
  // generation grammar can encode. A stable JSON-text adapter preserves all
  // roots/references without weakening the real pre-publication validator.
  auto parameters = Object(
      {{"type", String("object")},
       {"properties",
        Object(
            {{"arguments_json",
              Object(
                  {{"type", String("string")},
                   {"description",
                    String(
                        "The complete public search input "
                        "as JSON text. Preserve exact numbers and JSON types. "
                        "This decoded document must match: " +
                        OutputJson(schema))}})}})},
       {"required", Array({String("arguments_json")})},
       {"additionalProperties", Boolean(false)}});
  std::string description =
      "Discover and load tools before calling them. Pass the search input "
      "as JSON text in the arguments_json parameter. ";
  if (catalog.search.GetString("execution") == "server") {
    description +=
        "Use paths from this directory; a namespace path loads its deferred "
        "members. Unlisted tools are unavailable until discovery completes. "
        "Directory: " +
        OutputJson(catalog.summaries);
  } else {
    description += "The application will return the matching tool definitions.";
  }
  if (!catalog.search.GetString("description").empty())
    description += "\n" + catalog.search.GetString("description");
  *definition = Object(
      {{"type", String("function")},
       {"function",
        Object({{"name", String(std::string(kResponsesToolSearchModelName))},
                {"description", String(std::move(description))},
                {"parameters", std::move(parameters)},
                {"strict", Boolean(true)}})}});
  return {};
}

Status DecodeResponsesToolSearchArguments(std::string_view model_arguments,
                                          Json* arguments) {
  *arguments = {};
  Json wrapped;
  auto status = io::ParseJson(std::string(model_arguments), &wrapped, true,
                              kSchemaJsonLimits);
  if (!status) return status;
  if (!wrapped.IsObject() || wrapped.object.size() != 1 ||
      !wrapped.Find("arguments_json") ||
      !wrapped.Find("arguments_json")->IsString())
    return Status::Fail(
        "Search call requires one arguments_json text parameter");
  return io::ParseJson(wrapped.GetString("arguments_json"), arguments, true,
                       kSchemaJsonLimits);
}
}  // namespace q4t::server
