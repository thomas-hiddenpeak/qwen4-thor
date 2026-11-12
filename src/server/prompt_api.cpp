#include "q4t/server/prompt_registry.h"

#include "prompt_internal.h"
#include "q4t/server/application_query.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
namespace pd = prompt_detail;
ObjectReply Method(std::string allow) {
  auto result = Error(405, "method_not_allowed", "Method not allowed");
  result.value = Object({{"allow", String(std::move(allow))}});
  return result;
}
ObjectReply Fields(const Json& value,
                   std::initializer_list<std::string_view> allowed) {
  std::string path;
  auto status = InputFields(value, allowed, &path);
  return status ? ObjectReply() : pd::Invalid(status.message(), path);
}
}  // namespace

ObjectReply HandlePromptRequest(ObjectStore& objects, std::string_view method,
                                std::string_view target,
                                const std::string& body,
                                const ChatPlanConfig& config,
                                const StateAccess& access) {
  constexpr std::string_view root = "/v1/prompts";
  if (!IsPromptTarget(target) || target.find('#') != target.npos)
    return Error(404, "route_not_found", "Prompt route not found");
  const auto split = target.find('?');
  auto path = target.substr(0, split);
  std::string id, version;
  bool versions = false;
  const bool collection = path == root;
  if (!collection) {
    path.remove_prefix(root.size() + 1);
    const auto slash = path.find('/');
    id = path.substr(0, slash);
    if (!ValidId(id)) return pd::Invalid("Invalid prompt ID", "prompt.id");
    if (slash != path.npos) {
      path.remove_prefix(slash);
      if (path == "/versions")
        versions = true;
      else if (path.starts_with("/versions/")) {
        version = path.substr(10);
        if (!pd::Version(version))
          return pd::Invalid("Invalid version", "version");
      } else
        return Error(404, "route_not_found", "Prompt route not found");
    }
  }
  if (collection && method != "GET" && method != "POST")
    return Method("GET, POST");
  if (versions && method != "POST") return Method("POST");
  if (!version.empty() && method != "GET") return Method("GET");
  if (!collection && !versions && version.empty() && method != "GET" &&
      method != "POST" && method != "DELETE")
    return Method("GET, POST, DELETE");
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  if (!(collection && method == "GET") && !query.value.object.empty())
    return pd::Invalid("Query not allowed", "query");
  if (method != "POST" && !body.empty())
    return pd::Invalid("Body not allowed", "body");
  PromptRegistry registry(objects);
  if (collection && method == "GET") {
    auto fields = Fields(query.value, {"limit", "after", "before", "order"});
    if (!fields.ok()) return fields;
    ObjectRequest page;
    if (const auto* limit = query.value.Find("limit")) {
      if (!IsInputInteger(*limit) || limit->number < 1 || limit->number > 100)
        return pd::Invalid("Invalid page limit", "limit");
      page.limit = limit->AsInt();
    }
    page.after = query.value.GetString("after");
    page.before = query.value.GetString("before");
    const auto order = query.value.GetString("order", "desc");
    if (order != "asc" && order != "desc")
      return pd::Invalid("Invalid order", "order");
    page.descending = order == "desc";
    return registry.List(std::move(page), access);
  }
  if (method == "GET") return registry.Retrieve(id, version, access);
  if (method == "DELETE") return registry.Erase(id, access);
  Json request;
  auto parsed = io::ParseJson(body, &request, true, kSchemaJsonLimits);
  if (!parsed) return pd::Invalid(parsed.message(), "body");
  auto fields = collection ? Fields(request, {"definition"})
                : versions
                    ? Fields(request, {"definition", "expected_revision"})
                    : Fields(request, {"default_version", "expected_revision"});
  if (!fields.ok()) return fields;
  if (collection || versions) {
    const auto* definition = request.Find("definition");
    if (!definition) return pd::Invalid("Definition required", "definition");
    if (collection) return registry.Create(*definition, config, access);
  }
  const auto* revision = request.Find("expected_revision");
  if (!revision || !IsInputInteger(*revision) || revision->number < 1 ||
      revision->number > 9007199254740991LL)
    return pd::Invalid("Positive expected revision required",
                       "expected_revision");
  if (versions)
    return registry.Append(id, revision->AsInt(), *request.Find("definition"),
                           config, access);
  const auto* selected = request.Find("default_version");
  if (!selected || !selected->IsString())
    return pd::Invalid("Default version required", "default_version");
  return registry.Select(id, revision->AsInt(), selected->str, access);
}
}  // namespace q4t::server
