#include "q4t/server/agent_environment.h"

#include <ctime>
#include <set>

#include "agent_environment_internal.h"
#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/application_query.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/files.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;

ObjectReply Invalid(std::string message, std::string key) {
  auto out = Error(400, "invalid_request", std::move(message));
  out.value = Object({{"param", String(std::move(key))}});
  return out;
}

ObjectReply InvalidRecord() {
  return Error(503, "environment_template_invalid",
               "Invalid stored environment template");
}

Json Or(const Json& source, const char* key, Json fallback) {
  const auto* value = source.Find(key);
  return value && !value->IsNull() ? *value : std::move(fallback);
}


}  // namespace

bool environment_detail::ValidWorkspaceFile(std::string_view path) {
  if (!path.starts_with("/workspace/") || path.ends_with('/') ||
      path.find('\0') != path.npos)
    return false;
  path.remove_prefix(11);
  while (!path.empty()) {
    const auto cut = path.find('/');
    const auto part = path.substr(0, cut);
    if (part.empty() || part == "." || part == "..") return false;
    if (cut == path.npos) break;
    path.remove_prefix(cut + 1);
  }
  return true;
}

ObjectReply environment_detail::PublicTemplate(const Json& config,
                                              const std::string& id,
                                              int64_t created, int64_t updated) {
  auto files = Or(config, "files", Array());
  std::set<std::string> paths;
  for (auto& file : files.array) {
    const auto path = file.GetString("path");
    if (!ValidWorkspaceFile(path) || !paths.insert(path).second)
      return Invalid("Expected a unique file path inside /workspace", "files");
    if (file.GetString("type") == "inline") {
      size_t size;
      if (!io::Base64DecodedSize(file.Find("data")->str, &size))
        return Invalid("Invalid inline file data", "files");
      file = Object({{"type", String("inline")},
                     {"path", String(path)},
                     {"size_bytes", Number(size)}});
    }
  }
  // A regular file cannot also be an ancestor directory. Check both orders
  // after collecting all paths so replacing a whole file list is atomic.
  for (const auto& path : paths) {
    const std::string prefix = path + '/';
    const auto child = paths.lower_bound(prefix);
    if (child != paths.end() && child->starts_with(prefix))
      return Invalid("A workspace file is also used as a directory", "files");
  }
  auto plugins = Or(config, "plugins", Array());
  auto skills = Or(config, "skills", Array());
  for (auto* entries : {&plugins, &skills}) {
    for (auto& entry : entries->array) {
      if (entry.GetString("type") == "skill_reference") {
        if (!entry.Has("version")) Set(&entry, "version", {});
      } else {
        size_t size;
        if (!io::Base64DecodedSize(entry.Find("source")->Find("data")->str, &size) ||
            !size)
          return Invalid("Invalid inline capability data",
                         entries == &plugins ? "plugins" : "skills");
        std::erase_if(entry.object,
                      [](const auto& pair) { return pair.first == "source"; });
      }
    }
  }
  auto packages = Or(config, "packages", Object());
  for (const auto* key : {"npm", "python", "system"})
    if (!Nonnull(packages, key)) Set(&packages, key, Array());
  const auto network = Or(config, "network", Object());
  const auto* blocked = network.GetArray("blocked_domains");
  if (blocked && !blocked->array.empty()) {
    const auto* allowed = network.GetArray("allowed_domains");
    if (network.GetString("access") != "restricted" ||
        (allowed && !allowed->array.empty()))
      return Invalid(
          "Blocked domains require restricted access and an empty "
          "allowlist",
          "network.blocked_domains");
    for (const auto& domain : blocked->array) {
      FileFetchDomainPolicy policy;
      policy.blocked.push_back(http::Lower(domain.str));
      if (!file_detail::ValidDomains(policy))
        return Invalid("Expected an exact domain", "network.blocked_domains");
    }
  }
  ObjectReply result;
  result.value = Object(
      {{"id", String(id)},
       {"object", String("agent.environment.template")},
       {"created_at", Number(created)},
       {"updated_at", Number(updated)},
       {"name", Or(config, "name", {})},
       {"files", std::move(files)},
       {"network",
        Object({{"access", Or(network, "access", String("disabled"))},
                {"allowed_domains", Or(network, "allowed_domains", Array())}})},
       {"packages", std::move(packages)},
       {"skills", std::move(skills)},
       {"plugins", std::move(plugins)},
       {"capability_directories",
        Or(config, "capability_directories", Array())},
       {"desktop",
        Or(config, "desktop", Object({{"enabled", Boolean(false)}}))}});
  ChatPlanError error;
  if (!ValidateEnvironmentTemplate(result.value,
                                   EnvironmentTemplateShape::kResource, &error))
    return InvalidRecord();
  return result;
}

