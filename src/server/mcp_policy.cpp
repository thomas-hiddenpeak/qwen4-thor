#include "q4t/server/mcp_policy.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/responses_mcp.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kPolicyBytes = 64 * 1024;
Status Invalid() { return Status::Fail("Invalid MCP deployment policy"); }
bool Text(const Json* value, size_t maximum) {
  return value && value->IsString() && !value->str.empty() &&
         value->str.size() <= maximum &&
         std::all_of(value->str.begin(), value->str.end(),
                     [](unsigned char c) { return c >= 0x21 && c <= 0x7e; });
}
}  // namespace

Status ParseMcpDeploymentPolicy(std::string_view raw,
                                McpDeploymentPolicy* output) {
  if (!output || raw.empty() || raw.size() > kPolicyBytes) return Invalid();
  Json value;
  std::string field;
  if (!io::ParseJson(std::string(raw), &value, true,
                     {.max_depth = 5,
                      .max_values = 16384,
                      .max_string_bytes = kPolicyBytes,
                      .reject_duplicate_keys = true}) ||
      !InputFields(value, {"version", "fetch", "targets"}, &field, "policy") ||
      !value.Find("version") || !value.Find("version")->IsNumber() ||
      value.Find("version")->str != "1")
    return Invalid();
  McpDeploymentPolicy next;
  next.fetch.max_redirects = 0;
  if (const auto* fetch = value.Find("fetch")) {
    auto network = *fetch;
    if (network.IsObject() && !network.Has("max_redirects"))
      Set(&network, "max_redirects", Number(0));
    if (!ParseFileFetchOptions(network, &next.fetch).ok() ||
        next.fetch.max_redirects != 0)
      return Invalid();
  }
  if (const auto* targets = value.Find("targets")) {
    if (!targets->IsArray() || targets->array.size() > 128) return Invalid();
    std::set<std::pair<std::string, std::string>> ids;
    for (const auto& item : targets->array) {
      if (!InputFields(item,
                       {"kind", "id", "url", "generation", "scopes",
                        "transport", "agents", "channel"},
                       &field, "target") ||
          !Text(item.Find("kind"), 32) || !Text(item.Find("id"), 256) ||
          !Text(item.Find("generation"), 128))
        return Invalid();
      const auto kind = item.GetString("kind");
      const auto id = item.GetString("id");
      if ((kind != "connector_id" && kind != "tunnel_id") ||
          !ids.emplace(kind, id).second)
        return Invalid();
      // Reuse the fixed public connector catalog and identifier contract.
      ResponsesMcpDefinition checked;
      ChatPlanError error;
      if (!PlanResponsesMcpDefinition(
              Object({{"type", String("mcp")},
                      {"server_label", String("deployment")},
                      {kind, String(id)}}),
              &checked, &error))
        return Invalid();
      McpDeploymentTarget target;
      target.kind = kind;
      target.id = id;
      target.generation = item.GetString("generation");
      target.transport = item.GetString("transport", "http");
      if (item.Has("transport") && !Text(item.Find("transport"), 32))
        return Invalid();
      if (target.transport == "http") {
        file_detail::Url url;
        if (!Text(item.Find("url"), 8192) || item.Has("agents") ||
            item.Has("channel") ||
            !file_detail::ParseUrl(item.GetString("url"), &url) ||
            (url.scheme == "http" && !next.fetch.allow_http))
          return Invalid();
        target.url = url.Text();
      } else if (target.transport == "native") {
        if (kind != "tunnel_id" || !ValidId(id) || item.Has("url"))
          return Invalid();
        const auto* agents = item.GetArray("agents");
        if (!agents || agents->array.empty() || agents->array.size() > 128)
          return Invalid();
        std::set<std::string> unique;
        for (const auto& agent : agents->array) {
          if (!Text(&agent, 512) || agent.str == "*" ||
              !unique.insert(agent.str).second)
            return Invalid();
          target.agents.push_back(agent.str);
        }
        if (item.Has("channel") && !Text(item.Find("channel"), 128))
          return Invalid();
        target.channel = item.GetString("channel", "main");
      } else {
        return Invalid();
      }
      const auto* scopes = item.GetArray("scopes");
      if (!scopes || scopes->array.empty() || scopes->array.size() > 128)
        return Invalid();
      std::set<std::string> unique;
      for (const auto& scope : scopes->array) {
        if (!Text(&scope, 512) || scope.str == "*" ||
            !unique.insert(scope.str).second)
          return Invalid();
        target.scopes.push_back(scope.str);
      }
      next.targets.push_back(std::move(target));
    }
  }
  next.sha256 = Digest(raw);
  *output = std::move(next);
  return {};
}

