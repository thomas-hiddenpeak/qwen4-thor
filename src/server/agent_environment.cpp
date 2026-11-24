#include "q4t/server/agent_environment.h"

#include <algorithm>
#include <ctime>

#include "agent_environment_internal.h"
#include "object_protocol.h"
#include "q4t/server/agent_vault.h"
#include "q4t/server/application_query.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/request_json.h"
#include "q4t/server/request_limits.h"
#include "q4t/server/input_snapshot_bindings.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace environment_detail;
constexpr int64_t kWarmSeconds = 300;
constexpr int64_t kReplaySeconds = 86400;

ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply Corrupt() {
  return Error(503, "environment_record_invalid",
               "Invalid stored execution environment");
}
ObjectReply Resource(const Json& data, const std::string& id) {
  const auto* info = data.Find("resource");
  ChatPlanError error;
  if (!info || info->GetString("id") != id ||
      !ValidateEnvironment(*info, EnvironmentShape::kResource, &error) ||
      data.GetInt("warm_until") <= 0)
    return Corrupt();
  ObjectReply out;
  out.value = *info;
  if (info->GetString("status") == "ready" &&
      std::time(nullptr) >= data.GetInt("warm_until"))
    Set(&out.value, "status", String("expired"));
  return out;
}
ObjectReply Read(ObjectStore& objects, const StateAccess& access,
                 ObjectKind kind, const std::string& id) {
  ObjectRequest read;
  read.kind = kind;
  read.scope = access.scope;
  read.id = id;
  return objects.Execute(read, access.deadline, access.cancelled);
}
ObjectReply Replay(ObjectStore& objects, const StateAccess& access,
                   const Json& receipt, const std::string& digest) {
  const auto* data = receipt.Find("data");
  if (!data || data->GetString("format") != "q4t.environment.creation.v1")
    return Corrupt();
  if (data->GetString("request_sha256") != digest)
    return Error(409, "idempotency_key_conflict",
                 "This key already identifies another environment request");
  const auto id = data->GetString("environment_id");
  if (!id.starts_with("environment_") || !ValidId(id)) return Corrupt();
  auto saved = Read(objects, access, ObjectKind::kEnvironment, id);
  if (!saved.ok()) {
    if (saved.http_status == 404)
      return Error(409, "environment_unavailable",
                   "Previously created environment is no longer available");
    return saved;
  }
  return Resource(Field(saved.value, "data"), id);
}

ObjectReply PrepareRecord(ObjectStore& objects, const StateAccess& access,
                          const Json& input, const Json& vaults,
                          ObjectRequest& resource,
                          std::shared_ptr<environment_detail::WorkspaceCopies>* copies) {
  resource.kind = ObjectKind::kEnvironment;
  resource.scope = access.scope;
  resource.id = NewObjectId(resource.kind);
  if (resource.id.empty())
    return Error(503, "environment_id_unavailable", "No environment ID");
  Json config, sealed;
  *copies = std::make_shared<environment_detail::WorkspaceCopies>(objects, access, resource.id);
  auto prepared = PrepareWorkspace(objects, access, resource.id,
                                   input, &config, **copies);
  if (!prepared.ok()) return prepared;
  auto secure = SealDefinition(objects, access, resource.id, config, &sealed);
  if (!secure.ok()) return secure;
  resource.operation = ObjectOperation::kPut;
  resource.ttl_seconds = kReplaySeconds;
  resource.data =
      Object({{"resource", prepared.value},
              {"sealed_definition", std::move(sealed)},
              {"warm_until", Number(std::time(nullptr) + kWarmSeconds)},
              {"vault_ids", vaults}});
  (*copies)->Bind(&resource.data);
  return prepared;
}

