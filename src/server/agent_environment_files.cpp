#include "agent_environment_internal.h"

#include <algorithm>
#include <filesystem>
#include <ctime>

#include "agent_session_internal.h"
#include "object_protocol.h"
#include "q4t/server/application_query.h"

namespace q4t::server::environment_detail {
namespace {
using namespace object_detail;
ObjectReply Bad(std::string message, std::string field) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(field))}});
  return reply;
}
Json Entry(const std::string& id, const Json& file) {
  return Object({{"object", String("agent.environment.file")},
                 {"environment_id", String(id)},
                 {"path", *file.Find("path")},
                 {"size_bytes", *file.Find("size_bytes")}});
}
ObjectReply FilesPage(const std::string& id, const Json& files,
                      const Json& query) {
  ChatPlanError error;
  auto valid = ValidateEnvironment(query, EnvironmentShape::kFileQuery, &error);
  if (!valid) return Bad(valid.message(), error.param);
  const auto limit = query.GetInt("limit", 20);
  if (limit < 1 || limit > 100)
    return Bad("Expected a limit between 1 and 100", "limit");
  auto dir = query.GetString("path", "/workspace");
  if (dir.ends_with('/')) dir.pop_back();
  if (dir != "/workspace" && !ValidWorkspaceFile(dir))
    return Bad("Expected a directory inside /workspace", "path");
  const bool descending = query.GetString("order", "desc") == "desc";
  const auto page = query.GetString("page");
  const auto prefix = dir + '/';
  std::vector<const Json*> items;
  for (const auto& file : files.array) {
    if (file.GetString("path").starts_with(prefix)) items.push_back(&file);
  }
  std::ranges::sort(items, [&](const Json* a, const Json* b) {
    const auto order = std::filesystem::path(a->GetString("path"))
                           .compare(b->GetString("path"));
    return descending ? order > 0 : order < 0;
  });
  const auto cursor = [&](const Json& file) {
    return Digest(
        OutputJson(Array({String(id), String(dir), Boolean(descending),
                          Number(limit), *file.Find("path")})));
  };
  size_t pos = 0;
  if (query.Has("page")) {
    const auto after = std::ranges::find_if(
        items, [&](const Json* item) { return cursor(*item) == page; });
    if (after == items.end())
      return Bad("Invalid environment file page", "page");
    pos = static_cast<size_t>(after - items.begin()) + 1;
  }
  auto data = Array();
  while (pos < items.size() && data.array.size() < static_cast<size_t>(limit))
    data.array.push_back(Entry(id, *items[pos++]));
  const bool more = pos < items.size();
  ObjectReply result;
  result.value =
      Object({{"object", String("list")},
              {"data", std::move(data)},
              {"has_more", Boolean(more)},
              {"next", more ? String(cursor(*items[pos - 1])) : Json{}}});
  return result;
}
void Install(Json* list, Json file) {
  const auto path = file.GetString("path");
  const auto existing = std::ranges::find_if(list->array, [&](const Json& row) {
    return row.GetString("path") == path;
  });
  if (existing != list->array.end())
    *existing = std::move(file);
  else
    list->array.push_back(std::move(file));
}
}  // namespace

ObjectReply RefreshWorkspaceFiles(ObjectStore& objects,
                                  const StateAccess& access, const Json& record,
                                  Json* session) {
  if (!record.GetBool("shared_environment")) return {};
  auto* environment = session->Find("environment");
  if (!environment || environment->GetString("type") != "openai_hosted")
    return Error(503, "environment_record_invalid",
                 "Invalid shared environment");
  ObjectRequest get;
  get.kind = ObjectKind::kEnvironment;
  get.scope = access.scope;
  get.id = environment->GetString("id");
  auto current = objects.Execute(get, access.deadline, access.cancelled);
  if (!current.ok()) return current;
  const auto& data = Field(current.value, "data");
  const auto* resource = data.Find("resource");
  const auto* files = resource ? resource->GetArray("files") : nullptr;
  if (!files || !HasAttachedSession(data, session->GetString("id")))
    return Error(503, "environment_record_invalid",
                 "Shared workspace binding changed");
  Set(&Field(*session, "environment"), "files", *files);
  return {};
}

