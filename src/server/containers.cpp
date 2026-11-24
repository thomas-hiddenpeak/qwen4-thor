#include "containers_internal.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_json.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server::container_detail {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string field) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(field))}});
  return result;
}
ObjectReply Corrupt() {
  return Error(503, "container_state_invalid", "Invalid stored container");
}

ObjectReply Public(const Json& record, int64_t now) {
  const auto* data = record.Find("data");
  const auto* info = data ? data->Find("resource") : nullptr;
  const auto* lifetime = info ? info->Find("expires_after") : nullptr;
  const auto minutes = lifetime ? lifetime->GetInt("minutes", -1) : -1;
  const auto last = info ? info->GetInt("last_active_at", -1) : -1;
  ChatPlanError error;
  if (!info || last <= 0 || minutes < 1 || minutes > (INT64_MAX - last) / 60 ||
      lifetime->GetString("anchor") != "last_active_at" ||
      data->GetInt("workspace_expires_at") != last + minutes * 60 ||
      info->GetInt("created_at") != record.GetInt("created_at") ||
      info->GetString("status") != "running" ||
      data->GetString("format") != "q4t.container.v1" ||
      info->GetString("id") != record.GetString("id") ||
      info->GetString("object") != "container" ||
      !ValidateContainer(*info, ContainerShape::kResource, &error))
    return Corrupt();
  ObjectReply result;
  result.value = *info;
  if (now >= data->GetInt("workspace_expires_at"))
    Set(&result.value, "status", String("expired"));
  return result;
}

ObjectReply Read(ObjectStore& objects, const StateAccess& access,
                 std::string_view id, Stored* state) {
  state->metadata.kind = ObjectKind::kContainer;
  state->metadata.scope = access.scope;
  state->metadata.id = id;
  auto row =
      objects.Execute(state->metadata, access.deadline, access.cancelled);
  if (!row.ok()) return row;
  auto resource = Public(row.value, std::time(nullptr));
  if (!resource.ok()) return resource;
  state->metadata.data = *row.value.Find("data");
  state->metadata.expected_revision = row.value.GetInt("revision");
  state->metadata.ttl_seconds = 0;
  state->expires = state->metadata.data.GetInt("workspace_expires_at");
  return resource;
}

ObjectReply OpenWorkspace(ObjectStore& objects, const StateAccess& access,
                          Stored* state) {
  if (std::time(nullptr) >= state->expires)
    return Error(409, "container_expired", "Container workspace has expired");
  auto& work = state->workspace;
  work.kind = ObjectKind::kToolRun;
  work.id = "workspace_" + state->metadata.id;
  work.scope = access.scope;
  auto row = objects.Execute(work, access.deadline, access.cancelled);
  if (!row.ok()) {
    if (row.http_status == 404 && std::time(nullptr) >= state->expires)
      return Error(409, "container_expired", "Container workspace has expired");
    return row.http_status == 404 ? Corrupt() : row;
  }
  const auto* data = row.value.Find("data");
  if (!data || data->GetString("format") != "q4t.container_workspace.v1" ||
      data->GetString("container_id") != state->metadata.id ||
      !data->GetArray("files"))
    return Corrupt();
  if (row.value.GetInt("expires_at") != state->expires)
    return Error(409, "object_revision_conflict", "Container activity changed");
  work.data = *data;
  work.expected_revision = row.value.GetInt("revision");
  return {};
}

ObjectReply Commit(ObjectStore& objects, const StateAccess& access,
                   Stored state, std::vector<ObjectRequest> extra) {
  auto& info = Field(state.metadata.data, "resource");
  const int64_t now =
      std::max<int64_t>(std::time(nullptr), info.GetInt("last_active_at"));
  const int64_t minutes = info.Find("expires_after")->GetInt("minutes");
  if (minutes < 1 || minutes > (INT64_MAX - now) / 60)
    return Invalid("Invalid container expiration", "expires_after.minutes");
  const auto expires = now + 60 * minutes;
  Set(&info, "last_active_at", Number(now));
  auto resource = info;
  Set(&state.metadata.data, "workspace_expires_at", Number(expires));
  state.metadata.ttl_seconds = 0;
  state.metadata.operation = state.workspace.operation = ObjectOperation::kPut;
  state.metadata.mutate_before = state.workspace.mutate_before = state.expires;
  state.workspace.expires_at = expires;
  state.workspace.ttl_seconds = 0;
  const auto id = state.metadata.id;
  extra.push_back(std::move(state.metadata));
  extra.push_back(std::move(state.workspace));
  auto result = objects.ExecuteBatch(extra, access.deadline, access.cancelled);
  if (result.ok())
    result.value = std::move(resource);
  else if (result.commit_unknown)
    result.value = Object({{"id", String(id)}});
  return result;
}

