#include "q4t/server/skills.h"

#include <algorithm>
#include <charconv>
#include <ctime>
#include <set>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Kind = SkillRouteKind;
ObjectReply Invalid(std::string field, std::string message) {
  auto r = Error(400, "invalid_request", std::move(message));
  r.value = Object({{"param", String(std::move(field))}});
  return r;
}
ObjectReply Missing() {
  return Error(404, "object_not_found", "Skill or version not found");
}
ObjectReply Corrupt() {
  return Error(503, "skill_state_invalid", "Invalid stored skill");
}
BinaryAccess Source(const StateAccess& access) {
  return {access.scope, access.deadline, access.cancelled};
}
ObjectRequest Skill(const StateAccess& access, std::string_view id) {
  ObjectRequest q;
  q.kind = ObjectKind::kSkill;
  q.id = id;
  q.scope = access.scope;
  q.ttl_seconds = 0;
  return q;
}
bool SameVersion(const Json& a, const Json& b) {
  for (const auto* key : {"id", "skill_id", "version", "name", "description"})
    if (a.GetString(key) != b.GetString(key)) return false;
  return a.GetInt("created_at") == b.GetInt("created_at");
}
bool ValidIndex(const Json& row) {
  const auto* data = row.Find("data");
  const auto* info = data ? data->Find("resource") : nullptr;
  const auto* versions = data ? data->GetArray("versions") : nullptr;
  ChatPlanError error;
  if (!info || !versions || versions->array.empty() ||
      data->GetString("format") != "q4t.skill.v1" ||
      !ValidateSkill(*info, SkillShape::kResource, &error) ||
      info->GetString("id") != row.GetString("id") ||
      info->GetInt("created_at") <= 0 ||
      info->GetInt("created_at") != row.GetInt("created_at"))
    return false;
  bool found_default = false;
  std::set<std::string> ids;
  int64_t previous = 0;
  for (const auto& version : versions->array) {
    const auto number = version.GetString("version");
    const auto id = version.GetString("id");
    int64_t index = 0;
    const auto parsed =
        std::from_chars(number.data(), number.data() + number.size(), index);
    if (parsed.ec != std::errc{} ||
        parsed.ptr != number.data() + number.size() ||
        std::to_string(index) != number || index <= previous ||
        !ValidateSkill(version, SkillShape::kVersion, &error) ||
        !id.starts_with("skillver_") || !ValidId(id) ||
        !ids.insert(id).second ||
        version.GetString("skill_id") != info->GetString("id") ||
        version.GetString("name").empty() ||
        version.GetString("description").empty() ||
        version.GetInt("created_at") <= 0)
      return false;
    if (number == info->GetString("default_version"))
      found_default =
          version.GetString("name") == info->GetString("name") &&
          version.GetString("description") == info->GetString("description");
    previous = index;
  }
  return found_default &&
         info->GetString("latest_version") == std::to_string(previous) &&
         data->GetInt("next_version") > previous;
}
ObjectReply Read(ObjectStore& objects, const StateAccess& access,
                 ObjectRequest* q) {
  auto row = objects.Execute(*q, access.deadline, access.cancelled);
  if (!row.ok()) return row;
  if (row.value.GetString("id") != q->id || !ValidIndex(row.value))
    return Corrupt();
  q->expected_revision = row.value.GetInt("revision");
  q->data = *row.value.Find("data");
  ObjectReply reply;
  reply.value = *q->data.Find("resource");
  return reply;
}
const Json* Version(const ObjectRequest& q, std::string_view number) {
  const auto& info = *q.data.Find("resource");
  std::string resolved = number.empty() ? info.GetString("default_version")
                         : number == "latest" ? info.GetString("latest_version")
                                              : std::string(number);
  const auto& versions = q.data.GetArray("versions")->array;
  const auto r = std::ranges::find_if(versions, [&](const Json& v) {
    return v.GetString("version") == resolved;
  });
  return r == versions.end() ? nullptr : &*r;
}
void Default(Json* resource, const Json& version) {
  Set(resource, "default_version", *version.Find("version"));
  Set(resource, "name", *version.Find("name"));
  Set(resource, "description", *version.Find("description"));
}
ObjectReply Save(ObjectStore& objects, const StateAccess& access,
                 ObjectRequest q, Json response) {
  q.operation = ObjectOperation::kPut;
  auto r = objects.Execute(q, access.deadline, access.cancelled);
  if (r.ok()) r.value = std::move(response);
  return r;
}

