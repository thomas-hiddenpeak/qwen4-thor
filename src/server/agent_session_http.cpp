#include "q4t/server/agent_session.h"

#include <algorithm>
#include <ctime>
#include <thread>

#include "q4t/server/lifetime.h"

#include "agent_artifact_internal.h"
#include "agent_environment_bindings.h"
#include "agent_subagent_internal.h"
#include "agent_environment_journal.h"
#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_jobs.h"
#include "q4t/server/responses_output.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Kind = AgentSessionRouteKind;

ObjectReply Invalid(std::string message, std::string param) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}

ObjectReply DeleteSession(ObjectStore& objects, const std::string& id,
                          const StateAccess& access) {
  ObjectRequest session;
  session.scope = access.scope;
  session.id = id;
  session.kind = ObjectKind::kAgentSession;
  auto row = objects.Execute(session, access.deadline, access.cancelled);
  if (!row.ok()) return row;
  const auto* data = row.value.Find("data");
  const auto* turns = data ? data->GetArray("turns") : nullptr;
  if (!turns || (turns->array.empty() && !data->GetBool("created_idle")))
    return Error(503, "agent_session_record_invalid", "Invalid session turns");
  session.operation = ObjectOperation::kErase;
  session.expected_revision = row.value.GetInt("revision");
  const auto links = *turns;
  std::vector<ObjectRequest> mutations{session};
  const auto* stored_session = data->Find("session");
  const auto* env = stored_session ? stored_session->Find("environment") : nullptr;
  if (!env) return Error(503, "agent_session_record_invalid", "Missing environment");
  if (env->GetString("type") == "openai_hosted") {
    ObjectRequest release;
    release.kind = ObjectKind::kEnvironment;
    release.scope = access.scope;
    release.id = env->GetString("id");
    auto workspace = objects.Execute(release, access.deadline, access.cancelled);
    if (!workspace.ok()) return workspace;
    release.data = std::move(Field(workspace.value, "data"));
    if (!environment_detail::HasAttachedSession(release.data, id))
      return Error(503, "environment_record_invalid", "Invalid workspace owner");
    release.operation = ObjectOperation::kPut;
    release.expected_revision = workspace.value.GetInt("revision");
    if (release.data.Has("attached_sessions")) {
      auto& peers = Field(release.data, "attached_sessions").array;
      std::erase_if(peers, [&](const Json& peer) { return peer.str == id; });
      auto& resource = Field(release.data, "resource");
      const auto status = resource.GetString("status");
      const bool last_session = peers.empty();
      if (status == "connected") {
        if (last_session) {
          Set(&resource, "status", String("disconnected"));
          environment_detail::AppendEnvironmentStatus(&release.data,
                                                      "disconnected");
        }
        Set(&release.data, "last_activity_at", Number(std::time(nullptr)));
      }
      release.ttl_seconds = last_session && (status == "expired" || status == "failed" ||
                                             status == "pending") ? 86400 : 0;
    } else {
      release.ttl_seconds = 86400;
      Set(&Field(release.data, "resource"), "status", String("disconnected"));
      // Keep a status receipt after deletion and discard private workspace
      // bytes.
      std::erase_if(release.data.object, [](const auto& field) {
        return field.first == "sealed_definition" ||
               field.first == "bound_session" ||
               field.first == "input_snapshots";
      });
    }
    mutations.push_back(std::move(release));
  }
  for (const auto& turn : links.array) {
    ObjectRequest response;
    response.kind = ObjectKind::kResponse;
    response.scope = access.scope;
    response.id = turn.GetString("response_id");
    row = objects.Execute(response, access.deadline, access.cancelled);
    if (!row.ok()) return row;
    const auto* record = row.value.Find("data");
    const auto* value = record ? record->Find("response") : nullptr;
    if (!value || record->GetString("agent_session_id") != id ||
        value->GetString("id") != response.id)
      return Error(503, "agent_session_record_invalid",
                   "Invalid session owner");
    auto status = value->GetString("status");
    if (status != "completed" && status != "incomplete" && status != "failed" &&
        status != "cancelled")
      return Error(409, "agent_session_active",
                   "Cancel the active turn before deletion");
    response.operation = ObjectOperation::kErase;
    response.expected_revision = row.value.GetInt("revision");
    mutations.push_back(std::move(response));
  }
  auto deleted =
      objects.ExecuteBatch(mutations, access.deadline, access.cancelled);
  if (deleted.ok())
    deleted.value = Object({{"id", String(id)},
                            {"object", String("agent.session.deleted")},
                            {"deleted", Boolean(true)}});
  else if (deleted.commit_unknown)
    deleted.value = Object({{"id", String(id)}});
  return deleted;
}

