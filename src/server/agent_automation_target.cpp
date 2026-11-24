#include "agent_automation_internal.h"

#include <algorithm>

#include "agent_environment_internal.h"
#include "object_protocol.h"
#include "q4t/server/agent_session.h"
#include "q4t/server/agent_vault.h"
#include "q4t/server/conversation_state.h"

namespace q4t::server::automation_detail {
namespace {
using namespace object_detail;
Json Or(const Json& object, const char* key, Json fallback = {}) {
  const auto* value = object.Find(key);
  return value && !value->IsNull() ? *value : std::move(fallback);
}

ObjectReply Invalid(std::string message, std::string param) {
  auto out = Error(400, "invalid_request", std::move(message));
  out.value = Object({{"param", String(std::move(param))}});
  return out;
}

ObjectReply Lookup(ObjectStore& objects, const StateAccess& access,
                   ObjectKind kind, const std::string& id) {
  if (!ValidId(id)) return Error(404, "object_not_found", "Resource not found");
  ObjectRequest request;
  request.id = id;
  request.scope = access.scope;
  request.kind = kind;
  return objects.Execute(request, access.deadline, access.cancelled);
}

ObjectReply Environment(ObjectStore& objects, const StateAccess& access,
                        const Json& input) {
  auto result = ObjectReply{};
  const auto type = input.GetString("type");
  result.value = Object({{"type", String(type)}});
  if (type == "none") return result;
  Set(&result.value, "environment_id", Or(input, "environment_id"));
  if (type == "self_hosted") {
    Set(&result.value, "workspace_directory", Or(input, "workspace_directory"));
    Set(&result.value, "capability_directories",
        Or(input, "capability_directories", Array()));
    return result;
  }
  const auto* id = input.Find("environment_id");
  Json projected;
  if (id && !id->IsNull()) {
    if (input.object.size() != 2)
      return Invalid("An existing environment cannot include overrides",
                     "target.environment");
    auto row = Lookup(objects, access, ObjectKind::kEnvironment, id->str);
    if (!row.ok()) return row;
  } else {
    Json settings = input;
    std::erase_if(settings.object, [](const auto& field) {
      return field.first == "container_size";
    });
    Json definition;
    auto resolved = environment_detail::ResolveDefinition(
        objects, access, settings, &definition);
    if (!resolved.ok()) return resolved;
    // Reuse the template projection to omit archive bytes, file contents,
    // secret environment variables and setup commands. Inherited overrides
    // remain null on the automation's public selection.
    auto safe = environment_detail::PublicTemplate(
        definition, "environment_template_projection", 0, 0);
    if (!safe.ok()) return safe;
    projected = std::move(safe.value);
  }
  Set(&result.value, "environment_template_id",
      Or(input, "environment_template_id"));
  Set(&result.value, "container_size", Or(input, "container_size"));
  for (const char* key :
       {"packages", "network", "desktop", "capability_directories", "skills",
        "plugins", "files"})
    Set(&result.value, key, input.Has(key) ? Or(projected, key) : Json{});
  return result;
}
}  // namespace

ObjectReply ProjectTarget(ObjectStore& objects, const StateAccess& access,
                          const ChatPlanConfig& config, const Json& target) {
  const auto type = target.GetString("type");
  if (type == "existing_session") {
    auto row = Lookup(objects, access, ObjectKind::kAgentSession,
                      target.GetString("session_id"));
    if (row.ok()) row.value = target;
    return row;
  }
  if (target.Has("agent_id") == target.Has("agent"))
    return Invalid("Provide exactly one of agent_id and agent", "target");
  auto agent = ResolveSessionAgent(objects, target, config, access);
  if (!agent.ok()) return agent;
  Json inline_agent;
  if (!target.Has("agent_id")) {
    inline_agent = Object();
    for (const char* key :
         {"model", "reasoning", "text", "service_tier", "instructions", "tools",
          "multi_agent", "compaction", "experimental"})
      if (const auto* value = agent.value.Find(key))
        Set(&inline_agent, key, *value);
  }
  auto env = Environment(objects, access, *target.Find("environment"));
  if (!env.ok()) return env;
  auto vaults = Or(target, "vault_ids", Array());
  auto available = CheckAgentVaults(objects, access, vaults);
  if (!available.ok()) return available;
  auto metadata = NormalizeConversationMetadata(Or(target, "metadata"));
  if (!metadata.ok()) return metadata;
  auto messages = Or(target, "input", Array());
  for (auto& message : messages.array) Set(&message, "type", String("message"));
  ObjectReply result;
  result.value = Object({{"type", String(type)},
                         {"agent_id", Or(target, "agent_id")},
                         {"agent", std::move(inline_agent)},
                         {"environment", std::move(env.value)},
                         {"input", std::move(messages)},
                         {"vault_ids", std::move(vaults)},
                         {"metadata", std::move(metadata.value)}});
  return result;
}
}  // namespace q4t::server::automation_detail