ObjectReply Publish(ObjectStore& objects, const StateAccess& access,
                    const SkillRoute& route, const SkillPackage& package,
                    bool make_default) {
  if (package.archive.empty() || package.name.empty() ||
      package.description.empty())
    return Invalid("files", "Expected a validated skill package");
  const bool create = route.kind == Kind::kCollection;
  const auto id =
      create ? NewObjectId(ObjectKind::kSkill) : std::string(route.skill);
  const auto version_id = NewObjectId(ObjectKind::kSkillVersion);
  if (id.empty() || version_id.empty())
    return Error(503, "object_entropy_unavailable", "Skill ID unavailable");
  ObjectRequest q = Skill(access, id);
  if (!create) {
    auto row = Read(objects, access, &q);
    if (!row.ok()) return row;
  }
  BinaryStore blobs(objects);
  BinaryStage stage;
  const auto owner = Source(access);
  auto started = blobs.Begin(owner, 50'000'000, 3600, &stage);
  if (!started.ok()) return started;
  ScopeExit cleanup([&] {
    if (stage.closed || stage.commit_unknown) return;
    const BinaryAccess release{
        access.scope,
        std::chrono::steady_clock::now() + std::chrono::seconds(1),
        {}};
    try {
      if (!blobs.Abort(release, &stage).ok())
        objects.RecordAuxiliaryCleanupFailure();
    } catch (...) {
      objects.RecordAuxiliaryCleanupFailure();
    }
  });
  for (std::string_view bytes(package.archive); !bytes.empty();) {
    const auto block = bytes.substr(0, kBinaryChunkBytes);
    auto appended = blobs.Append(owner, &stage, block);
    if (!appended.ok()) return appended;
    bytes.remove_prefix(block.size());
  }
  // Retry only a known parent CAS conflict. Staging and the immutable version
  // ID are private until the same transaction advances the owner index.
  for (;;) {
    const auto now = std::time(nullptr);
    if (create) {
      q.created_at = now;
      q.data = Object({{"format", String("q4t.skill.v1")},
                       {"next_version", Number(1)},
                       {"resource", Object({{"id", String(id)},
                                            {"object", String("skill")},
                                            {"created_at", Number(now)}})},
                       {"versions", Array()}});
    }
    const auto number = q.data.GetInt("next_version");
    if (number < 1 || number >= 9007199254740991LL) return Corrupt();
    const auto label = String(std::to_string(number));
    const auto version = Object({{"object", String("skill.version")},
                                 {"id", String(version_id)},
                                 {"skill_id", String(id)},
                                 {"version", label},
                                 {"created_at", Number(now)},
                                 {"name", String(package.name)},
                                 {"description", String(package.description)}});
    auto& info = Field(q.data, "resource");
    Set(&info, "latest_version", label);
    if (create || make_default) Default(&info, version);
    auto response = create ? info : version;
    Field(q.data, "versions").array.push_back(version);
    Set(&q.data, "next_version", Number(number + 1));
    q.operation = ObjectOperation::kPut;
    ObjectRequest v;
    v.kind = ObjectKind::kSkillVersion;
    v.id = version_id;
    v.scope = access.scope;
    v.created_at = now;
    v.ttl_seconds = 0;
    v.operation = ObjectOperation::kPut;
    v.data = Object(
        {{"format", String("q4t.skill_version.v1")}, {"resource", version}});
    auto result = blobs.Publish(owner, &stage, std::move(v), std::span(&q, 1));
    if (result.ok()) result.value = std::move(response);
    if (result.commit_unknown)
      result.value = Object({{"id", String(id)},
                             {"version_id", String(version_id)},
                             {"version", label}});
    if (create || result.commit_unknown ||
        result.code != "object_revision_conflict" ||
        std::chrono::steady_clock::now() >= access.deadline ||
        (access.cancelled && access.cancelled()))
      return result;
    q = Skill(access, id);
    auto current = Read(objects, access, &q);
    if (!current.ok()) return current;
  }
}
ObjectReply List(ObjectStore& objects, const StateAccess& access,
                 ObjectRequest q, const Json& query, bool collection) {
  ChatPlanError error;
  auto valid = ValidateSkill(
      query, collection ? SkillShape::kQuery : SkillShape::kVersionQuery,
      &error);
  if (!valid) return Invalid(error.param, valid.message());
  const auto limit = query.GetInt("limit", 20);
  const auto after = query.GetString("after");
  const auto order = query.GetString("order", "desc");
  if (limit < 0 || limit > 100 ||
      (query.Has("limit") && !query.Find("limit")->IsNumber()))
    return Invalid("limit", "Expected 0 to 100 entries");
  if (order != "asc" && order != "desc")
    return Invalid("order", "Expected asc or desc");
  if (query.Has("after") &&
      (!ValidId(after) ||
       !after.starts_with(collection ? "skill_" : "skillver_")))
    return Invalid("after", "Invalid skill cursor");
  if (collection) {
    q.operation = ObjectOperation::kList;
    q.limit = std::max<int64_t>(limit, 1);
    q.after = after;
    q.descending = order == "desc";
    auto result = objects.Execute(q, access.deadline, access.cancelled);
    if (result.code == "invalid_object_cursor") return Missing();
    if (!result.ok()) return result;
    auto& rows = Field(result.value, "data").array;
    if (limit == 0) {
      const bool more = !rows.empty();
      rows.clear();
      Set(&result.value, "first_id", Json{});
      Set(&result.value, "last_id", Json{});
      Set(&result.value, "has_more", Boolean(more));
    }
    for (auto& row : rows) {
      if (!ValidIndex(row)) return Corrupt();
      auto resource = *row.Find("data")->Find("resource");
      row = std::move(resource);
    }
    return result;
  }
  auto rows = q.data.GetArray("versions")->array;
  if (order == "desc") std::ranges::reverse(rows);
  auto iter = rows.begin();
  if (!after.empty()) {
    iter = std::ranges::find_if(
        rows, [&](const Json& row) { return row.GetString("id") == after; });
    if (iter == rows.end()) {
      ObjectRequest cursor;
      cursor.scope = access.scope;
      cursor.kind = ObjectKind::kSkillVersion;
      cursor.id = after;
      auto record = objects.Execute(cursor, access.deadline, access.cancelled);
      if (!record.ok()) return record;
      const auto* data = record.value.Find("data");
      const auto* version = data ? data->Find("resource") : nullptr;
      if (!version || data->GetString("format") != "q4t.skill_version.v1")
        return Corrupt();
      // A retired blob may remain pinned briefly. Only public membership
      // in another skill belonging to this owner is a mismatched cursor.
      auto other = Skill(access, version->GetString("skill_id"));
      auto live = Read(objects, access, &other);
      if (!live.ok()) return live;
      const auto* member = Version(other, version->GetString("version"));
      if (!member || member->GetString("id") != after) return Missing();
      return Invalid("after", "Version cursor belongs to a different skill");
    }
    ++iter;
  }
  auto result = Array();
  while (iter != rows.end() && result.array.size() < static_cast<size_t>(limit))
    result.array.push_back(*iter++);
  const auto first =
      result.array.empty() ? Json() : *result.array.front().Find("id");
  const auto last =
      result.array.empty() ? Json() : *result.array.back().Find("id");
  ObjectReply out;
  out.value = Object({{"object", String("list")},
                      {"data", std::move(result)},
                      {"first_id", first},
                      {"last_id", last},
                      {"has_more", Boolean(iter != rows.end())}});
  return out;
}
}  // namespace

