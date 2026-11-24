#include "q4t/server/chatkit.h"

#include <ctime>

#include "chatkit_credentials.h"
#include "object_protocol.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using chatkit_detail::RestoreSecret;

ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}

const Json& Child(const Json& parent, const char* name) {
  static const Json empty;
  const auto* child = parent.Find(name);
  return child ? *child : empty;
}

Json Features(const Json& requested) {
  const auto& title = Child(requested, "automatic_thread_titling");
  const auto& files = Child(requested, "file_upload");
  const auto& history = Child(requested, "history");
  const bool upload = files.GetBool("enabled", false);
  return Object(
      {{"automatic_thread_titling",
        Object({{"enabled", Boolean(title.GetBool("enabled", true))}})},
       {"file_upload",
        Object({{"enabled", Boolean(upload)},
                {"max_file_size",
                 upload ? Number(files.GetInt("max_file_size", 512)) : Json{}},
                {"max_files",
                 upload ? Number(files.GetInt("max_files", 10)) : Json{}}})},
       {"history",
        Object({{"enabled", Boolean(history.GetBool("enabled", true))},
                {"recent_threads", Child(history, "recent_threads")}})}});
}

ObjectReply PublicSession(Json resource, std::string secret) {
  Set(&resource, "client_secret", String(std::move(secret)));
  ChatPlanError error;
  if (!ValidateChatKit(resource, "session", &error))
    return Error(503, "chatkit_record_invalid",
                 "Invalid stored ChatKit session");
  ObjectReply result;
  result.value = std::move(resource);
  return result;
}

ObjectReply Create(ObjectStore& objects, const StateAccess& access,
                   const std::string& body, ChatKitIssuer issuer) {
  Json input;
  auto parsed = io::ParseJson(body, &input, true, kApplicationJsonLimits);
  if (!parsed) return Invalid(parsed.message(), "body");
  ChatPlanError error;
  auto checked = ValidateChatKit(input, "create", &error);
  if (!checked) return Invalid(checked.message(), error.param);
  const auto& workflow = Child(input, "workflow");
  ObjectRequest agent;
  agent.kind = ObjectKind::kAgent;
  agent.id = workflow.GetString("id");
  if (!agent.id.starts_with("agent_") || !ValidApplicationId(agent.id))
    return Error(404, "workflow_not_found", "Workflow not found");
  agent.scope = access.scope;
  auto deployed = objects.Execute(agent, access.deadline, access.cancelled);
  if (!deployed.ok()) return deployed;
  const auto revision = deployed.value.GetInt("revision");
  if (workflow.Has("version") &&
      workflow.GetString("version") != std::to_string(revision))
    return Error(404, "workflow_version_not_found",
                 "Workflow version not found");
  const auto& definition = Child(deployed.value, "data");
  if (revision < 1 || definition.GetString("id") != agent.id)
    return Error(503, "workflow_record_invalid", "Invalid stored workflow");

  ObjectRequest session;
  session.operation = ObjectOperation::kPut;
  session.kind = ObjectKind::kChatKitSession;
  session.scope = access.scope;
  const auto now = std::time(nullptr);
  const auto expires =
      now + Child(input, "expires_after").GetInt("seconds", 600);
  const auto rpm =
      Child(input, "rate_limits").GetInt("max_requests_per_1_minute", 10);
  std::string token, sealed;
  auto issued = chatkit_detail::IssueSecret(objects, access, issuer, expires,
                                            &session.id, &token, &sealed);
  if (!issued.ok()) return issued;
  const auto metadata =
      Object({{"id", String(agent.id)},
              {"version", Child(workflow, "version")},
              {"state_variables", workflow.Has("state_variables")
                                      ? Child(workflow, "state_variables")
                                      : Object()},
              {"tracing",
               Object({{"enabled", Boolean(Child(workflow, "tracing")
                                               .GetBool("enabled", true))}})}});
  auto resource = Object(
      {{"object", String("chatkit.session")},
       {"id", String(session.id)},
       {"status", String("active")},
       {"user", Child(input, "user")},
       {"expires_at", Number(expires)},
       {"workflow", metadata},
       {"rate_limits", Object({{"max_requests_per_1_minute", Number(rpm)}})},
       {"max_requests_per_1_minute", Number(rpm)},
       {"chatkit_configuration",
        Features(Child(input, "chatkit_configuration"))}});
  StateCipher cipher(objects);
  // Pin the deployed version, including private tool settings. Only
  // authenticated state can recover it. The public workflow carries
  // client-visible fields.
  std::string private_definition = OutputJson(definition), snapshot;
  auto saved = cipher.Seal(private_definition,
                           {"q4t.chatkit", "workflow", "snapshot", session.id},
                           access, &snapshot);
  CryptoWipe(&private_definition);
  if (!saved.ok()) {
    CryptoWipe(&token);
    return saved;
  }
  session.data = Object({{"session", resource},
                         {"sealed_token", String(sealed)},
                         {"token_sha256", String(Digest(token))},
                         {"workflow_snapshot", String(std::move(snapshot))},
                         {"workflow_revision", Number(revision)}});
  auto credentials = Array();
  if (const auto* tools = definition.GetArray("tools")) {
    for (const auto& tool : tools->array) {
      if (const auto* id = tool.Find("credential_id");
          tool.GetString("type") == "mcp" && id && id->IsString())
        credentials.array.push_back(*id);
    }
  }
  Set(&session.data, "credential_ids", std::move(credentials));
  // Keep cancellation metadata available after grant expiry. Metadata continues
  // to count against the same durable object quotas as other API resources.
  session.expires_at = expires + 86400;
  auto reply = PublicSession(std::move(resource), std::move(token));
  if (!reply.ok()) return reply;
  auto committed = objects.Execute(session, access.deadline, access.cancelled);
  return committed.ok() ? reply : committed;
}
}  // namespace