namespace {
ObjectReply Create(ObjectStore& objects, const StateAccess& access,
                   const Json& body) {
  ChatPlanError validation;
  auto ok = ValidateContainer(body, ContainerShape::kCreate, &validation);
  if (!ok) return Invalid(ok.message(), validation.param);
  const auto id = NewObjectId(ObjectKind::kContainer);
  if (id.empty())
    return Error(503, "object_entropy_unavailable", "No container ID");
  const auto now = std::time(nullptr);
  auto expires =
      Object({{"anchor", String("last_active_at")}, {"minutes", Number(20)}});
  if (const auto* value = body.Find("expires_after")) expires = *value;
  const auto minutes = expires.GetInt("minutes", -1);
  if (minutes < 1 || minutes > (INT64_MAX - now) / 60 ||
      expires.Find("minutes")->number != static_cast<double>(minutes))
    return Invalid("Expected positive whole minutes", "expires_after.minutes");
  auto network = body.Has("network_policy")
                     ? *body.Find("network_policy")
                     : Object({{"type", String("disabled")}});
  auto execution = Object();
  if (const auto* secrets = network.GetArray("domain_secrets")) {
    const auto* domains = network.GetArray("allowed_domains");
    for (const auto& secret : secrets->array) {
      const auto host = secret.GetString("domain");
      if (host.empty() || !domains ||
          std::ranges::none_of(domains->array, [&](const Json& domain) {
            return domain.str == host;
          }))
        return Invalid("Secret domain must be in allowed_domains",
                       "network_policy.domain_secrets");
    }
    Set(&execution, "domain_secrets", *secrets);
    std::erase_if(network.object,
                  [](const auto& e) { return e.first == "domain_secrets"; });
  }
  if (const auto* skills = body.GetArray("skills");
      skills && !skills->array.empty())
    Set(&execution, "skills", *skills);
  Json info =
      Object({{"object", String("container")},
              {"id", String(id)},
              {"name", *body.Find("name")},
              {"created_at", Number(now)},
              {"last_active_at", Number(now)},
              {"status", String("running")},
              {"memory_limit", String(body.GetString("memory_limit", "1g"))},
              {"expires_after", std::move(expires)},
              {"network_policy", std::move(network)}});
  ok = ValidateContainer(info, ContainerShape::kCreated, &validation);
  if (!ok) return Corrupt();
  Stored state;
  state.metadata.kind = ObjectKind::kContainer;
  state.metadata.id = id;
  state.metadata.created_at = now;
  state.metadata.scope = access.scope;
  state.metadata.data = Object(
      {{"format", String("q4t.container.v1")}, {"resource", std::move(info)}});
  state.workspace.kind = ObjectKind::kToolRun;
  state.workspace.scope = access.scope;
  state.workspace.id = "workspace_" + id;
  state.workspace.data =
      Object({{"format", String("q4t.container_workspace.v1")},
              {"container_id", String(id)},
              {"files", Array()}});
  if (!execution.object.empty()) {
    std::string sealed;
    auto stored = StateCipher(objects).Seal(
        OutputJson(execution), {"q4t-containers", id, "execution-options", id},
        access, &sealed);
    if (!stored.ok()) return stored;
    Set(&state.workspace.data, "execution_options", String(std::move(sealed)));
  }
  std::vector<BinaryRead> pins;
  ScopeExit cleanup([&] { CloseFiles(objects, access, &pins); });
  std::vector<ObjectRequest> writes;
  if (const auto* files = body.GetArray("file_ids")) {
    auto prepared =
        PrepareFiles(objects, access, *files, &state, &pins, &writes);
    if (!prepared.ok()) return prepared;
  }
  return Commit(objects, access, std::move(state), std::move(writes));
}

ObjectReply List(ObjectStore& objects, const StateAccess& access,
                 const Json& query) {
  ChatPlanError error;
  auto valid = ValidateContainer(query, ContainerShape::kQuery, &error);
  if (!valid) return Invalid(valid.message(), error.param);
  const int limit = query.GetInt("limit", 20);
  if (limit < 1 || limit > 100)
    return Invalid("Expected 1 to 100 entries", "limit");
  ObjectRequest page;
  page.scope = access.scope;
  page.kind = ObjectKind::kContainer;
  page.operation = ObjectOperation::kList;
  page.after = query.GetString("after");
  page.descending = query.GetString("order", "desc") == "desc";
  if (query.Has("after") &&
      ParseContainerRoute("/v1/containers/" + page.after).kind !=
          ContainerRouteKind::kContainer)
    return Invalid("Invalid container cursor", "after");
  page.limit = limit;
  auto result = Array();
  bool more = false;
  do {
    auto rows = objects.Execute(page, access.deadline, access.cancelled);
    if (!rows.ok()) return rows;
    const auto* items = rows.value.GetArray("data");
    if (!items) return Corrupt();
    for (const auto& row : items->array) {
      auto resource = Public(row, std::time(nullptr));
      if (!resource.ok()) return resource;
      if (query.Has("name") &&
          resource.value.GetString("name") != query.GetString("name"))
        continue;
      if (result.array.size() == static_cast<size_t>(limit)) {
        more = true;
        break;
      }
      result.array.push_back(std::move(resource.value));
    }
    if (more || !rows.value.GetBool("has_more")) break;
    if (items->array.empty()) return Corrupt();
    page.after = items->array.back().GetString("id");
  } while (true);
  const auto first =
      result.array.empty() ? String("") : *result.array.front().Find("id");
  const auto last =
      result.array.empty() ? String("") : *result.array.back().Find("id");
  ObjectReply reply;
  reply.value = Object({{"object", String("list")},
                        {"data", std::move(result)},
                        {"first_id", first},
                        {"last_id", last},
                        {"has_more", Boolean(more)}});
  return reply;
}
}  // namespace
}  // namespace q4t::server::container_detail

