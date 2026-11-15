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
      if (!InputFields(item, {"kind", "id", "url", "generation", "scopes"},
                       &field, "target") ||
          !Text(item.Find("kind"), 32) || !Text(item.Find("id"), 256) ||
          !Text(item.Find("generation"), 128) || !Text(item.Find("url"), 8192))
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
      file_detail::Url url;
      if (!file_detail::ParseUrl(item.GetString("url"), &url) ||
          (url.scheme == "http" && !next.fetch.allow_http))
        return Invalid();
      const auto* scopes = item.GetArray("scopes");
      if (!scopes || scopes->array.empty() || scopes->array.size() > 128)
        return Invalid();
      McpDeploymentTarget target{
          kind, id, url.Text(), item.GetString("generation"), {}};
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

Status LoadMcpDeploymentPolicy(const std::string& path,
                               McpDeploymentPolicy* output) {
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
  return ParseMcpDeploymentPolicy(raw, output);
}

ObjectReply ResolveMcpDeploymentTarget(const McpDeploymentPolicy* policy,
                                       std::string_view scope,
                                       ResponsesMcpDefinition* definition) {
  if (!definition)
    return Error(500, "mcp_preparation_invalid", "Missing MCP definition");
  if (definition->target_kind == "server_url") return {};
  definition->credentials.url.clear();
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
      std::string binding;
      if (!InputValueIdentity(
              Object({{"kind", String(target.kind)},
                      {"id", String(target.id)},
                      {"url", String(target.url)},
                      {"generation", String(target.generation)}}),
              &binding))
        return Error(500, "mcp_preparation_invalid", "Invalid MCP route");
      definition->credentials.url = target.url;
      definition->target_binding = std::move(binding);
      return {};
    }
  return Error(503, "mcp_target_mapping_required",
               "MCP target has no configured deployment route");
}

Json McpDeploymentPolicyDescription(const McpDeploymentPolicy* policy) {
  return Object({{"profile", String("q4t-mcp-deployment-policy-v1")},
                 {"configured", Boolean(policy && !policy->sha256.empty())},
                 {"sha256", policy ? String(policy->sha256) : Json()},
                 {"target_count", Number(policy ? policy->targets.size() : 0)},
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
