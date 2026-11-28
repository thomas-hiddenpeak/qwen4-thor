#include "web_provider.h"

#include <algorithm>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr uint64_t kResponseBytes = 512U << 10;

std::string QueryUrl(const std::string& endpoint, std::string_view query) {
  static constexpr char hex[] = "0123456789ABCDEF";
  std::string url = endpoint + "?q=";
  for (unsigned char c : query) {
    if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
        (c >= '0' && c <= '9') || c == '-' || c == '.' || c == '_' ||
        c == '~') {
      url += static_cast<char>(c);
    } else {
      url += '%';
      url += hex[c >> 4];
      url += hex[c & 15];
    }
  }
  return url;
}

ObjectReply Discover(const WebDeploymentPolicy& policy,
                     const WebSearchOptions& options, const std::string& query,
                     const BinaryAccess& access, FileInputBudget* budget,
                     std::vector<std::string>* urls, GrammarControl control) {
  const auto& provider = *policy.provider;
  const std::string url = QueryUrl(provider.endpoint, query);
  FileInputSource source;
  source.kind = FileInputSource::Kind::kUrl;
  source.value = url;
  source.param = "web_search.query";
  if (source.value.size() > 8192)
    return Error(413, "web_search_provider_query_capacity",
                 "Encoded search query exceeds the provider URL capacity");
  if (budget->used_files >= budget->max_files ||
      budget->used_bytes >= budget->max_bytes)
    return Error(413, "web_search_capacity", "Web execution budget exhausted");
  FileInputBudget transfer;
  transfer.max_files = 1;
  transfer.max_bytes =
      std::min(kResponseBytes, budget->max_bytes - budget->used_bytes);
  FileFetchReceipt receipt;
  auto reply = FetchFileInput(access, source, provider.fetch, &transfer,
                              &receipt, control);
  budget->used_files += transfer.used_files;
  budget->used_bytes += transfer.used_bytes;
  if (!reply.ok()) {
    if (reply.code == "file_fetch_http_error" ||
        reply.code == "file_fetch_dns_failed" ||
        reply.code == "file_fetch_transport_failed" ||
        reply.code == "file_fetch_tls_failed")
      return Error(503, "web_search_provider_unavailable",
                   "Search provider did not serve the query");
    return reply;
  }
  Json data;
  if (receipt.content.mime_type != "application/json" ||
      !io::ParseJson(receipt.content.bytes, &data, false,
                     {.max_depth = 16,
                      .max_values = 32768,
                      .max_string_bytes = kResponseBytes,
                      .reject_duplicate_keys = true}) ||
      data.GetString("query") != query || !data.GetArray("results"))
    return Error(502, "web_search_provider_invalid",
                 "Invalid search provider response");
  std::set<std::string> unique;
  for (const auto& item : data.GetArray("results")->array) {
    const auto raw = item.GetString("url");
    if (raw.empty())
      return Error(502, "web_search_provider_invalid",
                   "Missing search result URL");
    file_detail::Url parsed;
    if (file_detail::ParseUrl(raw, &parsed) &&
        file_detail::AllowedUrl(parsed, policy.fetch) &&
        WebSearchUrlAllowed(raw, options) &&
        unique.insert(parsed.Text()).second)
      urls->push_back(parsed.Text());
    if (urls->size() >= policy.max_pages) break;
  }
  return {};
}
}  // namespace

ObjectReply DiscoverWebUrls(const WebDeploymentPolicy& policy,
                            const WebSearchOptions& options,
                            std::span<const std::string> queries,
                            const BinaryAccess& access, FileInputBudget* budget,
                            std::vector<std::string>* urls,
                            GrammarControl control) {
  std::vector<std::vector<std::string>> lists;
  for (const auto& query : queries) {
    auto reply = Discover(policy, options, query, access, budget,
                          &lists.emplace_back(), control);
    if (!reply.ok()) return reply;
  }
  // Interleave queries before applying the shared page limit.
  std::set<std::string> seen;
  for (size_t rank = 0; rank < policy.max_pages; ++rank)
    for (const auto& list : lists) {
      if (rank < list.size() && seen.insert(list[rank]).second)
        urls->push_back(list[rank]);
      if (urls->size() == policy.max_pages) return {};
    }
  return {};
}
}  // namespace q4t::server
