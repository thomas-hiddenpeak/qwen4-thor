#include "agent_artifact_internal.h"

#include "agent_session_internal.h"
#include "object_protocol.h"
#include "q4t/server/files_http.h"

namespace q4t::server::agent_detail {
namespace {
using namespace object_detail;
ObjectReply Bad(std::string message, std::string field) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(field))}});
  return reply;
}
ObjectReply Invalid() {
  return Error(503, "agent_artifact_state_invalid", "Invalid session artifact");
}
ObjectRequest Lookup(const StateAccess& access, const std::string& id) {
  ObjectRequest request;
  request.scope = access.scope;
  request.kind = ObjectKind::kToolRun;
  request.id = id;
  return request;
}
ObjectReply Metadata(ObjectStore& objects, const StateAccess& access,
                     const std::string& session, const std::string& id,
                     int64_t* revision = nullptr) {
  auto row =
      objects.Execute(Lookup(access, id), access.deadline, access.cancelled);
  if (!row.ok()) return row;
  const auto* data = row.value.Find("data");
  const auto* item = data ? data->Find("resource") : nullptr;
  if (!data || data->GetString("format") != "q4t.agent_artifact.v1" || !item ||
      item->GetString("session_id") != session)
    return Error(404, "object_not_found", "Artifact not found");
  if (item->GetString("id") != id) return Invalid();
  if (revision) *revision = row.value.GetInt("revision");
  ObjectReply result;
  result.value = *item;
  return result;
}

ObjectReply List(ObjectStore& objects, const StateAccess& access,
                 const std::string& session, const Json& links,
                 const Json& query) {
  for (const auto& [key, value] : query.object)
    if (key != "limit" && key != "after" && key != "order" &&
        key != "environment_id")
      return Bad("Unknown artifact query parameter", key);
  const auto order = query.GetString("order", "desc");
  const auto limit = query.GetInt("limit", 20);
  const auto after = query.GetString("after");
  if (limit < 1 || limit > 100)
    return Bad("Expected a limit from 1 to 100", "limit");
  if (order != "asc" && order != "desc")
    return Bad("Invalid list order", "order");
  const auto filter = query.GetString("environment_id");
  const bool reverse = order == "desc";
  bool past_cursor = !query.Has("after");
  bool more = false;
  auto items = Array();
  ResponsesState responses(objects);
  for (size_t i = 0; i < links.array.size() && !more; ++i) {
    const auto& link = links.array[reverse ? links.array.size() - 1 - i : i];
    const auto response_id = link.GetString("response_id");
    auto saved = responses.ReadRecord(response_id, access);
    if (!saved.ok()) return saved;
    if (saved.value.GetString("agent_session_id") != session) return Invalid();
    const auto* ids = saved.value.Find("agent_artifact_ids");
    if (!ids) continue;
    if (!ids->IsArray()) return Invalid();
    for (size_t j = 0; j < ids->array.size(); ++j) {
      const auto& id = ids->array[reverse ? ids->array.size() - 1 - j : j];
      if (!id.IsString() || !ValidId(id.str) ||
          !id.str.starts_with("artifact_"))
        return Invalid();
      if (!past_cursor && id.str != after) continue;
      auto row = Metadata(objects, access, session, id.str);
      if (row.http_status == 404) continue;  // Independently deleted artifact.
      if (!row.ok()) return row;
      if (row.value.GetString("turn_id") != link.GetString("id"))
        return Invalid();
      if (query.Has("environment_id") &&
          row.value.GetString("environment_id") != filter)
        continue;
      if (!past_cursor) {
        past_cursor = true;
        continue;
      }
      if (items.array.size() == static_cast<size_t>(limit)) {
        more = true;
        break;
      }
      items.array.push_back(std::move(row.value));
    }
  }
  if (!past_cursor) return Bad("Unknown artifact cursor", "after");
  const auto first =
      items.array.empty() ? Json{} : *items.array.front().Find("id");
  const auto last =
      items.array.empty() ? Json{} : *items.array.back().Find("id");
  ObjectReply result;
  result.value = Object({{"object", String("list")},
                         {"first_id", first},
                         {"last_id", last},
                         {"has_more", Boolean(more)},
                         {"data", std::move(items)}});
  return result;
}
}  // namespace

ObjectReply HandleArtifacts(ObjectStore& objects, const StateAccess& access,
                            const AgentSessionRoute& route,
                            std::string_view method, const io::Json& query,
                            int fd, bool* sent) {
  const std::string session(route.session);
  auto owner = ReadRow(objects, session, access);
  if (!owner.ok()) return owner;
  const auto* data = owner.value.Find("data");
  const auto* resource = data ? data->Find("session") : nullptr;
  const auto* links = data ? data->GetArray("turns") : nullptr;
  if (!resource || resource->GetString("id") != session || !links ||
      links->array.size() >= kMaxTransactionObjects)
    return Invalid();
  if (route.kind == AgentSessionRouteKind::kArtifacts)
    return List(objects, access, session, *links, query);
  const std::string id(route.artifact);
  int64_t revision = 0;
  auto item = Metadata(objects, access, session, id, &revision);
  if (!item.ok()) return item;
  if (route.kind == AgentSessionRouteKind::kArtifactContent) {
    BinaryStore binary(objects);
    BinaryRead read;
    const BinaryAccess source{access.scope, access.deadline, access.cancelled};
    auto result = binary.Open(source, ObjectKind::kToolRun, id, 3600, &read);
    if (!result.ok()) return result;
    *sent = true;
    SendPinnedBinary(fd, binary, source, read);
    return result;
  }
  if (method == "DELETE") {
    auto erase = Lookup(access, id);
    erase.operation = ObjectOperation::kErase;
    erase.expected_revision = revision;
    auto result = objects.Execute(erase, access.deadline, access.cancelled);
    if (result.ok())
      result.value =
          Object({{"id", String(id)},
                  {"deleted", Boolean(true)},
                  {"object", String("agent.session.artifact.deleted")}});
    return result;
  }
  return item;
}
}  // namespace q4t::server::agent_detail