struct Subscriber {
  std::string id;
  int64_t sequence = 0;
  static std::string ErrorEvent(void* context, int status,
                                const std::string& message,
                                std::string_view code, std::string_view param) {
    const auto& id = static_cast<Subscriber*>(context)->id;
    const auto error = Object(
        {{"type",
          String(status < 500 ? "invalid_request_error" : "server_error")},
         {"code", String(std::string(code))},
         {"message", String(message)},
         {"param", param.empty() ? Json{} : String(std::string(param))}});
    auto event = Object({{"event_id", String(detail::NewRequestId("evt_"))},
                         {"session_id", String(id)},
                         {"type", String("error")},
                         {"error", error}});
    return "event: error\r\ndata: " + OutputJson(event) + "\r\n\r\n";
  }
};

void Stream(int fd, ObjectStore& objects, const std::string& id, bool follow,
            bool obfuscation, const StateAccess& access) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http) return;
  Subscriber subscriber{id};
  ScopeExit clear([&] { http->SetResponsesStream(nullptr, nullptr, nullptr); });
  http->SetResponsesStream(&Subscriber::ErrorEvent, &subscriber,
                           &subscriber.sequence);
  AgentEventCursor cursor;
  using Clock = std::chrono::steady_clock;
  auto heartbeat = Clock::now() + std::chrono::seconds(5);
  const auto status = follow ? detail::StreamStatus::kOk
                             : detail::StreamStatus::kCreated;
  bool started = false;
  try {
    if (follow) {
      auto installed = OpenAgentEventSubscription(objects, id, access, &cursor);
      if (!installed.ok()) {
        detail::SendError(fd, installed.http_status, installed.ClientMessage(),
                          {}, installed.ClientCode());
        return;
      }
      if (!detail::BeginStreamingResponse(fd, http->State(), {access.deadline}, status))
        return;
      started = true;
    }
    while (!access.cancelled || !access.cancelled()) {
      if (Clock::now() >= access.deadline) {
        detail::SendError(fd, 408, "Session subscription expired", {},
                          "request_timeout");
        return;
      }
      Json events;
      bool settled = false;
      auto replay = ReplayAgentSessionEvents(objects, id, access, &cursor,
                                             &events, &settled);
      if (!replay.ok()) {
        detail::SendError(fd, replay.http_status, replay.ClientMessage(), {},
                          replay.ClientCode());
        return;
      }
      if (!started &&
          !detail::BeginStreamingResponse(fd, http->State(), {access.deadline}, status))
        return;
      started = true;
      for (auto& event : events.array) {
        ApplyDeltaObfuscation(&event, obfuscation);
        const auto wire = "id: " + event.GetString("event_id") +
                          "\r\nevent: " + event.GetString("type") +
                          "\r\ndata: " + OutputJson(event) + "\r\n\r\n";
        if (!detail::WriteAll(fd, wire, {access.deadline})) return;
        ++subscriber.sequence;
      }
      if (settled && !follow) {
        http->BeforeTerminal();
        *http->State() = detail::ResponseState::kComplete;
        return;
      }
      if (!events.array.empty()) {
        heartbeat = Clock::now() + std::chrono::seconds(5);
        continue;
      }
      if (Clock::now() >= heartbeat) {
        if (!detail::WriteAll(fd, ": keepalive\r\n\r\n", {access.deadline}))
          return;
        heartbeat = Clock::now() + std::chrono::seconds(5);
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  } catch (const std::bad_alloc&) {
    detail::SendExceptionResponse(fd, *http->State(),
                                  detail::ExceptionKind::kHostMemory);
  } catch (...) {
    detail::SendExceptionResponse(fd, *http->State());
  }
}

ObjectReply Handle(int fd, ObjectStore& objects, ClientAdmission& clients,
                   ResponsesJobs* jobs, std::string_view method,
                   std::string_view target, const std::string& body,
                   const ChatPlanConfig& config, const StateAccess& access,
                   bool* stream, bool* include_obfuscation, bool* sent) {
  const auto route = ParseAgentSessionRoute(target);
  if (route.kind == Kind::kNone)
    return Error(404, "route_not_found", "Session route not found");
  if (!ManagedAgentMethodAllowed(method, target)) {
    auto denied = Error(405, "method_not_allowed", "Method not allowed");
    const auto allowed = route.kind == Kind::kEvents       ? "GET, POST"
                         : route.kind == Kind::kCollection ? "GET, POST"
                         : route.kind == Kind::kArtifact   ? "GET, DELETE"
                         : route.kind == Kind::kSession    ? "GET, POST, DELETE"
                                                           : "GET";
    denied.value = Object({{"allow", String(allowed)}});
    return denied;
  }
  const auto split = target.find('?');
  if (route.kind == Kind::kArtifacts || route.kind == Kind::kArtifact ||
      route.kind == Kind::kArtifactContent) {
    if (!body.empty()) return Invalid("Unexpected body", "body");
    if (split != target.npos && route.kind != Kind::kArtifacts)
      return Invalid("Unexpected query", "query");
    auto query = ParseApplicationQuery(
        split == target.npos ? "" : target.substr(split + 1));
    if (!query.ok()) return query;
    return agent_detail::HandleArtifacts(objects, access, route, method,
                                         query.value, fd, sent);
  }
  if (route.kind == Kind::kCollection && method == "GET") {
    if (!body.empty()) return Invalid("Unexpected body", "body");
    auto query = ParseApplicationQuery(
        split == target.npos ? "" : target.substr(split + 1));
    return query.ok() ? ListAgentSessions(objects, query.value, access) : query;
  }
  if (route.kind == Kind::kEvents && method == "GET") {
    if (!body.empty()) return Invalid("Unexpected body", "body");
    auto query = ParseApplicationQuery(
        split == target.npos ? "" : target.substr(split + 1));
    if (!query.ok()) return query;
    for (const auto& field : query.value.object)
      if (field.first != "include_obfuscation")
        return Invalid("Unexpected event subscription field", field.first);
    *stream = true;
    ObjectReply result;
    result.value = Object({{"id", String(std::string(route.session))}});
    *include_obfuscation = query.value.GetBool("include_obfuscation", true);
    return result;
  }
  if (split != target.npos &&
      (route.kind == Kind::kCollection || route.kind == Kind::kEvents ||
       route.kind == Kind::kSession || route.kind == Kind::kTurn ||
       route.kind == Kind::kSubagent || route.kind == Kind::kSubagentTurn))
    return Invalid("Unexpected query", "query");
  if (route.kind == Kind::kSession && method == "POST") {
    Json request;
    auto parsed = io::ParseJson(body, &request, true, kSchemaJsonLimits);
    if (!parsed) return Invalid(parsed.message(), "body");
    return UpdateAgentSession(objects, std::string(route.session), request,
                              config, access);
  }
  if (route.kind == Kind::kCollection || route.kind == Kind::kEvents) {
    auto* http = detail::HttpResponseContext::Current(fd);
    if (!http || !jobs || !http->Client().Allowed())
      return Error(503, "background_unavailable",
                   "Session executor unavailable");
    Json request;
    auto valid = io::ParseJson(body, &request, true,
                               route.kind == Kind::kCollection
                                   ? kApplicationJsonLimits : kSchemaJsonLimits);
    if (!valid) return Invalid(valid.message(), "body");
    if (route.kind == Kind::kEvents) {
      const auto* events = request.GetArray("events");
      if (events && std::ranges::any_of(events->array, [](const auto& event) {
            return event.GetString("type") == "agent.session.input.cancel";
          }))
        return CancelAgentSessionTurn(objects, jobs, std::string(route.session),
                                      request, http->AgentEventKey(), access);
    }
    *stream = route.kind == Kind::kCollection && request.GetBool("stream");
    if (const auto* options = request.Find("stream_options"); *stream && options)
      *include_obfuscation = options->GetBool("include_obfuscation", true);
    auto run = std::make_unique<ResponsesExecution>(objects, jobs, true);
    const bool event = route.kind == Kind::kEvents;
    bool replay = false;
    auto session =
        event ? PrepareAgentSessionEvents(objects, std::string(route.session),
                                          request, http->AgentEventKey(),
                                          config, access, *run, &replay)
              : PrepareAgentSessionExecution(objects, request, config, access,
                                             std::time(nullptr), *run);
    // Finishing a pending workspace can win the attachment CAS. A definite
    // conflict committed neither the new session nor its initial turn.
    const auto* binding = request.Find("environment");
    if (!event && binding && binding->Has("environment_id") &&
        !session.commit_unknown && session.code == "object_revision_conflict") {
      run = std::make_unique<ResponsesExecution>(objects, jobs, true);
      session = PrepareAgentSessionExecution(objects, request, config, access,
                                             std::time(nullptr), *run);
    }
    if (event && !session.ok())
      return ReconcileAgentEventReceipt(objects, std::string(route.session),
                                        request, http->AgentEventKey(), access,
                                        std::move(session));
    if (!session.ok() || replay || !run->Prepared())
      return session;
    if (!run->BindRateDomain(http->RateDomain()))
      return Error(503, "response_rate_domain_changed",
                   "Session accounting origin changed");
    // The events endpoint also accepts cancellation while this client's model
    // slot is occupied. Only an event that starts work acquires a data permit.
    // Acquire before Begin can publish the new step and idempotency receipt.
    ClientLease work;
    if (event && !work.Acquire(clients, http->Client(), ClientLane::kData))
      return Error(429, "client_capacity_exhausted",
                   "Client inference capacity exhausted");
    auto result = jobs->Submit(
        std::move(run), http->Client(),
        [&](ClientLease& lease) {
          return event ? work.TransferTo(lease)
                       : http->TransferClientLease(lease);
        },
        http->TakeBatchRate());
    if (event)
      result = ReconcileAgentEventReceipt(objects, std::string(route.session),
                                          request, http->AgentEventKey(),
                                          access, std::move(result));
    if (result.ok()) return session;
    const auto id =
        event ? std::string(route.session) : session.value.GetString("id");
    if (result.code == "background_admission_lost") {
      // Begin committed before the permit transfer failed. Return the durable
      // session if readable so a client can observe its actual terminal.
      AgentSessionView view;
      auto recovered = ReadAgentSessionTurn(objects, id, "", access, &view);
      if (recovered.ok()) {
        recovered.value = std::move(view.session);
        return recovered;
      }
      result.commit_unknown = true;
    }
    if (result.commit_unknown) result.value = Object({{"id", String(id)}});
    return result;
  }
  if (!body.empty()) return Invalid("Unexpected body", "body");
  const std::string id(route.session);
  if (method == "DELETE") return DeleteSession(objects, id, access);
  AgentSessionView view;
  const bool subagents = route.kind == Kind::kSubagents || !route.subagent.empty();
  if (route.kind != Kind::kTraces && !subagents) {
    auto result = ReadAgentSessionTurn(objects, id, route.turn, access, &view,
                                       route.kind == Kind::kItems);
    if (!result.ok()) return result;
    if (route.kind == Kind::kSession || route.kind == Kind::kTurn) {
      result.value = route.kind == Kind::kSession ? std::move(view.session)
                                                  : std::move(view.turn);
      return result;
    }
  }
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  if (query.value.Has("include"))
    return Invalid("Unexpected include", "include");
  ItemViewOptions options;
  ChatPlanError error;
  auto valid = ParseItemViewOptions(query.value, &options, &error, true);
  if (!valid) return Invalid(valid.message(), error.param);
  if (subagents) return agent_detail::ReadSessionSubagents(objects, route, options, access);
  if (route.kind == Kind::kTraces)
    return ListAgentSessionTraces(objects, id, options, access);
  return PageStoredItems(route.kind == Kind::kTurns ? view.turns : view.items,
                         options, StoredItemView::kAgentSession);
}
}  // namespace