Status ReadMcpPrivateConfig(const std::string& path, std::string* output) {
  if (!output || path.empty() || path.size() > 4096 ||
      path.find('\0') != path.npos)
    return Invalid();
  const int fd =
      ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  if (fd < 0) return Invalid();
  struct Close {
    int fd;
    ~Close() { ::close(fd); }
  } close{fd};
  struct stat before {
  }, after{};
  if (::fstat(fd, &before) || !S_ISREG(before.st_mode) ||
      before.st_uid != ::geteuid() || (before.st_mode & 0077) ||
      before.st_nlink != 1 || before.st_size <= 0 ||
      static_cast<uint64_t>(before.st_size) > kPolicyBytes)
    return Invalid();
  std::string raw(static_cast<size_t>(before.st_size) + 1, '\0');
  size_t count = 0;
  while (count < raw.size()) {
    const auto got = ::read(fd, raw.data() + count, raw.size() - count);
    if (got < 0 && errno == EINTR) continue;
    if (got < 0) return Invalid();
    if (!got) break;
    count += static_cast<size_t>(got);
  }
  if (::fstat(fd, &after) || count != static_cast<size_t>(before.st_size) ||
      before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
      before.st_size != after.st_size || before.st_mode != after.st_mode ||
      before.st_uid != after.st_uid || before.st_nlink != after.st_nlink ||
      before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
      before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
      before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
      before.st_ctim.tv_nsec != after.st_ctim.tv_nsec)
    return Invalid();
  raw.resize(count);
  *output = std::move(raw);
  return {};
}

Status LoadMcpDeploymentPolicy(const std::string& path,
                               McpDeploymentPolicy* output) {
  if (!output) return Invalid();
  std::string raw;
  const auto status = ReadMcpPrivateConfig(path, &raw);
  return status ? ParseMcpDeploymentPolicy(raw, output) : status;
}

ObjectReply ResolveMcpDeploymentTarget(const McpDeploymentPolicy* policy,
                                       std::string_view scope,
                                       ResponsesMcpDefinition* definition) {
  if (!definition)
    return Error(500, "mcp_preparation_invalid", "Missing MCP definition");
  if (definition->target_kind == "server_url") return {};
  definition->credentials.url.clear();
  definition->credentials.tunnel_id.clear();
  definition->credentials.tunnel_generation.clear();
  definition->credentials.tunnel_channel = "main";
  definition->target_binding.clear();
  if (policy)
    for (const auto& target : policy->targets) {
      if (target.kind != definition->target_kind ||
          target.id != definition->target_id)
        continue;
      if (scope.empty() || std::find(target.scopes.begin(), target.scopes.end(),
                                     scope) == target.scopes.end())
        return Error(403, "mcp_target_denied",
                     "MCP route is outside the authenticated scope");
      auto identity = Object({{"kind", String(target.kind)},
                              {"id", String(target.id)},
                              {"url", String(target.url)},
                              {"generation", String(target.generation)}});
      // Preserve old HTTP route identities; changing transport or channel is
      // an actual target change, not a credential/policy rotation.
      if (target.transport == "native") {
        Set(&identity, "transport", String("native"));
        Set(&identity, "channel", String(target.channel));
      }
      std::string binding;
      if (!InputValueIdentity(identity, &binding))
        return Error(500, "mcp_preparation_invalid", "Invalid MCP route");
      definition->credentials.url = target.url;
      if (target.transport == "native") {
        definition->credentials.tunnel_id = target.id;
        definition->credentials.tunnel_generation = target.generation;
        definition->credentials.tunnel_channel = target.channel;
      }
      definition->target_binding = std::move(binding);
      return {};
    }
  return Error(503, "mcp_target_mapping_required",
               "MCP target has no configured deployment route");
}

std::vector<McpTunnelRoute> McpDeploymentTunnelRoutes(
    const McpDeploymentPolicy* policy) {
  std::vector<McpTunnelRoute> routes;
  if (policy)
    for (const auto& target : policy->targets)
      if (target.transport == "native")
        routes.push_back({target.id,
                          target.generation,
                          target.scopes,
                          target.agents,
                          {target.channel}});
  return routes;
}

Json McpDeploymentPolicyDescription(const McpDeploymentPolicy* policy) {
  return Object({{"profile", String("q4t-mcp-deployment-policy-v1")},
                 {"configured", Boolean(policy && !policy->sha256.empty())},
                 {"sha256", policy ? String(policy->sha256) : Json()},
                 {"target_count", Number(policy ? policy->targets.size() : 0)},
                 {"native_tunnel_count",
                  Number(McpDeploymentTunnelRoutes(policy).size())},
                 {"request_override", Boolean(false)},
                 {"redirects", Boolean(false)}});
}

int RunMcpDeploymentPolicyCheck(int argc, char** argv) {
  if (argc != 3) {
    std::fprintf(stderr, "Usage: q4t mcp-policy-check FILE\n");
    return 2;
  }
  McpDeploymentPolicy policy;
  if (!LoadMcpDeploymentPolicy(argv[2], &policy)) {
    std::fprintf(stderr, "Invalid MCP deployment policy\n");
    return 2;
  }
  std::printf("%s\n",
              OutputJson(McpDeploymentPolicyDescription(&policy)).c_str());
  return 0;
}
}  // namespace q4t::server
