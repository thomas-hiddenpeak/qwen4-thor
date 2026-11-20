#include "q4t/server/realtime_mcp_definitions.h"

#include <set>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
Status Invalid(ChatPlanError* error, std::string path, std::string message,
               std::string code = "invalid_request", int http_status = 400) {
  if (error) *error = {std::move(code), std::move(path), http_status};
  return Status::Fail(std::move(message));
}
Status Capacity(ChatPlanError* error) {
  return Invalid(error, "tools", "Realtime MCP definition capacity exhausted",
                 "realtime_mcp_definition_capacity", 413);
}
bool Spend(size_t bytes, size_t* remaining) {
  if (bytes > *remaining) return false;
  *remaining -= bytes;
  return true;
}
bool Charge(const Json& value, size_t* remaining) {
  size_t bytes = 0;
  return MeasureRealtimeStorage(value, *remaining, &bytes) &&
         Spend(bytes, remaining);
}
bool Charge(const ResponsesMcpDefinition& definition, size_t* remaining) {
  if (!Spend(sizeof(definition), remaining) ||
      !Charge(definition.declaration, remaining))
    return false;
  const auto& endpoint = definition.credentials;
  for (const auto* value :
       {&definition.target_kind, &definition.target_id,
        &definition.target_binding, &endpoint.url, &endpoint.authorization,
        &endpoint.tunnel_id, &endpoint.tunnel_generation,
        &endpoint.tunnel_channel, &endpoint.provider_id})
    if (!Spend(value->capacity(), remaining)) return false;
  for (const auto& [name, value] : endpoint.headers)
    if (!Spend(
            sizeof(decltype(endpoint.headers)::value_type) + 4 * sizeof(void*),
            remaining) ||
        !Spend(name.capacity(), remaining) ||
        !Spend(value.capacity(), remaining))
      return false;
  return true;
}
Json Restore(const ResponsesMcpDefinition& definition) {
  auto result = definition.declaration;
  const auto& endpoint = definition.credentials;
  if (!endpoint.authorization.empty())
    Set(&result, "authorization", String(endpoint.authorization));
  if (!endpoint.headers.empty()) {
    auto headers = Object();
    for (const auto& [key, value] : endpoint.headers)
      Set(&headers, key, String(value));
    Set(&result, "headers", std::move(headers));
  }
  return result;
}
bool Authorization(std::string_view name) {
  constexpr std::string_view expected = "authorization";
  if (name.size() != expected.size()) return false;
  for (size_t i = 0; i < name.size(); ++i) {
    const auto c = name[i];
    if ((c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c) != expected[i])
      return false;
  }
  return true;
}
}  // namespace

Status RealtimeMcpDefinitions::Prepare(const Json& tools,
                                       RealtimeMcpSelection* selection,
                                       ChatPlanError* error) const {
  if (!selection || !error)
    return Status::Fail("Missing Realtime MCP definition owner");
  *error = {};
  if (!maximum_servers_ || maximum_servers_ > kMaximumServers ||
      !maximum_bytes_ || maximum_bytes_ > kMaximumBytes)
    return Capacity(error);
  size_t input_bytes = 0;
  if (!MeasureRealtimeStorage(tools, maximum_bytes_, &input_bytes))
    return Capacity(error);
  auto status = ValidateRealtimeWireShape(Object({{"tools", tools}}),
                                          RealtimeShape::kResponseCreate, error,
                                          "response");
  if (!status) return status;
  if (!tools.IsArray())
    return Invalid(error, "tools", "Expected tool declarations");
  RealtimeMcpSelection next;
  next.owner_ = this;
  next.revision_ = revision_;
  next.tools_ = Array();
  next.remembered_ = remembered_;
  std::set<std::string> labels;
  std::set<std::string> function_names;
  for (size_t i = 0; i < tools.array.size(); ++i) {
    const auto& source = tools.array[i];
    const auto path = "tools[" + std::to_string(i) + ']';
    if (source.GetString("type") != "mcp") {
      auto function = source;
      std::erase_if(function.object,
                    [](const auto& field) { return field.first == "type"; });
      std::string param;
      status = ValidateToolDefinitions(
          Object({{"tools", Array({Object({{"type", String("function")},
                                           {"function", function}})})}}),
          &param);
      if (!status) {
        constexpr std::string_view prefix = "tools[0].function";
        return Invalid(error,
                       param.starts_with(prefix)
                           ? path + param.substr(prefix.size())
                           : path,
                       status.message());
      }
      if (!function_names.insert(source.GetString("name")).second)
        return Invalid(error, path + ".name", "Duplicate function name");
      next.tools_.array.push_back(source);
      continue;
    }
    const auto label = source.GetString("server_label");
    if (label.empty() || !labels.insert(label).second)
      return Invalid(error, path + ".server_label",
                     "Empty or duplicate MCP server label");
    auto resolved = source;
    if (!source.Has("server_url") && !source.Has("connector_id") &&
        !source.Has("tunnel_id")) {
      // Only committed definitions are reusable. A failed update, an earlier
      // element of this array, or another connection supplies no authority.
      const auto previous = remembered_.find(label);
      if (previous == remembered_.end())
        return Invalid(error, path + ".server_label",
                       "MCP label has no definition in this session",
                       "realtime_mcp_definition_missing");
      resolved = Restore(previous->second);
      for (const auto& [key, value] : source.object) Set(&resolved, key, value);
    }
    ResponsesMcpDefinition definition;
    status = PlanResponsesMcpDefinition(resolved, &definition, error, path);
    if (!status) return status;
    if (definition.target_kind == "connector_id")
      for (const auto& [name, value] : definition.credentials.headers) {
        (void)value;
        if (Authorization(name))
          return Invalid(error, path + ".headers",
                         "Connector authorization requires its token field");
      }
    if (!next.remembered_.contains(label) &&
        next.remembered_.size() == maximum_servers_)
      return Capacity(error);
    next.tools_.array.push_back(definition.declaration);
    next.remembered_.insert_or_assign(label, definition);
    next.active_.push_back(std::move(definition));
  }
  size_t remaining = maximum_bytes_;
  for (const auto& [label, definition] : next.remembered_)
    if (!Spend(sizeof(decltype(remembered_)::value_type) + 4 * sizeof(void*),
               &remaining) ||
        !Spend(label.capacity(), &remaining) || !Charge(definition, &remaining))
      return Capacity(error);
  next.retained_bytes_ = maximum_bytes_ - remaining;
  // The pending transaction also owns its public projection and immutable
  // active copy. Charge those copies instead of only counting wire bytes.
  if (!Spend(next.active_.capacity() * sizeof(ResponsesMcpDefinition),
             &remaining) ||
      !Charge(next.tools_, &remaining))
    return Capacity(error);
  for (const auto& definition : next.active_)
    if (!Charge(definition, &remaining)) return Capacity(error);
  *selection = std::move(next);
  return {};
}

Status RealtimeMcpDefinitions::Commit(RealtimeMcpSelection* selection,
                                      ChatPlanError* error) {
  if (!selection || !error)
    return Status::Fail("Missing Realtime MCP transaction owner");
  *error = {};
  if (selection->owner_ != this || selection->revision_ != revision_ ||
      revision_ == UINT64_MAX)
    return Invalid(error, "tools", "Realtime MCP transaction is stale",
                   "realtime_mcp_definition_changed", 409);
  remembered_.swap(selection->remembered_);
  retained_bytes_ = selection->retained_bytes_;
  selection->remembered_.clear();
  selection->owner_ = nullptr;
  ++revision_;
  return {};
}
}  // namespace q4t::server