ObjectReply Create(ObjectStore& objects, const StateAccess& access,
                   const Json& body, std::string_view key) {
  if (body.Has("Idempotency-Key"))
    return Invalid("Supply Idempotency-Key as a header", "Idempotency-Key");
  ChatPlanError error;
  auto shape = ValidateEnvironment(body, EnvironmentShape::kCreate, &error);
  if (!shape) return Invalid(shape.message(), error.param);
  ObjectRequest marker;
  std::string digest;
  if (!key.empty()) {
    auto encoded = InputValueIdentity(body, &digest);
    if (!encoded) return Invalid(encoded.message(), "body");
    marker.kind = ObjectKind::kToolRun;
    marker.id = "envkey_" + Digest(key);
    marker.scope = access.scope;
    auto old = objects.Execute(marker, access.deadline, access.cancelled);
    if (old.ok()) return Replay(objects, access, old.value, digest);
    if (old.http_status != 404) return old;
  }
  auto vaults = Array();
  if (const auto* ids = body.GetArray("vault_ids")) {
    auto checked = CheckAgentVaults(objects, access, *ids);
    if (!checked.ok()) return checked;
    vaults = *ids;
  }
  ObjectRequest resource;
  std::shared_ptr<environment_detail::WorkspaceCopies> copies;
  auto prepared = PrepareRecord(objects, access, *body.Find("environment"),
                                vaults, resource, &copies);
  if (!prepared.ok()) return prepared;
  ObjectReply committed;
  if (key.empty()) {
    committed = objects.Execute(resource, access.deadline, access.cancelled);
  } else {
    marker.operation = ObjectOperation::kPut;
    marker.ttl_seconds = kReplaySeconds;
    marker.data = Object({{"format", String("q4t.environment.creation.v1")},
                          {"environment_id", String(resource.id)},
                          {"request_sha256", String(digest)}});
    const auto marker_id = marker.id;
    const ObjectRequest mutations[] = {std::move(resource), std::move(marker)};
    committed =
        objects.ExecuteBatch(mutations, access.deadline, access.cancelled);
    if (!committed.commit_unknown && committed.http_status == 409) {
      auto old = Read(objects, access, ObjectKind::kToolRun, marker_id);
      if (old.ok()) return Replay(objects, access, old.value, digest);
      if (old.http_status != 404) return old;
    }
  }
  copies->Settle(committed);
  if (committed.commit_unknown)
    committed.value = Object({{"id", *prepared.value.Find("id")}});
  return committed.ok() ? prepared : committed;
}

ObjectReply List(ObjectStore& objects, const StateAccess& access, Json query) {
  ChatPlanError error;
  auto shape = ValidateEnvironment(query, EnvironmentShape::kQuery, &error);
  if (!shape) return Invalid(shape.message(), error.param);
  const auto type = query.GetString("type", "openai_hosted");
  std::erase_if(query.object,
                [](const auto& entry) { return entry.first == "type"; });
  ItemViewOptions page;
  auto valid = ParseItemViewOptions(query, &page, &error, true);
  if (!valid) return Invalid(valid.message(), error.param);
  ObjectRequest req;
  req.scope = access.scope;
  req.kind = ObjectKind::kEnvironment;
  req.operation = ObjectOperation::kList;
  req.after = page.after;
  req.limit = std::min(8, page.limit + 1);
  req.descending = page.descending;
  auto visible = Array();
  bool more = false;
  size_t page_bytes = 0;
  for (;;) {
    auto list = objects.Execute(req, access.deadline, access.cancelled);
    if (!list.ok()) return list;
    for (auto& row : Field(list.value, "data").array) {
      auto view = Resource(Field(row, "data"), row.GetString("id"));
      if (!view.ok()) return view;
      const auto status = view.value.GetString("status");
      if (status == "expired" || status == "failed" ||
          view.value.GetString("type") != type) continue;
      if (visible.array.size() == static_cast<size_t>(page.limit)) {
        more = true;
        break;
      }
      const auto bytes = OutputJson(view.value).size();
      if (bytes > kChatBodyBytes - page_bytes) {
        if (visible.array.empty())
          return Error(413, "environment_view_limit", "Environment exceeds the list budget");
        more = true;
        break;
      }
      page_bytes += bytes;
      visible.array.push_back(std::move(view.value));
    }
    if (more || !list.value.GetBool("has_more")) break;
    auto next = list.value.GetString("last_id");
    if (next.empty() || next == req.after) return Corrupt();
    req.after = std::move(next);
  }
  const auto edge = [&](bool first) {
    if (visible.array.empty()) return Json{};
    return *(first ? visible.array.front() : visible.array.back()).Find("id");
  };
  auto first = edge(true), last = edge(false);
  ObjectReply result;
  result.value = Object({{"object", String("list")}, {"data", std::move(visible)},
                         {"has_more", Boolean(more)}, {"first_id", std::move(first)},
                         {"last_id", std::move(last)}});
  return result;
}
}  // namespace

