#include "q4t/server/web_execution.h"

#include <algorithm>
#include <cstdio>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/mcp_policy.h"

namespace q4t::server {
namespace {
using namespace object_detail;
Status Invalid() {
  return Status::Fail("Invalid web search deployment policy");
}
Json Strings(const std::vector<std::string>& values) {
  auto result = Array();
  for (const auto& value : values) result.array.push_back(String(value));
  return result;
}
Json Policy(const WebDeploymentPolicy& value) {
  auto policy =
      Object({{"format", String("q4t.web.policy.v1")},
              {"fetch", file_detail::OptionsJson(value.fetch)},
              {"seeds", Strings(value.seeds)},
              {"scopes", Strings(value.scopes)},
              {"max_pages", Number(value.max_pages)},
              {"max_depth", Number(value.max_depth)},
              {"max_download_bytes", Number(value.max_download_bytes)},
              {"refresh_seconds", Number(value.refresh_seconds)},
              {"retention_seconds", Number(value.retention_seconds)}});
  if (value.provider)
    Set(&policy, "provider",
        Object({{"type", String("mwmbl_v2")},
                {"endpoint", String(value.provider->endpoint)},
                {"fetch", file_detail::OptionsJson(value.provider->fetch)}}));
  return policy;
}
}  // namespace

Status ParseWebDeploymentPolicy(std::string_view raw,
                                WebDeploymentPolicy* output) {
  if (!output || raw.empty() || raw.size() > 65536) return Invalid();
  Json value;
  std::string field;
  if (!io::ParseJson(std::string(raw), &value, true,
                     {.max_depth = 5,
                      .max_values = 16384,
                      .max_string_bytes = 65536,
                      .reject_duplicate_keys = true}) ||
      !InputFields(value,
                   {"version", "fetch", "seeds", "scopes", "provider",
                    "max_pages", "max_depth", "max_download_bytes",
                    "refresh_seconds", "retention_seconds"},
                   &field) ||
      !value.Find("version") || !value.Find("version")->IsNumber() ||
      value.Find("version")->str != "1")
    return Invalid();
  WebDeploymentPolicy next;
  if (const auto* fetch = value.Find("fetch"))
    if (!ParseFileFetchOptions(*fetch, &next.fetch).ok()) return Invalid();
  if (const auto* value_provider = value.Find("provider")) {
    if (!InputFields(*value_provider, {"type", "endpoint", "fetch"}, &field) ||
        value_provider->GetString("type") != "mwmbl_v2" || value.Has("seeds"))
      return Invalid();
    auto& provider = next.provider.emplace();
    const auto* fetch = value_provider->Find("fetch");
    if (fetch && !ParseFileFetchOptions(*fetch, &provider.fetch).ok())
      return Invalid();
    if (!fetch || !fetch->Has("allow_http")) provider.fetch.allow_http = false;
    if (fetch && fetch->Has("max_redirects") && provider.fetch.max_redirects)
      return Invalid();
    provider.fetch.max_redirects = 0;
    auto endpoint = value_provider->GetString("endpoint");
    file_detail::Url url;
    if (endpoint.find_first_of("?#") != endpoint.npos ||
        !file_detail::ParseUrl(endpoint, &url) ||
        !file_detail::AllowedUrl(url, provider.fetch))
      return Invalid();
    provider.endpoint = url.Text();
    next.max_depth = 0;
  }
  for (const auto* name : {"seeds", "scopes"}) {
    const auto* items = value.GetArray(name);
    const bool seed = std::string_view(name) == "seeds";
    if (seed && next.provider) continue;
    if (!items || items->array.empty() ||
        items->array.size() > (seed ? 32U : 128U))
      return Invalid();
    std::set<std::string> unique;
    for (const auto& item : items->array) {
      if (!item.IsString() || item.str.empty() ||
          item.str.size() > (seed ? 8192U : 512U) || item.str == "*" ||
          !std::all_of(item.str.begin(), item.str.end(),
                       [](unsigned char c) { return c >= 33 && c < 127; }))
        return Invalid();
      std::string text = item.str;
      if (seed) {
        file_detail::Url url;
        if (!file_detail::ParseUrl(text, &url) ||
            !file_detail::AllowedUrl(url, next.fetch))
          return Invalid();
        text = url.Text();
      }
      if (!unique.insert(text).second) return Invalid();
      (seed ? next.seeds : next.scopes).push_back(std::move(text));
    }
  }
  auto integer = [&](const char* name, int64_t min, int64_t max, auto* out) {
    const auto* item = value.Find(name);
    if (!item) return true;
    const auto n = item->AsInt(-1);
    if (!item->IsNumber() || n < min || n > max) return false;
    *out = n;
    return true;
  };
  if (!integer("max_pages", 1, 32, &next.max_pages) ||
      !integer("max_depth", 0, 2, &next.max_depth) ||
      !integer("max_download_bytes", 1, kFileInputBytes,
               &next.max_download_bytes) ||
      !integer("refresh_seconds", 0, 86400, &next.refresh_seconds) ||
      !integer("retention_seconds", 1, 30 * 86400, &next.retention_seconds) ||
      (next.provider && next.max_depth != 0) ||
      next.max_pages < next.seeds.size() ||
      next.refresh_seconds > next.retention_seconds)
    return Invalid();
  std::sort(next.scopes.begin(), next.scopes.end());
  next.sha256 = Digest(OutputJson(Policy(next)));
  *output = std::move(next);
  return {};
}
Status LoadWebDeploymentPolicy(const std::string& path,
                               WebDeploymentPolicy* output) {
  std::string raw;
  if (!ReadMcpPrivateConfig(path, &raw)) return Invalid();
  return ParseWebDeploymentPolicy(raw, output);
}
Json WebDeploymentPolicyDescription(const WebDeploymentPolicy* policy) {
  return Object(
      {{"profile", String("q4t-web-deployment-policy-v1")},
       {"configured", Boolean(policy && !policy->sha256.empty())},
       {"sha256", policy ? String(policy->sha256) : Json()},
       {"backend",
        String(policy && policy->provider ? "mwmbl_v2"
                                          : "native_configured_sites")},
       {"seed_count", Number(policy ? policy->seeds.size() : 0)},
       {"scope_count", Number(policy ? policy->scopes.size() : 0)},
       {"max_pages", Number(policy ? policy->max_pages : 0)},
       {"max_download_bytes", Number(policy ? policy->max_download_bytes : 0)},
       {"request_override", Boolean(false)},
       {"core_readiness_dependency", Boolean(false)},
       {"model_restart_on_error", Boolean(false)}});
}
int RunWebDeploymentPolicyCheck(int argc, char** argv) {
  if (argc != 3) {
    std::fprintf(stderr, "Usage: q4t web-policy-check FILE\n");
    return 2;
  }
  WebDeploymentPolicy policy;
  std::string raw;
  if (!ReadMcpPrivateConfig(argv[2], &raw) ||
      !ParseWebDeploymentPolicy(raw, &policy)) {
    std::fprintf(stderr, "Invalid web search deployment policy\n");
    return 2;
  }
  auto description = WebDeploymentPolicyDescription(&policy);
  // The index uses a semantic policy identity. Packaging also binds the
  // exact private input bytes read and parsed above, without a second read.
  Set(&description, "source_sha256", String(Digest(raw)));
  std::printf("%s\n", OutputJson(description).c_str());
  return 0;
}
std::string WebIndexIdentity(const WebDeploymentPolicy& deployment,
                             const WebSearchOptions& options) {
  auto allowed = options.allowed_domains, blocked = options.blocked_domains;
  std::sort(allowed.begin(), allowed.end());
  std::sort(blocked.begin(), blocked.end());
  // Live/offline, location, query and output formatting do not alter what a
  // page is allowed to contain. Include every acquisition/parser policy.
  return Digest(
      OutputJson(Object({{"deployment", Policy(deployment)},
                         {"parser", String("q4t.web.page.v1-default-limits")},
                         {"allowed_domains", Strings(allowed)},
                         {"blocked_domains", Strings(blocked)}})));
}
}  // namespace q4t::server
