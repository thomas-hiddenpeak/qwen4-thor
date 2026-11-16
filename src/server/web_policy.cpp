#include "q4t/server/web_execution.h"

#include <algorithm>
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
  return Object({{"format", String("q4t.web.policy.v1")},
                 {"fetch", file_detail::OptionsJson(value.fetch)},
                 {"seeds", Strings(value.seeds)},
                 {"scopes", Strings(value.scopes)},
                 {"max_pages", Number(value.max_pages)},
                 {"max_depth", Number(value.max_depth)},
                 {"max_download_bytes", Number(value.max_download_bytes)},
                 {"refresh_seconds", Number(value.refresh_seconds)},
                 {"retention_seconds", Number(value.retention_seconds)}});
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
      !InputFields(
          value,
          {"version", "fetch", "seeds", "scopes", "max_pages", "max_depth",
           "max_download_bytes", "refresh_seconds", "retention_seconds"},
          &field) ||
      !value.Find("version") || !value.Find("version")->IsNumber() ||
      value.Find("version")->str != "1")
    return Invalid();
  WebDeploymentPolicy next;
  if (const auto* fetch = value.Find("fetch"))
    if (!ParseFileFetchOptions(*fetch, &next.fetch).ok()) return Invalid();
  for (const auto* name : {"seeds", "scopes"}) {
    const auto* items = value.GetArray(name);
    const bool seed = std::string_view(name) == "seeds";
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
