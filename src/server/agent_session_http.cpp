#include "q4t/server/agent_session.h"

#include <ctime>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_jobs.h"

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
  if (!turns || turns->array.empty())
    return Error(503, "agent_session_record_invalid", "Invalid session turns");
  session.operation = ObjectOperation::kErase;
  session.expected_revision = row.value.GetInt("revision");
  const auto links = *turns;
  std::vector<ObjectRequest> mutations{session};
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

ObjectReply Handle(int fd, ObjectStore& objects, ResponsesJobs* jobs,
                   std::string_view method, std::string_view target,
                   const std::string& body, const ChatPlanConfig& config,
                   const StateAccess& access) {
  const auto route = ParseAgentSessionRoute(target);
  if (route.kind == Kind::kNone)
    return Error(404, "route_not_found", "Session route not found");
  if (!ManagedAgentMethodAllowed(method, target)) {
    auto denied = Error(405, "method_not_allowed", "Method not allowed");
    denied.value =
        Object({{"allow", String(route.kind == Kind::kCollection ||
                                         route.kind == Kind::kEvents
                                     ? "POST"
                                 : route.kind == Kind::kSession ? "GET, DELETE"
                                                                : "GET")}});
    return denied;
  }
  const auto split = target.find('?');
  if (split != target.npos &&
      (route.kind == Kind::kCollection || route.kind == Kind::kEvents ||
       route.kind == Kind::kSession || route.kind == Kind::kTurn))
    return Invalid("Unexpected query", "query");
  if (route.kind == Kind::kCollection || route.kind == Kind::kEvents) {
    auto* http = detail::HttpResponseContext::Current(fd);
    if (!http || !jobs || !http->Client().Allowed())
      return Error(503, "background_unavailable",
                   "Session executor unavailable");
    Json request;
    auto valid = io::ParseJson(body, &request, true, kSchemaJsonLimits);
    if (!valid) return Invalid(valid.message(), "body");
    if (request.GetBool("stream"))
      return Error(503, "agent_session_stream_unavailable",
                   "Session stream unavailable");
    auto run = std::make_unique<ResponsesExecution>(objects, jobs, true);
    const bool event = route.kind == Kind::kEvents;
    bool replay = false;
    auto session =
        event ? PrepareAgentSessionEvents(objects, std::string(route.session),
                                          request, http->AgentEventKey(),
                                          config, access, *run, &replay)
              : PrepareAgentSessionExecution(objects, request, config, access,
                                             std::time(nullptr), *run);
    if (event && !session.ok())
      return ReconcileAgentEventReceipt(objects, std::string(route.session),
                                        request, http->AgentEventKey(), access,
                                        std::move(session));
    if (!session.ok() || (event && (replay || !run->Prepared())))
      return session;
    if (!run->BindRateDomain(http->RateDomain()))
      return Error(503, "response_rate_domain_changed",
                   "Session accounting origin changed");
    auto result = jobs->Submit(
        std::move(run), http->Client(),
        [&](ClientLease& lease) { return http->TransferClientLease(lease); },
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
  auto result = ReadAgentSessionTurn(objects, id, route.turn, access, &view,
                                     route.kind == Kind::kItems);
  if (!result.ok()) return result;
  if (route.kind == Kind::kSession || route.kind == Kind::kTurn) {
    result.value = route.kind == Kind::kSession ? std::move(view.session)
                                                : std::move(view.turn);
    return result;
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
  return PageStoredItems(route.kind == Kind::kTurns ? view.turns : view.items,
                         options, StoredItemView::kAgentSession);
}
}  // namespace

void ServeAgentSession(int fd, ObjectStore& objects, ResponsesJobs* jobs,
                       std::string_view method, std::string_view target,
                       const std::string& body, const ChatPlanConfig& config,
                       const StateAccess& access) {
  auto reply = Handle(fd, objects, jobs, method, target, body, config, access);
  if (reply.ok() && ParseAgentSessionRoute(target).kind == Kind::kEvents &&
      method == "POST") {
    detail::SendSimple(fd, 202, "Accepted", "", "application/json",
                       {access.deadline});
  } else if (reply.ok()) {
    detail::SendSimple(fd, 200, "OK", OutputJson(reply.value),
                       "application/json", {access.deadline});
  } else {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(),
                      {access.deadline}, reply.ClientCode(),
                      reply.value.GetString("param"),
                      reply.value.GetString("allow"));
  }
}
}  // namespace q4t::server
