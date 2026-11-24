#include "agent_automation_internal.h"

#include <algorithm>
#include <ctime>

#include "agent_definition_cipher.h"
#include "object_protocol.h"
#include "q4t/server/agent_session.h"
#include "q4t/server/responses_jobs.h"

namespace q4t::server::automation_detail {
namespace {
using namespace object_detail;

ObjectReply Corrupt() {
  return Error(503, "automation_record_invalid", "Invalid queued invocation");
}

Json MessageEvent(const std::string& instructions) {
  auto part =
      Object({{"type", String("input_text")}, {"text", String(instructions)}});
  auto message = Object({{"role", String("user")}, {"content", Array({part})}});
  return Object(
      {{"events",
        Array({Object({{"type", String("agent.session.input.message")},
                       {"input", Array({message})}})})}});
}

ObjectReply Execute(ObjectStore& objects, ClientAdmission& clients,
                    const ClientIdentity& client, ResponsesJobs& jobs,
                    const ChatPlanConfig& config, const StateAccess& access,
                    const Json& target, ObjectRequest delivery) {
  ClientLease permit;
  if (!permit.Acquire(clients, client, ClientLane::kData))
    return Error(429, "client_capacity_exhausted", "Client is at capacity");
  auto run = std::make_unique<ResponsesExecution>(objects, &jobs, true);
  ObjectReply result;
  if (target.GetString("type") == "new_session") {
    auto request = target;
    std::erase_if(request.object,
                  [](const auto& entry) { return entry.first == "type"; });
    result = PrepareAgentSessionExecution(objects, request, config, access,
                                          std::time(nullptr), *run,
                                          std::move(delivery));
  } else if (target.GetString("type") == "existing_session") {
    const auto session = target.GetString("session_id");
    bool replay = false;
    result = PrepareAgentSessionEvents(
        objects, session, MessageEvent(target.GetString("instructions")),
        delivery.id, config, access, *run, &replay);
    if (result.ok()) {
      Set(&Field(delivery.data, "resource"), "session_id", String(session));
      // A recovered receipt can close an old queued record without submitting
      // another generation. Normally the receipt commits atomically in Begin.
      if (replay)
        return objects.Execute(delivery, access.deadline, access.cancelled);
      result = run->BindAutomationDelivery(std::move(delivery));
    }
  } else {
    return Corrupt();
  }
  if (!result.ok() || !run->Prepared()) return result;
  return jobs.Submit(std::move(run), client, [&](ClientLease& next) {
    return permit.TransferTo(next);
  });
}

bool Retry(const ObjectReply& result) {
  if (result.http_status == 409)
    return result.code == "object_revision_conflict" ||
           result.code == "active_turn_not_steerable" ||
           result.code == "agent_action_required";
  if (result.code.ends_with("_invalid") ||
      result.code.ends_with("_executor_unavailable"))
    return false;
  return result.http_status == 408 || result.http_status == 429 ||
         result.http_status >= 500;
}
}  // namespace

ObjectReply Deliver(ObjectStore& objects, ClientAdmission& clients,
                    const ClientIdentity& client, ResponsesJobs& jobs,
                    ChatPlanConfig config, const StateAccess& access,
                    const Json& record, int64_t now) {
  const auto* data = record.Find("data");
  const auto* row = data ? data->Find("resource") : nullptr;
  if (!row || row->GetString("id") != record.GetString("id")) return Corrupt();
  if (row->GetString("status") != "queued" ||
      data->GetInt("next_attempt") > now)
    return {};
  if (!clients.OwnsIdentity(client)) return {};
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  if (scope != access.scope)
    return Error(403, "object_scope_mismatch", "Automation client changed");
  const auto* sealed = data->Find("sealed_target");
  if (!sealed) return Corrupt();
  ObjectRequest write;
  write.operation = ObjectOperation::kPut;
  write.scope = access.scope;
  write.kind = ObjectKind::kAutomationInvocation;
  write.id = record.GetString("id");
  write.expected_revision = record.GetInt("revision");
  write.ttl_seconds = 0;
  write.data = Object({{"resource", *row}});
  Set(&Field(write.data, "resource"), "status", String("completed"));
  Json target;
  auto result = agent_detail::OpenDefinition(
      objects, access, agent_detail::DefinitionKind::kAutomationTarget,
      row->GetString("automation_id"), *sealed, &target);
  if (result.ok()) {
    config.client_limits = client.Limits();
    result =
        Execute(objects, clients, client, jobs, config, access, target, write);
  }
  if (result.ok() || result.commit_unknown ||
      (access.cancelled && access.cancelled()))
    return result;
  if (Retry(result)) {
    write.data = *data;
    const auto previous = data->GetInt("retry_delay", 1);
    const auto delay = std::clamp<int64_t>(previous, 1, 30) * 2;
    Set(&write.data, "retry_delay", Number(delay));
    Set(&write.data, "next_attempt", Number(now + delay));
  } else {
    auto& failed = Field(write.data, "resource");
    Set(&failed, "status", String("failed"));
    Set(&failed, "error",
        Object({{"code", String(result.code)},
                {"message", String(result.message)}}));
  }
  // A successful or uncertain Begin already advanced the revision. Its
  // delivery and session cannot be replaced by this later failure receipt.
  return objects.Execute(write, access.deadline, access.cancelled);
}
}  // namespace q4t::server::automation_detail
