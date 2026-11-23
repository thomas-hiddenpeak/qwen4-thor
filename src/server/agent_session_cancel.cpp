#include "agent_session_internal.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/responses_jobs.h"

namespace q4t::server {
namespace {
using namespace object_detail;

ObjectReply Invalid() {
  return Error(503, "agent_session_record_invalid",
               "Invalid session cancellation state");
}

ObjectReply Signal(ObjectStore& objects, ResponsesJobs* jobs,
                   const std::string& session, const std::string& response,
                   const StateAccess& access) {
  if (response.empty()) return {};
  const auto row = ResponsesState(objects).ReadRecord(response, access);
  if (!row.ok()) return row;
  if (row.value.GetString("agent_session_id") != session) return Invalid();
  const auto* value = row.value.Find("response");
  if (!value || value->GetString("id") != response) return Invalid();
  const auto state = value->GetString("status");
  if (state == "queued" || state == "in_progress")
    return jobs ? jobs->Cancel(response, access)
                : Error(503, "background_unavailable",
                        "Session executor unavailable");
  if (state == "completed" || state == "failed" || state == "cancelled" ||
      state == "incomplete")
    return {};
  return Invalid();
}
ObjectReply Replay(ObjectStore& objects, ResponsesJobs* jobs,
                   const std::string& id, const Json& known,
                   const std::string& digest, const StateAccess& access) {
  if (!known.IsObject() || known.GetString("digest") != digest)
    return Error(409, "idempotency_key_conflict",
                 "Key was used for different events");
  const auto* target = known.Find("target");
  if (!target || !target->IsString()) return Invalid();
  return Signal(objects, jobs, id, target->str, access);
}
}  // namespace

namespace agent_detail {
Json SavedAgentToolResult(const std::string& response_id, const Json& event) {
  const auto call = event.GetString("call_id");
  auto item = Object({{"id", String("fco_" + Digest(response_id + "_" + call))},
                      {"type", String("function_call_output")},
                      {"call_id", String(call)},
                      {"status", String("completed")}});
  for (const auto* k : {"error", "output"}) {
    if (const auto* v = event.Find(k)) Set(&item, k, *v);
  }
  return item;
}
}  // namespace agent_detail

ObjectReply AppendCancelledAgentHistory(const CancelledAgentHistory& prior,
                                        const Json& record, Json* history) {
  const auto* response = record.Find("response");
  const auto* output = response ? response->GetArray("output") : nullptr;
  if (!output || prior.session_id.empty() ||
      record.GetString("agent_session_id") != prior.session_id ||
      (!prior.results.IsNull() && !prior.results.IsArray()))
    return Invalid();
  // Leave the original response intact. Only carry already published history
  // and calls with real client results into the next model turn.
  auto results = Array();
  for (const auto& item : output->array) {
    if (item.GetString("status") != "completed") continue;
    if (item.GetString("type") == "function_call") {
      const auto call = item.GetString("call_id");
      const auto match =
          std::ranges::find_if(prior.results.array, [&](const auto& event) {
            return event.GetString("call_id") == call;
          });
      if (match == prior.results.array.end()) continue;
      auto result =
          agent_detail::SavedAgentToolResult(response->GetString("id"), *match);
      if (!match->GetBool("success")) {
        const auto* error = match->Find("error");
        if (!error || !error->IsString()) return Invalid();
        auto failure = Object({{"error", *error}});
        if (const auto* value = match->Find("output"))
          Set(&failure, "output", *value);
        Set(&result, "output", String(OutputJson(failure)));
      }
      std::erase_if(result.object,
                    [](const auto& field) { return field.first == "error"; });
      results.array.push_back(std::move(result));
    }
    history->array.push_back(item);
  }
  if (results.array.size() != prior.results.array.size()) return Invalid();
  history->array.insert(history->array.end(), results.array.begin(),
                        results.array.end());
  return {};
}

ObjectReply CancelAgentSessionTurn(ObjectStore& objects, ResponsesJobs* jobs,
                                   const std::string& id, const Json& request,
                                   std::string_view key,
                                   const StateAccess& access) {
  const auto* events = request.GetArray("events");
  if (request.object.size() != 1 || !events || events->array.empty() ||
      events->array.size() > 256)
    return Error(400, "invalid_request",
                 "Expected between 1 and 256 input events");
  for (const auto& event : events->array) {
    ChatPlanError error;
    const auto valid =
        ValidateAgentSession(event, AgentSessionShape::kInputEvent, &error);
    if (!valid) {
      auto result = Error(error.http_status, error.code, valid.message());
      result.value = Object({{"param", String(error.param)}});
      return result;
    }
    if (event.GetString("type") != "agent.session.input.cancel")
      return Error(400, "invalid_request",
                   "Submit cancellation separately from new input");
  }
  const auto name = key.empty() ? "" : Digest(key);
  std::string digest;
  if (!name.empty() && !InputValueIdentity(request, &digest))
    return Error(413, "agent_event_limit",
                 "Cancellation exceeds the identity budget");
  auto row = agent_detail::ReadRow(objects, id, access);
  if (!row.ok()) return row;
  auto* data = row.value.Find("data");
  if (!data) return Invalid();
  auto modified = *data;
  const auto* receipts = modified.Find("event_receipts");
  if (receipts && !receipts->IsObject()) return Invalid();
  if (!name.empty() && receipts && receipts->Has(name))
    return Replay(objects, jobs, id, *receipts->Find(name), digest, access);
  AgentSessionView view;
  auto projected = agent_detail::ProjectHistory(objects, modified, id, "",
                                                access, &view, false);
  if (!projected.ok()) return projected;
  const auto state = view.turn.GetString("status");
  auto& links = Field(modified, "turns");
  std::string target;
  if (state == "waiting" || state == "in_progress" || state == "queued") {
    if (links.array.empty()) return Invalid();
    auto& link = links.array.back();
    target = link.GetString("response_id");
    if (!link.Has("cancel_requested_at"))
      Set(&link, "cancel_requested_at", Number(std::time(nullptr)));
    if (const auto* saved = modified.Find("pending_tool_results")) {
      Set(&link, "saved_tool_results", *saved);
      std::erase_if(modified.object, [](const auto& entry) {
        return entry.first == "pending_tool_results";
      });
    }
  } else if (name.empty()) {
    return {};
  }
  // Commit the exact response before signalling. A retry after interruption
  // can finish the same cancellation even if a different turn is now active.
  if (!name.empty()) {
    auto& keys = Field(modified, "event_receipts");
    if (keys.IsNull()) keys = Object();
    Set(&keys, name,
        Object({{"digest", String(digest)}, {"target", String(target)}}));
  }
  ObjectRequest write;
  write.kind = ObjectKind::kAgentSession;
  write.operation = ObjectOperation::kPut;
  write.scope = access.scope;
  write.id = id;
  write.ttl_seconds = 0;
  write.expected_revision = row.value.GetInt("revision");
  write.data = std::move(modified);
  auto saved = objects.Execute(write, access.deadline, access.cancelled);
  if (!saved.ok()) {
    if (!name.empty() &&
        (saved.commit_unknown || saved.code == "object_revision_conflict")) {
      auto latest = agent_detail::ReadRow(objects, id, access);
      const auto* state = latest.ok() ? latest.value.Find("data") : nullptr;
      const auto* keys = state ? state->Find("event_receipts") : nullptr;
      if (keys && keys->Has(name))
        return Replay(objects, jobs, id, *keys->Find(name), digest, access);
    }
    if (saved.commit_unknown) saved.value = Object({{"id", String(id)}});
    return saved;
  }
  return Signal(objects, jobs, id, target, access);
}
}  // namespace q4t::server
