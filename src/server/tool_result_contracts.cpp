#include "q4t/server/tool_result_contracts.h"

#include <unordered_map>
#include <array>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/tool_result_schema.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kMaxBytes = 8 * 1024 * 1024;
ObjectReply Invalid() {
  return Error(503, "tool_result_contract_invalid",
               "Stored tool result contract is inconsistent");
}
std::string Identity(const Json& tool, std::string_view name_space = {}) {
  const auto type = tool.GetString("type");
  return ResponsesToolModelName(name_space, tool.GetString("name"),
                                type == "function_call"      ? "function"
                                : type == "custom_tool_call" ? "custom"
                                                             : type);
}
struct Declaration {
  const Json* schema;
  std::string param;
};
std::unordered_map<std::string, Declaration> Declarations(const Json& request) {
  std::unordered_map<std::string, Declaration> result;
  const auto add = [&](const Json& tool, const std::string& space,
                       const std::string& at) {
    const auto type = tool.GetString("type");
    if (type == "function" || type == "custom")
      result.emplace(
          Identity(tool, space),
          Declaration{tool.Find("output_schema"), at + ".output_schema"});
  };
  if (const auto* tools = request.GetArray("tools")) {
    for (size_t i = 0; i < tools->array.size(); ++i) {
      const auto& tool = tools->array[i];
      const auto path = "tools[" + std::to_string(i) + ']';
      if (tool.GetString("type") != "namespace") {
        add(tool, "", path);
      } else if (const auto* children = tool.GetArray("tools")) {
        for (size_t j = 0; j < children->array.size(); ++j)
          add(children->array[j], tool.GetString("name"),
              path + ".tools[" + std::to_string(j) + ']');
      }
    }
  }
  return result;
}
Json Binding(const Json& item, const Json& schema) {
  auto result = Object({{"item_id", String(item.GetString("id"))},
                        {"call_id", String(item.GetString("call_id"))},
                        {"type", String(item.GetString("type"))},
                        {"name", String(item.GetString("name"))},
                        {"namespace", String(item.GetString("namespace"))},
                        {"schema", schema}});
  // Omitted/false and omitted/direct remain the pre-async storage contract.
  // The true execution policy must survive tool changes and reject rebinding.
  if (item.GetBool("async")) Set(&result, "async", Boolean(true));
  return result;
}
bool Same(const Json& a, const Json& b) {
  std::string left, right;
  return InputValueIdentity(a, &left) && InputValueIdentity(b, &right) &&
         left == right;
}
ObjectReply Capacity(const Json& contracts) {
  if (contracts.array.size() > 4096 || OutputJson(contracts).size() > kMaxBytes)
    return Error(503, "tool_result_contract_capacity",
                 "Tool result contract capacity exceeded");
  return {};
}
}  // namespace

bool IsResultBoundToolCall(const io::Json& item) {
  const auto type = item.GetString("type");
  return type == "function_call" || type == "custom_tool_call";
}

ObjectReply MergeToolResultContracts(const Json& source, const Json& items,
                                     Json* contracts) {
  if (!items.IsArray() || (!source.IsNull() && !source.IsArray()) ||
      (!contracts->IsNull() && !contracts->IsArray()))
    return Invalid();
  auto reply = Capacity(source);
  if (!reply.ok()) return reply;
  reply = Capacity(*contracts);
  if (!reply.ok()) return reply;
  std::unordered_map<std::string, const Json*> calls;
  for (const auto& item : items.array)
    if (IsResultBoundToolCall(item)) {
      const auto id = item.GetString("id");
      if (!ValidId(id) || !calls.emplace(id, &item).second) return Invalid();
    }
  auto result = Array();
  std::unordered_map<std::string, size_t> selected;
  for (const auto* list : std::array<const Json*, 2>{contracts, &source}) {
    for (const auto& binding : list->array) {
      const auto id = binding.GetString("item_id");
      const auto* schema = binding.Find("schema");
      if (!ValidId(id) || !schema || (!schema->IsNull() && !schema->IsObject()))
        return Invalid();
      const auto found = calls.find(id);
      if (found == calls.end()) continue;
      if (!Same(binding, Binding(*found->second, *schema))) return Invalid();
      const auto [prior, inserted] = selected.emplace(id, result.array.size());
      if (inserted)
        result.array.push_back(binding);
      else if (!Same(result.array[prior->second], binding))
        return Invalid();
    }
  }
  reply = Capacity(result);
  if (reply.ok()) *contracts = std::move(result);
  return reply;
}

ObjectReply BindToolResultContracts(const Json& request, const Json& items,
                                    Json* contracts) {
  // Do not filter existing bindings here: the caller may be adding only the
  // new output to a plan that also owns historical calls.
  if (!items.IsArray() || (!contracts->IsNull() && !contracts->IsArray()))
    return Invalid();
  const auto declarations = Declarations(request);
  auto result = contracts->IsArray() ? *contracts : Array();
  std::unordered_map<std::string, const Json*> known;
  for (const auto& binding : contracts->array)
    if (!known.emplace(binding.GetString("item_id"), &binding).second)
      return Invalid();
  for (const auto& item : items.array) {
    if (!IsResultBoundToolCall(item)) continue;
    const auto id = item.GetString("id");
    if (!ValidId(id)) return Invalid();
    const auto bound = known.find(id);
    if (bound != known.end()) {
      const auto* schema = bound->second->Find("schema");
      if (!schema || !Same(*bound->second, Binding(item, *schema)))
        return Invalid();
      continue;
    }
    const auto declaration =
        declarations.find(Identity(item, item.GetString("namespace")));
    const auto* schema = declaration == declarations.end()
                             ? nullptr
                             : declaration->second.schema;
    result.array.push_back(Binding(item, schema ? *schema : Json()));
  }
  auto reply = Capacity(result);
  if (reply.ok()) *contracts = std::move(result);
  return reply;
}

ObjectReply CheckToolResultContracts(const Json& request, const Json& history,
                                     const Json& contracts,
                                     const StateAccess& access) {
  Json validated;
  auto reply = MergeToolResultContracts(contracts, history, &validated);
  if (!reply.ok()) return reply;
  std::unordered_map<std::string, const Json*> calls;
  for (const auto& binding : validated.array) {
    if (!calls.emplace(binding.GetString("call_id"), &binding).second)
      return Invalid();
  }
  auto checks = Array();
  for (const auto& [name, declaration] : Declarations(request)) {
    if (declaration.schema && !declaration.schema->IsNull())
      checks.array.push_back(Object({{"schema", *declaration.schema},
                                     {"param", String(declaration.param)}}));
  }
  for (size_t i = 0; i < history.array.size(); ++i) {
    const auto& item = history.array[i];
    const auto type = item.GetString("type");
    if (type != "function_call_output" && type != "custom_tool_call_output")
      continue;
    const auto call = calls.find(item.GetString("call_id"));
    if (call == calls.end()) return Invalid();
    const auto* schema = call->second->Find("schema");
    const auto* output = item.Find("output");
    // Standard content arrays are deliberately outside output_schema.
    if (schema && !schema->IsNull() && output && output->IsString())
      checks.array.push_back(Object(
          {{"schema", *schema},
           {"output", *output},
           {"param", String("input[" + std::to_string(i) + "].output")}}));
  }
  return ValidateToolResultSchemas(
      checks, {access.scope, access.deadline, access.cancelled});
}
}  // namespace q4t::server