ObjectReply OpenSkillVersion(ObjectStore& objects, const StateAccess& access,
                             std::string_view skill, std::string_view label,
                             BinaryRead* download) {
  auto q = Skill(access, skill);
  auto parent = Read(objects, access, &q);
  if (!parent.ok()) return parent;
  const auto* version = Version(q, label);
  if (!version) return Missing();
  BinaryStore blobs(objects);
  Json data;
  auto pin = blobs.Open(Source(access), ObjectKind::kSkillVersion,
                        version->GetString("id"), 3600, download, &data);
  if (pin.ok()) {
    const auto* resource = data.Find("resource");
    ChatPlanError error;
    if (data.GetString("format") != "q4t.skill_version.v1" || !resource ||
        !ValidateSkill(*resource, SkillShape::kVersion, &error) ||
        !SameVersion(*resource, *version)) {
      const BinaryAccess cleanup{
          access.scope,
          std::chrono::steady_clock::now() + std::chrono::seconds(1),
          {}};
      if (!blobs.Close(cleanup, download).ok())
        objects.RecordAuxiliaryCleanupFailure();
      return Corrupt();
    }
    pin.value = *version;
  }
  return pin;
}

ObjectReply HandleSkills(ObjectStore& objects, const StateAccess& access,
                         std::string_view method, std::string_view target,
                         const std::string& body, SkillPackage* package,
                         bool make_default, BinaryRead* download) {
  const auto route = ParseSkillRoute(target);
  if (route.kind == Kind::kNone) return Missing();
  if (!SkillMethodAllowed(method, target))
    return Error(405, "method_not_allowed", "Skill method not allowed");
  if (!route.version.empty() && !ValidSkillVersionNumber(route.version))
    return Invalid("version", "Expected a positive version number");
  const auto split = target.find('?');
  auto params = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!params.ok()) return params;
  const bool page = method == "GET" && (route.kind == Kind::kCollection ||
                                        route.kind == Kind::kVersions);
  if (!page && !params.value.object.empty())
    return Invalid("query", "Unexpected query");
  if (method != "POST" && !body.empty())
    return Invalid("body", "Unexpected body");
  if (IsSkillCreation(method, target)) {
    if (!package || !body.empty())
      return Invalid("files", "Expected skill upload");
    return Publish(objects, access, route, *package, make_default);
  }
  auto q = Skill(access, route.skill);
  if (route.kind == Kind::kCollection)
    return List(objects, access, q, params.value, true);
  if (route.kind == Kind::kContent || route.kind == Kind::kVersionContent)
    return OpenSkillVersion(objects, access, route.skill, route.version,
                            download);
  for (;;) {
    q = Skill(access, route.skill);
    auto parent = Read(objects, access, &q);
    if (!parent.ok()) return parent;
    if (page) return List(objects, access, q, params.value, false);
    const auto* v = Version(q, route.version);
    if (!v) return Missing();
    if (method == "GET") {
      if (route.kind == Kind::kVersion) parent.value = *v;
      return parent;
    }
    ObjectReply result;
    if (method == "POST") {
      Json settings;
      ChatPlanError error;
      if (!io::ParseJson(body, &settings, true, kApplicationJsonLimits) ||
          !ValidateSkill(settings, SkillShape::kUpdate, &error) ||
          !ValidSkillVersionNumber(settings.GetString("default_version")))
        return Invalid("default_version", "Expected a skill version number");
      const auto* selected = Version(q, settings.GetString("default_version"));
      if (!selected) return Missing();
      auto resource = parent.value;
      Default(&resource, *selected);
      Set(&q.data, "resource", resource);
      result = Save(objects, access, std::move(q), std::move(resource));
    } else {
      const bool version = route.kind == Kind::kVersion;
      auto response =
          Object({{"object",
                   String(version ? "skill.version.deleted" : "skill.deleted")},
                  {"id", String(version ? v->GetString("id") : q.id)},
                  {"deleted", Boolean(true)}});
      auto& rows = Field(q.data, "versions").array;
      if (version) Set(&response, "version", *v->Find("version"));
      if (!version || rows.size() == 1) {
        q.operation = ObjectOperation::kErase;
        result = objects.Execute(q, access.deadline, access.cancelled);
        if (result.ok()) result.value = std::move(response);
      } else {
        if (parent.value.GetString("default_version") == route.version)
          return Error(
              400, "skill_default_version",
              "Select a different default before deleting this version");
        std::erase_if(rows, [&](const Json& r) {
          return r.GetString("version") == route.version;
        });
        Set(&Field(q.data, "resource"), "latest_version",
            *rows.back().Find("version"));
        result = Save(objects, access, std::move(q), std::move(response));
      }
    }
    if (result.commit_unknown || result.code != "object_revision_conflict" ||
        std::chrono::steady_clock::now() >= access.deadline ||
        (access.cancelled && access.cancelled()))
      return result;
  }
}
}  // namespace q4t::server
