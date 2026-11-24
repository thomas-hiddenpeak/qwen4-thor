#include "q4t/server/agent_session.h"
#include "q4t/server/agent_vault.h"

#include "object_protocol.h"
#include "agent_session_internal.h"
#include "agent_environment_internal.h"
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
  const auto kind = environment->GetString("type");
  if (kind != "none" && kind != "openai_hosted")
    return Error(503, "agent_environment_executor_unavailable",
                 "Session requires a connected self-hosted executor");
  const auto* input = request.Find("input");
  const bool idle = !input || input->IsNull() ||
                    (input->IsString() && input->str.empty()) ||
                    (input->IsArray() && input->array.empty());
  if (idle && (kind == "none" || (request.GetBool("stream") &&
      kind != "self_hosted" && !environment->Has("environment_id"))))
    return Invalid("This session requires initial input", "input");

  Json private_http;
  bool tool_search = false;
  auto agent = ResolveSessionAgent(objects, request, config, access,
                                   &private_http, &tool_search);
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
              {"status", String(idle ? "idle" : "in_progress")},
              {"vault_ids", Array()},
              {"review", String(request.GetString("review", "none"))}});
  if (const auto* vaults = request.GetArray("vault_ids")) {
    auto checked = CheckAgentVaults(objects, access, *vaults);
    if (!checked.ok()) return checked;
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

  std::optional<ObjectRequest> attachment;
  std::shared_ptr<environment_detail::WorkspaceCopies> copies;
  if (kind == "openai_hosted") {
    auto attached = environment_detail::PrepareAttachment(
        objects, access, *environment, std::string(session_id),
        *session.Find("vault_ids"), &attachment.emplace(), &copies, idle);
    if (!attached.ok()) return attached;
    Set(&session, "environment", std::move(attached.value));
  }
  valid = ValidateAgentSession(session, AgentSessionShape::kResource, &error);
  if (!valid)
    return Error(500, "invalid_agent_session",
                 "Invalid public session resource");
  auto messages =
      idle ? Array() : input->IsString()
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
  *start = {std::move(session), std::move(messages), std::move(sealed),
            std::move(attachment), std::move(copies), tool_search};
  return {};
}
}  // namespace q4t::server