namespace {
ObjectReply Public(const Json& data, const std::string& id) {
  const auto* row = data.Find("resource");
  ChatPlanError error;
  if (!row || row->GetString("id") != id ||
      !ValidateEnvironmentTemplate(*row, EnvironmentTemplateShape::kResource,
                                   &error))
    return InvalidRecord();
  ObjectReply out;
  out.value = *row;
  return out;
}
}  // namespace

ObjectReply HandleEnvironmentTemplate(ObjectStore& objects,
                                      std::string_view method,
                                      std::string_view target,
                                      const std::string& body,
                                      const StateAccess& access) {
  using namespace object_detail;
  if (!EnvironmentTemplateMethodAllowed(method, target))
    return Error(404, "route_not_found", "No environment template route");
  const auto split = target.find('?');
  const auto path = target.substr(0, split);
  const bool collection = path == kEnvironmentTemplatesPath;
  if (method != "POST" && !body.empty())
    return Invalid("Unexpected body", "body");
  ObjectRequest req;
  req.kind = ObjectKind::kEnvironmentTemplate;
  req.scope = access.scope;
  const auto execute = [&] {
    return objects.Execute(req, access.deadline, access.cancelled);
  };
  if (collection && method == "GET") {
    auto query = ParseApplicationQuery(
        split == target.npos ? "" : target.substr(split + 1));
    if (!query.ok()) return query;
    if (query.value.Has("include"))
      return Invalid("Unknown list option", "include");
    ItemViewOptions page;
    ChatPlanError error;
    auto parsed = ParseItemViewOptions(query.value, &page, &error, true);
    if (!parsed) return Invalid(parsed.message(), error.param);
    req.operation = ObjectOperation::kList;
    req.after = page.after;
    req.limit = page.limit;
    req.descending = page.descending;
    auto list = execute();
    if (!list.ok()) return list;
    for (auto& row : Field(list.value, "data").array) {
      auto projected = Public(Field(row, "data"), row.GetString("id"));
      if (!projected.ok()) return projected;
      row = std::move(projected.value);
    }
    return list;
  }
  if (split != target.npos) return Invalid("Unexpected query", "query");
  Json changes = Object();
  if (method == "POST") {
    if (!body.empty() &&
        !io::ParseJson(body, &changes, true, kApplicationJsonLimits))
      return Invalid("Expected a JSON template object", "body");
    if (collection && changes.IsNull()) changes = Object();
    ChatPlanError error;
    auto valid = ValidateEnvironmentTemplate(
        changes,
        collection ? EnvironmentTemplateShape::kCreate
                   : EnvironmentTemplateShape::kUpdate,
        &error);
    if (!valid) return Invalid(valid.message(), error.param);
  }
  req.id = collection
               ? NewObjectId(req.kind)
               : std::string(path.substr(kEnvironmentTemplatesPath.size() + 1));
  if (req.id.empty())
    return Error(503, "environment_id_unavailable", "No template ID");
  const auto now = std::time(nullptr);
  int64_t created = now;
  Json config = Object();
  req.expected_revision = 0;
  if (!collection) {
    auto old = execute();
    if (!old.ok()) return old;
    const auto& data = Field(old.value, "data");
    auto resource = Public(data, req.id);
    if (!resource.ok() || method == "GET") return resource;
    req.expected_revision = old.value.GetInt("revision");
    if (method == "DELETE") {
      req.operation = ObjectOperation::kErase;
      auto erased = execute();
      if (erased.ok())
        erased.value =
            Object({{"id", String(req.id)},
                    {"deleted", Boolean(true)},
                    {"object", String("agent.environment.template.deleted")}});
      return erased;
    }
    created = resource.value.GetInt("created_at");
    const auto* sealed = data.Find("sealed_definition");
    if (!sealed) return InvalidRecord();
    auto opened = environment_detail::OpenDefinition(objects, access, req.id,
                                                      *sealed, &config);
    if (!opened.ok()) return opened;
  }
  if (const auto* files = changes.GetArray("files")) {
    Files owned(objects);
    for (const auto& file : files->array) {
      if (file.GetString("type") != "file_id") continue;
      auto found =
          owned.Retrieve({access.scope, access.deadline, access.cancelled},
                         file.GetString("file_id"));
      if (!found.ok()) return found;
    }
  }
  for (auto& [key, value] : changes.object) Set(&config, key, std::move(value));
  auto projected = environment_detail::PublicTemplate(config, req.id, created, now);
  if (!projected.ok()) return projected;
  Json sealed;
  auto saved =
      environment_detail::SealDefinition(objects, access, req.id, config, &sealed);
  if (!saved.ok()) return saved;
  req.data = Object({{"resource", projected.value},
                     {"sealed_definition", std::move(sealed)}});
  req.ttl_seconds = 0;
  req.operation = ObjectOperation::kPut;
  auto committed = execute();
  if (committed.commit_unknown)
    committed.value = Object({{"id", String(req.id)}});
  if (!committed.ok()) return committed;
  if (collection) projected.http_status = 201;
  return projected;
}
}  // namespace q4t::server