void ServeAgentSession(int fd, ObjectStore& objects, ClientAdmission& clients,
                       ResponsesJobs* jobs, std::string_view method,
                       std::string_view target, const std::string& body,
                       const ChatPlanConfig& config,
                       const StateAccess& access) {
  bool stream = false;
  bool include_obfuscation = true;
  bool sent = false;
  auto reply = Handle(fd, objects, clients, jobs, method, target, body, config,
                      access, &stream, &include_obfuscation, &sent);
  if (sent) return;
  if (reply.ok() && stream) {
    Stream(fd, objects, reply.value.GetString("id"), method == "GET",
           include_obfuscation, access);
    return;
  }
  const auto kind = ParseAgentSessionRoute(target).kind;
  if (reply.ok() && kind == Kind::kEvents &&
      method == "POST") {
    detail::SendSimple(fd, 202, "Accepted", "", "application/json",
                       {access.deadline});
  } else if (reply.ok()) {
    const bool created = kind == Kind::kCollection && method == "POST";
    detail::SendSimple(fd, created ? 201 : 200, created ? "Created" : "OK",
                       OutputJson(reply.value),
                       "application/json", {access.deadline});
  } else {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(),
                      {access.deadline}, reply.ClientCode(),
                      reply.value.GetString("param"),
                      reply.value.GetString("allow"));
  }
}
}  // namespace q4t::server
