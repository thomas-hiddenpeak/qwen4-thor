#include "q4t/server/safety_records.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/client_identity.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/schema_graph.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr std::string_view kFormat = "q4t.safety.record.v1";

ObjectReply Invalid(std::string param) {
  auto result = Error(400, "invalid_request", "Invalid safety record field");
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}

ObjectReply InvalidStored() {
  return Error(503, "safety_record_invalid", "Stored safety record is invalid");
}

bool AlertId(std::string_view id) {
  return id.size() == 38 && id.starts_with("alert_") &&
         std::all_of(id.begin() + 6, id.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

bool ValidRecord(const Json& record, bool alert) {
  const auto id = record.GetString("id");
  const auto* reason = record.Find("reason");
  if (!record.IsObject() || !reason ||
      (!reason->IsNull() && !reason->IsString()) ||
      record.GetInt("created_at") <= 0 ||
      record.GetInt("created_at") > (1LL << 53) - 1)
    return false;
  std::string bad;
  if (alert) {
    const auto type = record.GetString("error_type");
    const auto* paused = record.Find("request_paused");
    const auto* detail = record.Find("detailed_explanation");
    return (!detail || detail->IsNull()) &&
           InputFields(record,
                       {"id", "object", "created_at", "reason", "model",
                        "request_id", "response_id", "request_paused",
                        "error_type", "detailed_explanation"},
                       &bad, "resource") &&
           AlertId(id) && record.GetString("object") == "safety.alert" &&
           !record.GetString("request_id").empty() &&
           !record.GetString("response_id").empty() &&
           !record.GetString("model").empty() && paused && paused->IsBool() &&
           (type == "potentially_unintended_data_access" ||
            type == "potentially_unintended_data_transfer" ||
            type == "potentially_unintended_destructive_activity" ||
            type == "other");
  }
  const auto* notice = record.Find("notice");
  if (!notice || !notice->IsObject() || notice->object.size() != 1)
    return false;
  const auto type = notice->GetString("type");
  return InputFields(record,
                     {"id", "object", "created_at", "reason",
                      "entity_identifier", "notice"},
                     &bad, "resource") &&
         record.GetString("object") == "safety.case" && ValidId(id) &&
         !record.GetString("entity_identifier").empty() &&
         (type == "warning" || type == "deactivation");
}

ObjectRequest Read(std::string scope, ObjectKind kind, std::string id) {
  ObjectRequest req;
  req.scope = std::move(scope);
  req.kind = kind;
  req.id = std::move(id);
  return req;
}
}  // namespace

ObjectReply PublishSafetyRecord(ObjectStore& store, const Json& command,
                                const StateAccess& access) {
  if (access.scope != "local:key-admin")
    return Error(403, "operator_required", "Local operator required");
  std::string bad;
  const auto client = command.GetString("client_id");
  const auto* submitted = command.Find("resource");
  if (!InputFields(command,
                   {"version", "action", "client_id", "resource",
                    "explanation_expires_at", "record_expires_at"},
                   &bad, "command") ||
      command.GetInt("version") != 1 ||
      command.GetString("action") != "safety_publish" ||
      !ValidClientId(client) || !submitted || !submitted->IsObject())
    return Invalid("command");
  auto resource = *submitted;
  const bool alert = resource.GetString("object") == "safety.alert";
  const auto* explanation = submitted->Find("detailed_explanation");
  const auto* expiry = command.Find("explanation_expires_at");
  const auto now = std::time(nullptr);
  if (explanation) {
    if (!alert || (!explanation->IsNull() && !explanation->IsString()))
      return Invalid("resource.detailed_explanation");
    if (!explanation->IsNull())
      std::erase_if(resource.object, [](const auto& pair) {
        return pair.first == "detailed_explanation";
      });
  }
  const bool temporary = explanation && explanation->IsString();
  if (temporary != (expiry != nullptr) ||
      (temporary &&
       (!resource.Has("reason") || !resource.Find("reason")->IsNull() ||
        expiry->AsInt() <= resource.GetInt("created_at"))))
    return Invalid("explanation_expires_at");
  if (!ValidRecord(resource, alert) || resource.GetInt("created_at") > now)
    return Invalid("resource");
  auto parent = Read("client:" + client,
                     alert ? ObjectKind::kSafetyAlert : ObjectKind::kSafetyCase,
                     resource.GetString("id"));
  const auto* retention = command.Find("record_expires_at");
  const auto record_end =
      retention ? retention->AsInt()
                : resource.GetInt("created_at") + parent.ttl_seconds;
  if (record_end <= now || (temporary && expiry->AsInt() > record_end))
    return Invalid("record_expires_at");
  // A repeated operator receipt for the same immutable finding must not
  // publish a second notification or extend the explanation lifetime.
  std::string canonical;
  if (!CanonicalSchemaValue(Object({{"resource", *submitted},
                                    {"record_expires_at", Number(record_end)},
                                    {"expires_at", expiry ? *expiry : Json()}}),
                            &canonical))
    return Invalid("resource");
  const auto digest = Digest(canonical);
  const auto receipt = [&] {
    ObjectReply ok;
    ok.value = Object({{"id", String(parent.id)}, {"digest", String(digest)}});
    return ok;
  };
  auto previous = [&](const ObjectReply& found) {
    const auto* data = found.value.Find("data");
    if (!found.ok()) return found;
    if (!data || data->GetString("format") != kFormat) return InvalidStored();
    if (data->GetString("digest") != digest)
      return Error(409, "safety_record_conflict",
                   "Safety ID already published");
    return receipt();
  };
  auto found = store.Execute(parent, access.deadline, access.cancelled);
  if (found.code != "object_not_found") return previous(found);
  parent.operation = ObjectOperation::kPut;
  parent.ttl_seconds = 0;
  parent.expires_at = record_end;
  parent.data = Object({{"format", String(std::string(kFormat))},
                        {"resource", std::move(resource)},
                        {"digest", String(digest)}});
  std::vector<ObjectRequest> changes;
  // The persistent summary never contains the temporary explanation.
  if (temporary) {
    if (expiry->AsInt() <= now) return Invalid("explanation_expires_at");
    Set(&parent.data, "explanation_expires_at", *expiry);
    auto detail = Read(parent.scope, ObjectKind::kSafetyDetail, parent.id);
    detail.operation = ObjectOperation::kPut;
    detail.ttl_seconds = 0;
    detail.expires_at = expiry->AsInt();
    detail.data = Object({{"format", String("q4t.safety.detail.v1")},
                          {"digest", String(digest)},
                          {"text", *explanation}});
    changes.push_back(std::move(detail));
  }
  changes.push_back(parent);
  auto result = store.ExecuteBatch(changes, access.deadline, access.cancelled);
  if (result.ok()) return receipt();
  if (result.code == "object_revision_conflict" && !result.commit_unknown) {
    parent.operation = ObjectOperation::kGet;
    return previous(store.Execute(parent, access.deadline, access.cancelled));
  }
  if (result.commit_unknown) result.value = Object({{"id", String(parent.id)}});
  return result;
}

ObjectReply GetSafetyRecord(ObjectStore& store, const StateAccess& access,
                            std::string_view method, std::string_view target,
                            std::string_view body) {
  const auto route = ParseSafetyRoute(target);
  if (route.id.empty())
    return Error(404, "route_not_found", "Unknown safety route");
  if (method != "GET") {
    auto out = Error(405, "method_not_allowed", "Method not allowed");
    out.value = Object({{"allow", String("GET")}});
    return out;
  }
  if (!body.empty()) return Invalid("body");
  if (target.find('?') != target.npos) return Invalid("query");
  if (route.id.size() > (route.alert ? 38U : 128U)) return Invalid("id");
  if (!ValidId(route.id) || (route.alert && !AlertId(route.id)))
    return Error(404, "object_not_found", "Object not found");
  auto req =
      Read(access.scope,
           route.alert ? ObjectKind::kSafetyAlert : ObjectKind::kSafetyCase,
           std::string(route.id));
  auto found = store.Execute(req, access.deadline, access.cancelled);
  if (!found.ok()) return found;
  const auto* data = found.value.Find("data");
  const auto* stored = data ? data->Find("resource") : nullptr;
  if (!stored || stored->GetString("id") != route.id ||
      data->GetString("format") != kFormat)
    return InvalidStored();
  auto resource = *stored;
  if (!ValidRecord(resource, route.alert)) return InvalidStored();
  if (data->GetInt("explanation_expires_at") > std::time(nullptr)) {
    req.kind = ObjectKind::kSafetyDetail;
    auto detail = store.Execute(req, access.deadline, access.cancelled);
    if (!detail.ok() && detail.http_status != 404) return detail;
    if (detail.ok()) {
      const auto* value = detail.value.Find("data");
      if (!value || value->GetString("digest") != data->GetString("digest") ||
          value->GetString("format") != "q4t.safety.detail.v1" ||
          !value->Has("text") || !value->Find("text")->IsString())
        return InvalidStored();
      if (data->GetInt("explanation_expires_at") > std::time(nullptr))
        Set(&resource, "detailed_explanation", *value->Find("text"));
    }
  }
  ObjectReply result;
  result.value = std::move(resource);
  return result;
}
}  // namespace q4t::server
