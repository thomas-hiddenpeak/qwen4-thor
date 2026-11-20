#include "q4t/server/realtime_mcp_context.h"

#include <algorithm>
#include <set>

#include "object_protocol.h"
#include "q4t/server/realtime_conversation.h"

namespace q4t::server {
namespace {
using namespace object_detail;
Status Invalid(ChatPlanError* error, std::string message,
               std::string param = "tools") {
  *error = {"realtime_mcp_context_invalid", std::move(param), 400};
  return Status::Fail(std::move(message));
}
bool Same(const ResponsesMcpDefinition& left,
          const ResponsesMcpDefinition& right) {
  return left.target_kind == right.target_kind &&
         left.target_id == right.target_id &&
         OutputJson(left.declaration) == OutputJson(right.declaration) &&
         left.credentials.authorization == right.credentials.authorization &&
         left.credentials.headers == right.credentials.headers;
}
bool Spend(size_t bytes, size_t* remaining) {
  if (bytes > *remaining) return false;
  *remaining -= bytes;
  return true;
}
bool Charge(const Json& value, size_t* remaining) {
  size_t size = 0;
  return MeasureRealtimeStorage(value, *remaining, &size) &&
         Spend(size, remaining);
}
bool Charge(const ResponsesMcpDefinition& definition, size_t* remaining) {
  if (!Charge(definition.declaration, remaining)) return false;
  const auto& endpoint = definition.credentials;
  for (const auto* value :
       {&definition.target_kind, &definition.target_id,
        &definition.target_binding, &endpoint.url, &endpoint.authorization,
        &endpoint.tunnel_id, &endpoint.tunnel_generation,
        &endpoint.tunnel_channel, &endpoint.provider_id})
    if (!Spend(value->capacity(), remaining)) return false;
  for (const auto& [key, value] : endpoint.headers)
    if (!Spend(sizeof(decltype(endpoint.headers)::value_type) +
                   4 * sizeof(void*) + key.capacity() + value.capacity(),
               remaining))
      return false;
  return true;
}
bool Measure(const RealtimeMcpContext& context, size_t* bytes) {
  constexpr size_t maximum = 32ULL << 20;
  size_t remaining = maximum;
  if (!Spend(sizeof(context), &remaining) ||
      !Charge(context.tools, &remaining) ||
      !Spend(context.definitions.capacity() * sizeof(ResponsesMcpDefinition) +
                 context.catalogs.capacity() * sizeof(RealtimeMcpCatalog) +
                 context.bindings.capacity() * sizeof(ResponsesMcpBinding),
             &remaining))
    return false;
  for (const auto& definition : context.definitions)
    if (!Charge(definition, &remaining)) return false;
  for (const auto& catalog : context.catalogs)
    if (!Charge(catalog.definition, &remaining) ||
        !Charge(catalog.item, &remaining) ||
        !Charge(catalog.authority, &remaining))
      return false;
  for (const auto& binding : context.bindings) {
    if (!Charge(binding.tool, &remaining) ||
        !Charge(binding.function, &remaining))
      return false;
    for (const auto* value : {&binding.server_label, &binding.name,
                              &binding.model_name, &binding.catalog_id})
      if (!Spend(value->capacity(), &remaining)) return false;
  }
  *bytes = maximum - remaining;
  return true;
}
Status PublicFunctionChoice(const Json& choice, const Json& tools,
                            ChatPlanError* error) {
  if (choice.GetString("type") == "allowed_tools") {
    if (const auto* selected = choice.GetArray("tools"))
      for (const auto& member : selected->array) {
        const auto status = PublicFunctionChoice(member, tools, error);
        if (!status) return status;
      }
  } else if (choice.GetString("type") == "function") {
    if (std::ranges::none_of(tools.array, [&](const auto& tool) {
          return tool.GetString("type") != "mcp" &&
                 tool.GetString("name") == choice.GetString("name");
        }))
      return Invalid(error, "Function selector is not a declared function",
                     "tool_choice");
  }
  return {};
}
void PublicAliases(Json* choice,
                   const std::vector<ResponsesMcpBinding>& bindings) {
  if (choice->GetString("type") == "allowed_tools") {
    for (auto& member : Field(*choice, "tools").array)
      PublicAliases(&member, bindings);
  } else if (choice->GetString("type") == "function") {
    for (const auto& binding : bindings)
      if (choice->GetString("name") == binding.model_name) {
        Set(choice, "name", *binding.function.Find("name"));
        break;
      }
  }
}
}  // namespace

Status CaptureRealtimeMcpContext(
    const RealtimeMcpSelection& selection,
    const std::vector<RealtimeMcpCatalog>& catalogs,
    std::shared_ptr<const RealtimeMcpContext>* context, ChatPlanError* error) {
  if (!context || !error) return Status::Fail("Missing MCP context owner");
  *error = {};
  if (!selection.Tools().IsArray() || catalogs.size() > 64)
    return Invalid(error, "Invalid MCP selection");
  auto next = std::make_shared<RealtimeMcpContext>();
  next->tools = selection.Tools();
  next->definitions = selection.Active();
  std::set<std::string> labels;
  std::vector<ResponsesMcpDefinition> loaded;
  auto projected = Array();
  for (const auto& catalog : catalogs) {
    const auto label = catalog.item.GetString("server_label");
    const auto found =
        std::ranges::find_if(next->definitions, [&](const auto& definition) {
          return definition.declaration.GetString("server_label") == label;
        });
    Json item;
    bool success = false;
    if (found == next->definitions.end() || !labels.insert(label).second ||
        !Same(*found, catalog.definition) || !catalog.loaded ||
        !ProjectRealtimeMcpCatalog(catalog.authority, &item, &success, error) ||
        !success || OutputJson(item) != OutputJson(catalog.item))
      return Invalid(error, "Catalog differs from the selected native import");
    next->catalogs.push_back(catalog);
    loaded.push_back(catalog.definition);
    projected.array.push_back(catalog.authority);
  }
  auto status =
      PlanResponsesMcpBindings(loaded, projected, &next->bindings, error);
  if (!status) return status;
  if (!Measure(*next, &next->retained_bytes)) {
    *error = {"realtime_mcp_context_capacity", "tools", 413};
    return Status::Fail("MCP context exceeds its retained storage limit");
  }
  *context = std::move(next);
  return {};
}

Status MapRealtimeMcpTools(const RealtimeMcpContext& context, Json* request,
                           bool definitions_only, ChatPlanError* error) {
  if (!request || !error) return Status::Fail("Missing MCP tool projection");
  const auto* original = request->GetArray("tools");
  if (!original || OutputJson(*original) != OutputJson(context.tools))
    return Invalid(error, "Tool selection differs from its frozen context");
  auto tools = Array();
  for (const auto& tool : original->array)
    if (tool.GetString("type") != "mcp") tools.array.push_back(tool);
  const auto ordinary = tools.array.size();
  if (!definitions_only)
    for (const auto& binding : context.bindings)
      tools.array.push_back(binding.function);
  if (const auto* choice = request->Find("tool_choice")) {
    auto status = PublicFunctionChoice(*choice, *original, error);
    if (!status) return status;
    Json mapped;
    status = MapResponsesMcpChoice(*choice, context.bindings, &mapped, error,
                                   definitions_only ? original : nullptr);
    if (!status) return status;
    if (!definitions_only) PublicAliases(&mapped, context.bindings);
    // A settings preview validates policy without pretending a remote import
    // already exists. Actual execution always enforces required callables.
    if (definitions_only && !ordinary && !context.definitions.empty() &&
        mapped.IsString() && mapped.str == "required")
      mapped = String("auto");
    auto next = *request;
    Set(&next, "tools", std::move(tools));
    Set(&next, "tool_choice", std::move(mapped));
    *request = std::move(next);
  } else {
    Set(request, "tools", std::move(tools));
  }
  return {};
}
}  // namespace q4t::server