ObjectReply WorkspaceFiles(ObjectStore& objects, const StateAccess& access,
                           std::string_view method, std::string_view target,
                           const Json& input, const Json& stored,
                           const Json& resource) {
  const auto id = resource.GetString("id");
  if (resource.GetString("status") != "connected")
    return Error(409, "environment_not_connected",
                 "Environment has no connected session");
  if (method == "GET") {
    const auto split = target.find('?');
    auto parsed = ParseApplicationQuery(
        split == target.npos ? "" : target.substr(split + 1));
    return parsed.ok()
               ? FilesPage(id, *resource.GetArray("files"), parsed.value)
               : parsed;
  }
  const auto* record = stored.Find("data");
  if (!record)
    return Error(503, "environment_record_invalid", "Missing workspace");
  const bool shared = record->Has("attached_sessions");
  ObjectReply session;
  AgentSessionView state;
  const auto owner = record->GetString("bound_session");
  if (!shared) {
    if (owner.empty())
      return Error(503, "environment_record_invalid",
                   "Missing workspace owner");
    session = agent_detail::ReadRow(objects, owner, access);
    if (!session.ok()) return session;
    auto projected =
        agent_detail::ProjectHistory(objects, Field(session.value, "data"),
                                     owner, "", access, &state, false);
    if (!projected.ok()) return projected;
    const auto* bound = state.session.Find("environment");
    if (!bound || bound->GetString("id") != id)
      return Error(503, "environment_record_invalid",
                   "Workspace owner changed");
    if (state.session.GetString("status") != "idle")
      return Error(409, "environment_turn_active",
                   "Workspace files are available between turns");
  }
  ChatPlanError error;
  auto shape =
      ValidateEnvironment(input, EnvironmentShape::kFileCreate, &error);
  if (!shape) return Bad(shape.message(), error.param);
  auto single = Object({{"files", Array({input})}});
  shape = ValidateEnvironmentTemplate(single, EnvironmentTemplateShape::kCreate,
                                      &error);
  if (!shape) return Bad(shape.message(), error.param);
  auto projection = PublicTemplate(single, id, 0, 0);
  if (!projection.ok()) return projection;
  auto data = *stored.Find("data");
  const auto* sealed = data.Find("sealed_definition");
  if (!sealed)
    return Error(503, "environment_record_invalid",
                 "Missing workspace definition");
  Json definition;
  auto opened = OpenDefinition(objects, access, id, *sealed, &definition);
  if (!opened.ok()) return opened;
  WorkspaceCopies copies(objects, access, id);
  Json installed, content;
  auto copy =
      SnapshotFile(input, copies, "file_id", &installed, &content);
  if (!copy.ok()) return copy;
  Install(&Field(definition, "files"), std::move(content));
  auto checked = PublicTemplate(definition, id, 0, 0);
  if (!checked.ok()) return checked;
  Json encoded;
  auto secured = SealDefinition(objects, access, id, definition, &encoded);
  if (!secured.ok()) return secured;
  auto view = Entry(id, installed);
  copies.Bind(&data, input.GetString("path"));
  Set(&data, "sealed_definition", std::move(encoded));
  Install(&Field(Field(data, "resource"), "files"), std::move(installed));
  ObjectRequest update;
  update.kind = ObjectKind::kEnvironment;
  update.operation = ObjectOperation::kPut;
  update.scope = access.scope;
  update.id = id;
  update.expected_revision = stored.GetInt("revision");
  update.expires_at = stored.GetInt("expires_at");
  update.data = std::move(data);
  ObjectReply result;
  if (shared) {
    Set(&update.data, "last_activity_at", Number(std::time(nullptr)));
    result = objects.Execute(update, access.deadline, access.cancelled);
  } else {
    auto& session_data = Field(session.value, "data");
    Set(&Field(state.session, "environment"), "files",
        *Field(update.data, "resource").Find("files"));
    auto bound_settings =
        agent_detail::BindSessionSettings(&session_data, state.session);
    if (!bound_settings.ok()) return bound_settings;
    ObjectRequest owner_write;
    owner_write.operation = ObjectOperation::kPut;
    owner_write.kind = ObjectKind::kAgentSession;
    owner_write.scope = access.scope;
    owner_write.id = owner;
    owner_write.expected_revision = session.value.GetInt("revision");
    owner_write.ttl_seconds = 0;
    owner_write.data = std::move(session_data);
    const ObjectRequest writes[]{std::move(update), std::move(owner_write)};
    result = objects.ExecuteBatch(writes, access.deadline, access.cancelled);
  }
  copies.Settle(result);
  if (result.ok()) {
    result.http_status = 201;
    result.value = std::move(view);
  } else if (result.commit_unknown) {
    result.value = Object({{"id", String(id)}});
  }
  return result;
}
}  // namespace q4t::server::environment_detail
