#include "q4t/server/responses_mcp.h"

#include <algorithm>
#include <set>

#include "file_fetch_internal.h"
#include "mcp_transport_internal.h"
#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/responses_program.h"

namespace q4t::server {
namespace {
using namespace object_detail;
Status Invalid(ChatPlanError* error, std::string path, std::string message,
               bool capacity = false) {
  *error = {capacity ? "mcp_catalog_capacity" : "invalid_request",
            std::move(path), capacity ? 413 : 400};
  return Status::Fail(std::move(message));
}
Status Names(const Json& value, ChatPlanError* error, const std::string& path) {
  if (!value.IsArray()) return Invalid(error, path, "Expected tool names");
  if (value.array.size() > 256)
    return Invalid(error, path, "Too many MCP tool names", true);
  for (size_t i = 0; i < value.array.size(); ++i) {
    const auto& name = value.array[i];
    if (!name.IsString() || name.str.empty())
      return Invalid(error, path + '[' + std::to_string(i) + ']',
                     "Expected nonempty tool name");
    if (name.str.size() > 256)
      return Invalid(error, path, "MCP tool name exceeds capacity", true);
  }
  return {};
}
Status Filter(const Json& value, ChatPlanError* error,
              const std::string& path) {
  auto checked =
      InputFields(value, {"read_only", "tool_names"}, &error->param, path);
  if (!checked) {
    error->code = "invalid_request";
    return checked;
  }
  if (const auto* read = value.Find("read_only"); read && !read->IsBool())
    return Invalid(error, path + ".read_only", "Expected boolean filter");
  if (const auto* names = value.Find("tool_names"))
    return Names(*names, error, path + ".tool_names");
  return {};
}
bool Includes(const Json& names, std::string_view name) {
  return std::any_of(names.array.begin(), names.array.end(),
                     [&](const auto& item) { return item.str == name; });
}
bool Matches(const Json& filter, const Json& tool) {
  if (!filter.IsObject()) return false;
  if (const auto* names = filter.Find("tool_names");
      names && !Includes(*names, tool.GetString("name")))
    return false;
  if (const auto* read = filter.Find("read_only")) {
    if (!read->IsBool()) return false;
    const auto* annotations = tool.Find("annotations");
    // MCP defaults missing readOnlyHint to false. This hint is a filter only,
    // never proof that an external invocation may safely be replayed.
    const bool read_only = annotations && annotations->GetBool("readOnlyHint");
    if (read->boolean != read_only) return false;
  }
  return true;
}
}  // namespace

Status PlanResponsesMcpDefinition(const Json& input,
                                  ResponsesMcpDefinition* output,
                                  ChatPlanError* error,
                                  const std::string& path) {
  if (!output || !error) return Status::Fail("Missing MCP planner output");
  *output = {};
  *error = {};
  auto checked = InputFields(
      input,
      {"type", "server_label", "server_description", "server_url",
       "connector_id", "tunnel_id", "authorization", "headers", "allowed_tools",
       "require_approval", "allowed_callers", "defer_loading"},
      &error->param, path);
  if (!checked) {
    error->code = "invalid_request";
    return checked;
  }
  if (input.GetString("type") != "mcp")
    return Invalid(error, path + ".type", "Expected MCP tool type");
  const auto* label = input.Find("server_label");
  if (!label || !label->IsString() || label->str.empty())
    return Invalid(error, path + ".server_label", "Expected MCP server label");
  if (label->str.size() > 256 || OutputJson(input).size() > (1U << 20))
    return Invalid(error, path, "MCP declaration exceeds capacity", true);
  ResponsesMcpDefinition next;
  for (const auto* field : {"server_url", "connector_id", "tunnel_id"}) {
    if (const auto* target = input.Find(field)) {
      if (!next.target_kind.empty() || !target->IsString() ||
          target->str.empty())
        return Invalid(error, path + '.' + field, "Expected one MCP target");
      next.target_kind = field;
      next.target_id = target->str;
    }
  }
  if (next.target_kind.empty())
    return Invalid(error, path, "An MCP URL, connector or tunnel is required");
  if (next.target_kind == "server_url") {
    file_detail::Url url;
    if (!file_detail::ParseUrl(next.target_id, &url))
      return Invalid(error, path + ".server_url", "Invalid MCP HTTP(S) URL");
    next.credentials.url = url.Text();
  } else {
    if (next.target_id.size() > 256)
      return Invalid(error, path, "MCP target exceeds capacity", true);
    if (next.target_kind == "connector_id") {
      const std::set<std::string> connectors{
          "connector_dropbox",        "connector_gmail",
          "connector_googlecalendar", "connector_googledrive",
          "connector_microsoftteams", "connector_outlookcalendar",
          "connector_outlookemail",   "connector_sharepoint"};
      if (!connectors.contains(next.target_id))
        return Invalid(error, path + ".connector_id", "Invalid connector ID");
    }
  }
  if (const auto* description = input.Find("server_description")) {
    if (!description->IsString())
      return Invalid(error, path + ".server_description",
                     "Expected description");
    if (description->str.size() > 65536)
      return Invalid(error, path, "MCP description exceeds capacity", true);
  }
  if (const auto* auth = input.Find("authorization")) {
    if (!auth->IsString())
      return Invalid(error, path + ".authorization", "Expected token string");
    next.credentials.authorization = auth->str;
  }
  if (const auto* headers = input.Find("headers");
      headers && !headers->IsNull()) {
    if (!headers->IsObject())
      return Invalid(error, path + ".headers", "Expected HTTP headers");
    for (const auto& [key, value] : headers->object) {
      if (!value.IsString())
        return Invalid(error, InputParamPath(path + ".headers", key),
                       "Expected HTTP header string");
      next.credentials.headers.emplace(key, value.str);
    }
  }
  if (!mcp_detail::CheckHeaders(next.credentials).ok())
    return Invalid(error, path + ".headers", "Invalid MCP request credentials");
  if (const auto* defer = input.Find("defer_loading");
      defer && !defer->IsBool())
    return Invalid(error, path + ".defer_loading", "Expected boolean");
  checked = ValidateResponsesAllowedCallers(input, error, path);
  if (!checked) return checked;
  if (const auto* allowed = input.Find("allowed_tools");
      allowed && !allowed->IsNull()) {
    checked = allowed->IsArray()
                  ? Names(*allowed, error, path + ".allowed_tools")
                  : Filter(*allowed, error, path + ".allowed_tools");
    if (!checked) return checked;
  }
  if (const auto* approval = input.Find("require_approval");
      approval && !approval->IsNull()) {
    if (approval->IsString()) {
      if (approval->str != "always" && approval->str != "never")
        return Invalid(error, path + ".require_approval",
                       "Invalid approval mode");
    } else {
      checked = InputFields(*approval, {"always", "never"}, &error->param,
                            path + ".require_approval");
      if (!checked) return checked;
      for (const auto& [key, filter] : approval->object) {
        checked = Filter(filter, error, path + ".require_approval." + key);
        if (!checked) return checked;
      }
    }
  }
  next.declaration = input;
  std::erase_if(next.declaration.object, [](const auto& field) {
    return field.first == "authorization" || field.first == "headers";
  });
  *output = std::move(next);
  return {};
}
bool ResponsesMcpToolAllowed(const Json& declaration, const Json& tool) {
  const auto* filter = declaration.Find("allowed_tools");
  if (!filter || filter->IsNull()) return true;
  return filter->IsArray() ? Includes(*filter, tool.GetString("name"))
                           : Matches(*filter, tool);
}
bool ResponsesMcpApprovalRequired(const Json& declaration, const Json& tool) {
  const auto* approval = declaration.Find("require_approval");
  if (!approval || approval->IsNull()) return true;
  if (approval->IsString()) return approval->str != "never";
  if (const auto* always = approval->Find("always");
      always && Matches(*always, tool))
    return true;
  if (const auto* never = approval->Find("never");
      never && Matches(*never, tool))
    return false;
  return true;
}
Status ProjectResponsesMcpCatalog(const ResponsesMcpDefinition& definition,
                                  const Json& native_tools, Json* public_tools,
                                  Json* authority, ChatPlanError* error) {
  if (!public_tools || !authority || !error)
    return Status::Fail("Missing MCP catalog output");
  *public_tools = Array();
  *authority = Array();
  ResponsesMcpDefinition checked;
  auto status = PlanResponsesMcpDefinition(definition.declaration, &checked,
                                           error, "tools");
  if (!status) return status;
  if (!native_tools.IsArray())
    return Invalid(error, "mcp.tools", "Expected MCP tool list");
  if (native_tools.array.size() > 256 ||
      OutputJson(native_tools).size() > (1U << 20))
    return Invalid(error, "mcp.tools", "MCP catalog exceeds capacity", true);
  auto projected = Array(), original = Array();
  std::set<std::string> names;
  for (size_t i = 0; i < native_tools.array.size(); ++i) {
    const auto& tool = native_tools.array[i];
    const auto path = "mcp.tools[" + std::to_string(i) + ']';
    const auto* name = tool.Find("name");
    const auto* schema = tool.Find("inputSchema");
    if (!name || !name->IsString() || name->str.empty() ||
        name->str.size() > 256 || !names.insert(name->str).second || !schema ||
        !schema->IsObject() || schema->GetString("type") != "object")
      return Invalid(error, path, "Invalid remote tool identity or schema");
    if (const auto* output = tool.Find("outputSchema");
        output &&
        (!output->IsObject() || output->GetString("type") != "object"))
      return Invalid(error, path + ".outputSchema", "Expected output schema");
    if (const auto* description = tool.Find("description");
        description && !description->IsString())
      return Invalid(error, path + ".description", "Expected description");
    if (const auto* annotations = tool.Find("annotations")) {
      if (!annotations->IsObject())
        return Invalid(error, path + ".annotations", "Expected annotations");
      for (const auto* key : {"readOnlyHint", "destructiveHint",
                              "idempotentHint", "openWorldHint"})
        if (const auto* hint = annotations->Find(key); hint && !hint->IsBool())
          return Invalid(error, path + ".annotations." + key,
                         "Expected boolean");
    }
    if (!ResponsesMcpToolAllowed(checked.declaration, tool)) continue;
    auto item = Object({{"name", *name}, {"input_schema", *schema}});
    for (const auto* key : {"description", "annotations"})
      if (const auto* value = tool.Find(key)) Set(&item, key, *value);
    projected.array.push_back(std::move(item));
    original.array.push_back(tool);
  }
  *public_tools = std::move(projected);
  *authority = std::move(original);
  return {};
}
}  // namespace q4t::server
