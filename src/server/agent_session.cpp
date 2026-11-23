#include "q4t/server/agent_session.h"

#include "object_protocol.h"
#include "agent_session_internal.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;

ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
}  // namespace

ObjectReply PrepareDirectAgentSession(ObjectStore& objects, const Json& request,
                                      const ChatPlanConfig& config,
                                      const StateAccess& access,
                                      std::string_view session_id,
                                      int64_t created,
                                      AgentSessionStart* start) {
  ChatPlanError error;
  auto valid =
      ValidateAgentSession(request, AgentSessionShape::kCreate, &error);
  if (!valid) {
    auto reply = Error(error.http_status, error.code, valid.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  const auto* environment = request.Find("environment");
  if (environment->GetString("type") != "none")
    return Error(500, "agent_environment_mismatch",
                 "Incorrect session executor");
  const auto* input = request.Find("input");
  if (!input || (input->IsString() && input->str.empty()) ||
      (!input->IsString() && (!input->IsArray() || input->array.empty())))
    return Invalid("A session without an environment needs initial input",
                   "input");

  Json private_http;
  auto agent = ResolveSessionAgent(objects, request, config, access, &private_http);
  if (!agent.ok()) return agent;
  const auto* metadata = request.Find("metadata");
  auto normalized =
      NormalizeConversationMetadata(metadata ? *metadata : Json{});
  if (!normalized.ok()) return normalized;
  Json session =
      Object({{"id", String(std::string(session_id))},
              {"object", String("agent.session")},
              {"agent", std::move(agent.value)},
              {"environment", *environment},
              {"created_at", Number(created)},
              {"last_active_at", Number(created)},
              {"error", {}},
              {"usage", {}},
              {"metadata", std::move(normalized.value)},
              {"required_actions", Array()},
              {"status", String("in_progress")},
              {"vault_ids", Array()},
              {"review", String(request.GetString("review", "none"))}});
  if (const auto* vaults = request.GetArray("vault_ids")) {
    ObjectRequest lookup;
    lookup.kind = ObjectKind::kVault;
    lookup.scope = access.scope;
    for (const auto& id : vaults->array) {
      if (!id.str.starts_with("vault_") || !ValidId(id.str))
        return Error(404, "object_not_found", "Vault not found");
      lookup.id = id.str;
      auto found = objects.Execute(lookup, access.deadline, access.cancelled);
      if (!found.ok()) return found;
      const auto* data = found.value.Find("data");
      if (!data || data->GetString("id") != id.str ||
          data->GetString("object") != "vault")
        return Error(503, "vault_record_invalid", "Invalid vault record");
      if (data->GetBool("archived"))
        return Error(404, "object_not_found", "Vault not found");
    }
    Set(&session, "vault_ids", *vaults);
  }
  if (const auto* spending = request.Find("spend_control");
      spending && Nonnull(*spending, "limit")) {
    const auto* limit = spending->Find("limit");
    if (!limit || !IsInputInteger(*limit) || limit->number <= 0)
      return Invalid("Expected a positive limit in cents",
                     "spend_control.limit");
    Set(&session, "spend_control",
        Object({{"limit", *limit}, {"consumed", {}}}));
  }

  valid = ValidateAgentSession(session, AgentSessionShape::kResource, &error);
  if (!valid)
    return Error(500, "invalid_agent_session",
                 "Invalid public session resource");
  auto messages =
      input->IsString()
          ? Array({Object(
                {{"role", String("user")},
                 {"type", String("message")},
                 {"content", Array({Object({{"type", String("input_text")},
                                            {"text", *input}})})}})})
          : *input;
  for (auto& message : messages.array) {
    if (message.GetArray("content")->array.empty())
      return Invalid("Expected message content", "input");
    Set(&message, "type", String("message"));
  }
  std::string sealed;
  auto secured = agent_detail::SealAgentHttp(objects, access, session_id, private_http, &sealed);
  if (!secured.ok()) return secured;
  *start = {std::move(session), std::move(messages), std::move(sealed)};
  return {};
}
}  // namespace q4t::server
