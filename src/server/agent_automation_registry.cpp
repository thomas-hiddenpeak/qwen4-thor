#include "agent_automation_internal.h"

#include <algorithm>

#include "agent_definition_cipher.h"
#include "object_protocol.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/request_json.h"

namespace q4t::server::automation_detail {
namespace {
using namespace object_detail;
using Kind = AutomationRouteKind;

ObjectReply Invalid(std::string message, std::string key) {
  auto out = Error(400, "invalid_request", std::move(message));
  out.value = Object({{"param", String(std::move(key))}});
  return out;
}

ObjectReply CheckInput(const Json& value, AutomationShape shape) {
  ChatPlanError error;
  const auto status = ValidateAutomation(value, shape, &error);
  if (!status) return Invalid(status.message(), error.param);
  return {};
}
}  // namespace

ObjectReply PublicAutomation(const Json& data, const std::string& id) {
  const auto* row = data.Find("resource");
  ChatPlanError error;
  if (!row || row->GetString("id") != id ||
      !ValidateAutomation(*row, AutomationShape::kResource, &error))
    return Error(503, "automation_record_invalid", "Invalid saved automation");
  ObjectReply out;
  out.value = *row;
  return out;
}

ObjectReply Registry(ObjectStore& objects, const StateAccess& access,
                     const ChatPlanConfig& config, const AutomationRoute& route,
                     std::string_view method, const Json& body, int64_t now) {
  const bool create = route.kind == Kind::kCollection;
  ObjectRequest req;
  req.kind = ObjectKind::kAutomation;
  req.scope = access.scope;
  req.id = create ? NewObjectId(req.kind) : std::string(route.automation);
  if (req.id.empty())
    return Error(503, "automation_id_unavailable", "No automation ID");
  req.ttl_seconds = 0;
  auto execute = [&] {
    return objects.Execute(req, access.deadline, access.cancelled);
  };
  Json resource;
  if (create) {
    req.data = Object({{"format", String("q4t.agent.automation.v1")}});
    resource =
        Object({{"type", String(body.GetString("type"))},
                {"id", String(req.id)},
                {"object", String("agent.automation")},
                {"created_at", Number(now)},
                {"metadata", Object()},
                {"status", String("active")},
                {"created_by", Object({{"type", String("developer")}})}});
  } else {
    auto prior = execute();
    if (!prior.ok()) return prior;
    req.data = Field(prior.value, "data");
    auto found = PublicAutomation(req.data, req.id);
    if (!found.ok() || method == "GET") return found;
    req.expected_revision = prior.value.GetInt("revision");
    if (method == "DELETE") {
      req.operation = ObjectOperation::kErase;
      auto deleted = execute();
      if (deleted.ok())
        deleted.value = Object({{"id", String(req.id)},
                                {"object", String("agent.automation.deleted")},
                                {"deleted", Boolean(true)}});
      if (deleted.commit_unknown)
        deleted.value = Object({{"id", String(req.id)}});
      return deleted;
    }
    resource = std::move(found.value);
  }

  if (route.kind == Kind::kPause || route.kind == Kind::kResume) {
    if (!body.IsObject() || !body.object.empty())
      return Invalid("Unexpected automation action body", "body");
    if (resource.GetString("status") == "completed")
      return Error(409, "automation_completed",
                   "The one-time occurrence finished");
    const auto* desired = route.kind == Kind::kPause ? "paused" : "active";
    if (resource.GetString("status") == desired) {
      ObjectReply same;
      same.value = std::move(resource);
      return same;
    }
    if (route.kind == Kind::kResume && resource.GetString("type") == "once" &&
        resource.GetInt("run_at") <= now) {
      bool pending;
      auto lookup = StillQueued(objects, access, req.data, req.id, &pending);
      if (!lookup.ok()) return lookup;
      if (!pending)
        return Invalid("Set a future occurrence before resuming", "run_at");
    }
    Set(&resource, "status", String(desired));
    Set(&req.data, "schedule_cursor", Number(now));
  } else {
    auto valid = CheckInput(
        body, create ? AutomationShape::kCreate : AutomationShape::kUpdate);
    if (!valid.ok()) return valid;
    if (const auto* name = body.Find("name")) {
      const auto length = std::ranges::count_if(
          name->str, [](unsigned char byte) { return (byte & 0xc0) != 0x80; });
      if (length == 0 || length > 256)
        return Invalid("Expected an automation name", "name");
      Set(&resource, "name", *name);
    }
    if (const auto* meta = body.Find("metadata")) {
      auto normalized = NormalizeConversationMetadata(*meta);
      if (!normalized.ok()) return normalized;
      Set(&resource, "metadata", std::move(normalized.value));
    }
    if (const auto* target = body.Find("target")) {
      if (!create && resource.Find("target")->GetString("type") !=
                         target->GetString("type"))
        return Invalid("An update must retain the target type", "target.type");
      auto public_target = ProjectTarget(objects, access, config, *target);
      if (!public_target.ok()) return public_target;
      const auto dependencies = InspectAutomationTarget(public_target.value);
      if (dependencies.uncertain)
        return Invalid("Invalid automation target references", "target");
      auto references = Array();
      for (const auto& id : dependencies.references)
        references.array.push_back(String(id));
      Set(&req.data, "target_references", std::move(references));
      Json sealed;
      auto saved = agent_detail::SealDefinition(
          objects, access, agent_detail::DefinitionKind::kAutomationTarget,
          req.id, *target, &sealed);
      if (!saved.ok()) return saved;
      Set(&req.data, "sealed_target", std::move(sealed));
      Set(&resource, "target", std::move(public_target.value));
    }
    auto check = ApplyClock(body, now, &resource);
    if (!check.ok()) return check;
    if (create || body.Has("timezone") || body.Has("expression") ||
        body.Has("run_at"))
      Set(&req.data, "schedule_cursor", Number(now));
  }
  Set(&req.data, "resource", resource);
  auto checked = PublicAutomation(req.data, req.id);
  if (!checked.ok()) return checked;
  req.operation = ObjectOperation::kPut;
  auto saved = execute();
  if (saved.ok()) {
    saved.http_status = create ? 201 : 200;
    saved.value = std::move(resource);
  } else if (saved.commit_unknown) {
    saved.value = Object({{"id", String(req.id)}});
  }
  return saved;
}
}  // namespace q4t::server::automation_detail