namespace q4t::server {
ObjectReply HandleContainers(ObjectStore& objects, const StateAccess& access,
                             std::string_view method, std::string_view target,
                             const std::string& body, BinaryRead* download,
                             BinaryStage* upload, std::string_view filename) {
  using namespace container_detail;
  using namespace object_detail;
  using Kind = ContainerRouteKind;
  const auto route = ParseContainerRoute(target);
  if (route.kind == Kind::kNone)
    return Error(404, "route_not_found", "Unknown container route");
  if (!ContainerMethodAllowed(method, target)) {
    auto result = Error(405, "method_not_allowed", "Method not allowed");
    result.value = Object(
        {{"param", String("method")},
         {"allow",
          String(route.kind == Kind::kFiles || route.kind == Kind::kCollection
                     ? "GET, POST"
                 : route.kind == Kind::kContent ? "GET"
                                                : "GET, DELETE")}});
    return result;
  }
  const auto split = target.find('?');
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  if (!query.value.object.empty() &&
      !(method == "GET" &&
        (route.kind == Kind::kCollection || route.kind == Kind::kFiles)))
    return Invalid("Unknown query for this operation", "query");
  if (method != "POST" && !body.empty())
    return Invalid("Unexpected request body", "body");
  if (route.kind == Kind::kCollection) {
    if (method == "GET") return List(objects, access, query.value);
    Json input;
    auto parsed = io::ParseJson(body, &input, true, kApplicationJsonLimits);
    if (!parsed) return Invalid(parsed.message(), "body");
    return Create(objects, access, input);
  }
  // Known CAS conflicts have no effects. Re-read both halves so concurrent
  // access can refresh the same workspace without resurrecting expired bytes.
  const auto once = [&]() -> ObjectReply {
    Stored state;
    auto resource = Read(objects, access, route.container, &state);
    if (!resource.ok()) return resource;
    const bool expired = resource.value.GetString("status") == "expired";
    if (route.kind == Kind::kContainer && method == "DELETE") {
      std::vector<ObjectRequest> changes;
      if (!expired) {
        auto opened = OpenWorkspace(objects, access, &state);
        if (!opened.ok()) return opened;
        state.workspace.operation = ObjectOperation::kErase;
        changes.push_back(std::move(state.workspace));
      }
      state.metadata.operation = ObjectOperation::kErase;
      changes.push_back(std::move(state.metadata));
      auto erased =
          objects.ExecuteBatch(changes, access.deadline, access.cancelled);
      if (erased.ok())
        erased.value = Object({{"object", String("container.deleted")},
                               {"id", String(std::string(route.container))},
                               {"deleted", Boolean(true)}});
      return erased;
    }
    if (expired && route.kind == Kind::kContainer) return resource;
    auto opened = OpenWorkspace(objects, access, &state);
    if (!opened.ok()) return opened;
    if (route.kind == Kind::kContainer) {
      if (resource.value.GetInt("last_active_at") >= std::time(nullptr))
        return resource;
      return Commit(objects, access, std::move(state));
    }
    return ContainerFiles(objects, access, route, method, query.value, body,
                          std::move(state), download, upload, filename);
  };
  for (;;) {
    auto result = once();
    if (result.commit_unknown || result.code != "object_revision_conflict" ||
        std::chrono::steady_clock::now() >= access.deadline ||
        (access.cancelled && access.cancelled()))
      return result;
  }
}
}  // namespace q4t::server