ObjectReply environment_detail::PrepareAttachment(
    ObjectStore& objects, const StateAccess& access, const Json& input,
    const std::string& session_id, const Json& vaults, ObjectRequest* write,
    std::shared_ptr<WorkspaceCopies>* copies) {
  auto& req = *write;
  const auto* existing = input.Find("environment_id");
  if (existing) {
    if (input.object.size() != 2)
      return Invalid("Existing environment cannot be reconfigured on attachment",
                     "environment");
    if (!existing->IsString() || !existing->str.starts_with("environment_") ||
        !ValidId(existing->str))
      return Error(404, "object_not_found", "Environment not found");
    req.kind = ObjectKind::kEnvironment;
    req.scope = access.scope;
    req.id = existing->str;
    auto row = objects.Execute(req, access.deadline, access.cancelled);
    if (!row.ok()) return row;
    req.data = std::move(Field(row.value, "data"));
    auto view = Resource(req.data, req.id);
    if (!view.ok()) return view;
    if (view.value.GetString("status") != "ready")
      return Error(409, "environment_not_ready",
                   "Environment is no longer available for attachment");
    req.operation = ObjectOperation::kPut;
    req.expected_revision = row.value.GetInt("revision");
    req.mutate_before = req.data.GetInt("warm_until");
    const auto* ids = req.data.GetArray("vault_ids");
    if (!ids) return Corrupt();
    auto checked = CheckAgentVaults(objects, access, *ids);
    if (!checked.ok()) return checked;
  } else {
    auto prepared = PrepareRecord(objects, access, input, vaults, req, copies);
    if (!prepared.ok()) return prepared;
  }
  const auto* sealed = req.data.Find("sealed_definition");
  if (!sealed) return Corrupt();
  Json config;
  auto opened = OpenDefinition(objects, access, req.id, *sealed, &config);
  if (!opened.ok()) return opened;
  // The installed file list has session-scoped IDs and preserves Files origin.
  Set(&config, "files", Array());
  auto settings = PublicTemplate(config, req.id, 0, 0);
  if (!settings.ok()) return settings;
  auto& resource = Field(req.data, "resource");
  Json session = Object({{"type", String("openai_hosted")},
                         {"id", String(req.id)},
                         {"files", *resource.Find("files")}});
  for (const auto* key : {"network", "packages", "skills", "plugins",
                          "capability_directories", "desktop"})
    Set(&session, key, *settings.value.Find(key));
  Set(&resource, "status", String("connected"));
  Set(&req.data, "bound_session", String(session_id));
  req.ttl_seconds = 0;
  ObjectReply result;
  result.value = std::move(session);
  return result;
}

ObjectReply HandleHostedEnvironment(
    ObjectStore& objects, std::string_view method, std::string_view target,
    const std::string& body, const StateAccess& access, std::string_view key) {
  const auto route = ParseHostedRoute(target);
  if (route.kind == HostedRouteKind::kNone ||
      !ManagedAgentMethodAllowed(method, target))
    return Error(404, "route_not_found", "No environment route");
  auto split = target.find('?');
  if (method == "GET" && !body.empty())
    return Invalid("Unexpected body", "body");
  Json input;
  if (method == "POST") {
    if (split != target.npos) return Invalid("Unexpected query", "query");
    if (!io::ParseJson(body, &input, true, kApplicationJsonLimits))
      return Invalid("Expected JSON body", "body");
  }
  if (route.kind == HostedRouteKind::kCollection) {
    if (method == "POST") {
      auto created = Create(objects, access, input, key);
      if (created.ok()) created.http_status = 201;
      return created;
    }
    auto query = ParseApplicationQuery(
        split == target.npos ? "" : target.substr(split + 1));
    return query.ok() ? List(objects, access, std::move(query.value)) : query;
  }
  if (route.kind == HostedRouteKind::kResource && split != target.npos)
    return Invalid("Unexpected query", "query");
  const std::string id(route.id);
  auto saved = Read(objects, access, ObjectKind::kEnvironment, id);
  if (!saved.ok()) return saved;
  auto view = Resource(Field(saved.value, "data"), id);
  if (!view.ok() || route.kind == HostedRouteKind::kResource) return view;
  return environment_detail::WorkspaceFiles(objects, access, method, target,
                                            input, saved.value, view.value);
}
}  // namespace q4t::server