ObjectReply HandleChatKitSession(ObjectStore& objects,
                                 const StateAccess& access,
                                 std::string_view target,
                                 const std::string& body,
                                 ChatKitIssuer issuer) {
  const auto route = ParseChatKitRoute(target);
  if (target.find('?') != target.npos)
    return Invalid("Unexpected query", "query");
  if (route.kind == ChatKitRouteKind::kSessions)
    return Create(objects, access, body, issuer);
  if (route.kind != ChatKitRouteKind::kCancel)
    return Error(404, "route_not_found", "ChatKit session route not found");
  if (!body.empty()) {
    Json empty;
    if (!io::ParseJson(body, &empty, true, kApplicationJsonLimits) ||
        !empty.IsObject() || !empty.object.empty())
      return Invalid("Cancellation takes no parameters", "body");
  }
  ObjectRequest request;
  request.scope = access.scope;
  request.id = std::string(route.id);
  request.kind = ObjectKind::kChatKitSession;
  for (int attempt = 0; attempt < 3; ++attempt) {
    request.operation = ObjectOperation::kGet;
    auto prior = objects.Execute(request, access.deadline, access.cancelled);
    if (!prior.ok()) return prior;
    request.data = Child(prior.value, "data");
    auto& session = Field(request.data, "session");
    const auto status = session.GetString("status");
    if (session.GetString("id") != request.id ||
        (status != "active" && status != "cancelled" && status != "expired"))
      return Error(503, "chatkit_record_invalid",
                   "Invalid stored ChatKit session");
    StateCipher cipher(objects);
    std::string token;
    auto opened = RestoreSecret(cipher, access, request.id,
                                request.data.GetString("sealed_token"), &token);
    if (!opened.ok()) return opened;
    if (status != "active") return PublicSession(session, std::move(token));
    Set(&session, "status",
        String(std::time(nullptr) >= session.GetInt("expires_at")
                   ? "expired"
                   : "cancelled"));
    auto projected = PublicSession(session, std::move(token));
    if (!projected.ok()) return projected;
    request.expected_revision = prior.value.GetInt("revision");
    request.expires_at = prior.value.GetInt("expires_at");
    request.operation = ObjectOperation::kPut;
    auto committed =
        objects.Execute(request, access.deadline, access.cancelled);
    if (committed.ok()) return projected;
    if (committed.code != "object_revision_conflict") return committed;
  }
  return Error(409, "chatkit_session_busy",
               "Session changed during cancellation");
}
}  // namespace q4t::server
