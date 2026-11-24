#include "agent_automation_internal.h"

#include <algorithm>
#include <chrono>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/agent_cron.h"

namespace q4t::server::automation_detail {
namespace {
using namespace object_detail;

ObjectReply Corrupt() {
  return Error(503, "automation_record_invalid", "Invalid automation queue");
}

ObjectReply Get(ObjectStore& objects, const StateAccess& access,
                ObjectKind kind, std::string id) {
  ObjectRequest req;
  req.kind = kind;
  req.scope = access.scope;
  req.id = std::move(id);
  return objects.Execute(req, access.deadline, access.cancelled);
}
}  // namespace

ObjectReply StillQueued(ObjectStore& objects, const StateAccess& access,
                        const Json& data, const std::string& automation,
                        bool* queued) {
  *queued = false;
  const auto pending = data.GetString("pending_delivery");
  if (pending.empty()) return {};
  auto row = Get(objects, access, ObjectKind::kAutomationInvocation, pending);
  if (!row.ok()) return row;
  const auto* saved = row.value.Find("data");
  const auto* item = saved ? saved->Find("resource") : nullptr;
  if (!item || item->GetString("automation_id") != automation ||
      item->GetString("id") != pending)
    return Corrupt();
  *queued = item->GetString("status") == "queued";
  return {};
}

ObjectReply Queue(ObjectStore& objects, const StateAccess& access,
                  const Json& record, int64_t now, int64_t occurrence) {
  const auto id = record.GetString("id");
  const auto* data = record.Find("data");
  if (!data || !data->Find("sealed_target") ||
      !data->GetArray("target_references"))
    return Corrupt();
  auto automation = PublicAutomation(*data, id);
  if (!automation.ok()) return automation;
  const bool scheduled = occurrence > 0;
  bool pending = false;
  if (scheduled) {
    auto prior = StillQueued(objects, access, *data, id, &pending);
    if (!prior.ok()) return prior;
  }
  auto source = Object({{"type", String(scheduled ? "automation" : "manual")}});
  if (scheduled) Set(&source, "automation", automation.value);
  ObjectRequest invocation;
  invocation.operation = ObjectOperation::kPut;
  invocation.kind = ObjectKind::kAutomationInvocation;
  invocation.scope = access.scope;
  invocation.id = NewObjectId(invocation.kind);
  invocation.ttl_seconds = 0;
  if (invocation.id.empty())
    return Error(503, "automation_id_unavailable", "No invocation ID");
  auto resource =
      Object({{"object", String("agent.automation.invocation")},
              {"id", String(invocation.id)},
              {"automation_id", String(id)},
              {"created_at", Number(now)},
              {"source", std::move(source)},
              {"scheduled_at", scheduled ? Number(occurrence) : Json{}},
              {"status", String(pending ? "skipped" : "queued")},
              {"skip_reason", pending ? String("pending_delivery") : Json{}},
              {"session_id", {}},
              {"error", {}}});
  ChatPlanError error;
  if (!ValidateAutomation(resource, AutomationShape::kInvocation, &error))
    return Corrupt();
  invocation.data = Object({{"resource", resource}});
  if (!pending) {
    Set(&invocation.data, "sealed_target", *data->Find("sealed_target"));
    Set(&invocation.data, "target_references",
        *data->GetArray("target_references"));
  }
  ObjectRequest update;
  update.operation = ObjectOperation::kPut;
  update.kind = ObjectKind::kAutomation;
  update.scope = access.scope;
  update.id = id;
  update.ttl_seconds = 0;
  update.expected_revision = record.GetInt("revision");
  update.data = *data;
  if (scheduled) {
    if (!pending) Set(&update.data, "pending_delivery", String(invocation.id));
    Set(&update.data, "schedule_cursor", Number(occurrence));
    if (automation.value.GetString("type") == "once")
      Set(&Field(update.data, "resource"), "status", String("completed"));
  }
  const ObjectRequest writes[]{std::move(invocation), std::move(update)};
  auto result = objects.ExecuteBatch(writes, access.deadline, access.cancelled);
  if (result.ok()) result.value = std::move(resource);
  if (result.commit_unknown)
    result.value = Object({{"id", String(resource.GetString("id"))}});
  return result;
}

ObjectReply Schedule(ObjectStore& objects, const StateAccess& access,
                     const Json& record, int64_t now) {
  using namespace std::chrono;
  const auto* data = record.Find("data");
  if (!data) return Corrupt();
  const auto id = record.GetString("id");
  auto checked = PublicAutomation(*data, id);
  if (!checked.ok()) return checked;
  const auto& row = checked.value;
  if (row.GetString("status") != "active") return {};
  if (row.GetString("type") == "once") {
    const auto at = row.GetInt("run_at");
    return at <= now ? Queue(objects, access, record, now, at) : ObjectReply{};
  }
  const auto cursor = data->GetInt("schedule_cursor");
  if (cursor < 0 || now / 60 <= cursor / 60) return {};
  const auto pattern = AgentCron::Parse(row.GetString("expression"));
  if (!pattern) return Corrupt();
  const time_zone* zone;
  try {
    zone = locate_zone(row.GetString("timezone"));
  } catch (const std::exception&) {
    return Error(503, "automation_timezone_unavailable",
                 "System timezone data unavailable");
  }
  // A bounded pass advances the persistent cursor in order after downtime.
  // One successful CAS claims each occurrence. No request is replayed.
  const auto end = std::min(now / 60, cursor / 60 + 1440);
  for (auto minute = cursor / 60 + 1; minute <= end; ++minute)
    if (pattern->Matches(sys_time<minutes>(minutes(minute)), *zone))
      return Queue(objects, access, record, now, minute * 60);
  if (end - cursor / 60 < 60) return {};
  ObjectRequest update;
  update.operation = ObjectOperation::kPut;
  update.kind = ObjectKind::kAutomation;
  update.scope = access.scope;
  update.id = id;
  update.expected_revision = record.GetInt("revision");
  update.ttl_seconds = 0;
  update.data = *data;
  Set(&update.data, "schedule_cursor", Number(end * 60));
  return objects.Execute(update, access.deadline, access.cancelled);
}
}  // namespace q4t::server::automation_detail
