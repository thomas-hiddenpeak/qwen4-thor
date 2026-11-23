#include "agent_environment_internal.h"

#include <algorithm>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/http_framing.h"

namespace q4t::server::environment_detail {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string param) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}

struct Network {
  // An allowlist denotes precisely its members. A denylist denotes their
  // complement. Empty lists therefore also represent disabled and enabled.
  bool denylist = false;
  std::set<std::string> hosts;
};

ObjectReply Policy(const Json* value, Network* output) {
  const auto mode = value ? value->GetString("access", "disabled") : "disabled";
  if (mode == "disabled" || mode == "enabled") {
    output->denylist = mode == "enabled";
    return {};
  }
  const auto* allowed = value->GetArray("allowed_domains");
  const auto* blocked = value->GetArray("blocked_domains");
  output->denylist = blocked && !blocked->array.empty();
  if (output->denylist && allowed && !allowed->array.empty())
    return Invalid("Conflicting network domain lists", "environment.network");
  const auto* list = output->denylist ? blocked : allowed;
  if (!list) return {};
  for (const auto& domain : list->array) {
    FileFetchDomainPolicy exact;
    exact.allowed = {http::Lower(domain.str)};
    if (!file_detail::ValidDomains(exact))
      return Invalid("Expected an exact network host", "environment.network");
    output->hosts.insert(std::move(exact.allowed.front()));
  }
  return {};
}

bool AllowsOverride(const Network& base, const Network& child) {
  if (child.denylist)
    return base.denylist && std::ranges::includes(child.hosts, base.hosts);
  if (!base.denylist) return std::ranges::includes(base.hosts, child.hosts);
  return std::ranges::none_of(
      child.hosts, [&](const auto& host) { return base.hosts.contains(host); });
}
}  // namespace

ObjectReply ResolveDefinition(ObjectStore& objects, const StateAccess& access,
                              const Json& environment, Json* definition) {
  if (environment.GetString("type") != "openai_hosted")
    return Invalid("Expected a hosted environment", "environment.type");

  Json overrides = Object();
  for (const auto& [key, value] : environment.object) {
    if (key == "type" || key == "environment_template_id") continue;
    if (key == "name")
      return Invalid("Name belongs to a template", "environment.name");
    overrides.object.emplace_back(key, value);
  }
  ChatPlanError error;
  auto valid = ValidateEnvironmentTemplate(
      overrides, EnvironmentTemplateShape::kCreate, &error);
  if (!valid) return Invalid(valid.message(), "environment." + error.param);

  Json config = Object();
  const auto* id = environment.Find("environment_template_id");
  if (id) {
    if (!id->IsString() || !id->str.starts_with("environment_template_") ||
        !ValidId(id->str))
      return Error(404, "object_not_found", "Environment template not found");
    ObjectRequest req;
    req.scope = access.scope;
    req.kind = ObjectKind::kEnvironmentTemplate;
    req.id = id->str;
    auto record = objects.Execute(req, access.deadline, access.cancelled);
    if (!record.ok()) return record;
    const auto* data = record.value.Find("data");
    const auto* sealed = data ? data->Find("sealed_definition") : nullptr;
    if (!sealed)
      return Error(503, "environment_template_invalid",
                   "Missing template definition");
    auto result = OpenDefinition(objects, access, req.id, *sealed, &config);
    if (!result.ok()) return result;
    std::erase_if(config.object,
                  [](const auto& pair) { return pair.first == "name"; });
  }
  const auto* network = overrides.Find("network");
  Network inherited, effective;
  auto policy = Policy(config.Find("network"), &inherited);
  if (!policy.ok()) return policy;
  if (network && !network->IsNull()) {
    policy = Policy(network, &effective);
    if (!policy.ok()) return policy;
    if (id && !AllowsOverride(inherited, effective))
      return Invalid("Network override broadens the template policy",
                     "environment.network");
  }
  // Null setup settings inherit just like omission when instantiating; an
  // explicit empty list/map replaces the template setting.
  for (auto& [key, value] : overrides.object)
    if (!value.IsNull()) Set(&config, key, std::move(value));

  if (const auto* env = config.Find("env"); env && env->IsObject()) {
    for (const auto& [key, val] : env->object) {
      if (key == "PATH" || key == "OPENAI_API_KEY" ||
          key.starts_with("CODEX_") || key.empty() ||
          key.find('=') != key.npos || key.find('\0') != key.npos ||
          val.str.find('\0') != val.str.npos)
        return Invalid("Invalid or reserved environment variable",
                       "environment.env");
    }
  }
  auto projected = PublicTemplate(config, "environment_config", 0, 0);
  if (!projected.ok()) return projected;
  *definition = std::move(config);
  return projected;
}
}  // namespace q4t::server::environment_detail
