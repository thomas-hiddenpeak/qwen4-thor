#include "q4t/server/web_execution.h"

#include <algorithm>
#include <ctime>
#include <deque>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "web_provider.h"
#include "q4t/server/completion_response.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Active(const BinaryAccess& access) {
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Web execution cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Web execution timed out");
  return {};
}
bool Text(std::string_view text, size_t maximum) {
  return !text.empty() && text.size() <= maximum &&
         text.find('\0') == text.npos && detail::NormalizeUtf8(text) == text;
}
ObjectReply Invalid() {
  return Error(400, "web_search_arguments_invalid", "Invalid web action");
}
ObjectReply Capacity() {
  return Error(413, "web_search_capacity", "Web execution budget exhausted");
}
bool SameOrigin(const std::string& a, const std::string& b) {
  file_detail::Url left, right;
  return file_detail::ParseUrl(a, &left) && file_detail::ParseUrl(b, &right) &&
         left.scheme == right.scheme && left.host == right.host &&
         left.port == right.port;
}
ObjectReply Acquire(const std::string& url, const BinaryAccess& access,
                    const WebDeploymentPolicy& policy,
                    const WebSearchOptions& options, WebExecutionBudget* budget,
                    WebPageReceipt* receipt, GrammarControl control) {
  if (auto active = Active(access); !active.ok()) return active;
  return FetchWebPage(url, options, policy.fetch, {}, access,
                      &budget->acquisition, receipt, std::move(control));
}
std::string PageIdentity(const std::string& policy, const std::string& url) {
  return Digest(
      OutputJson(Array({String("page"), String(policy), String(url)})));
}
ObjectReply RetainPage(WebIndexStore& index, const BinaryAccess& access,
                       const std::string& policy, const WebPageReceipt& receipt,
                       int ttl_seconds) {
  WebIndexSnapshot prior;
  const auto id = PageIdentity(policy, receipt.page.url);
  auto result = index.Read(access, id, &prior);
  if (!result.ok() && result.code != "web_index_unavailable") return result;
  // A concurrent complete acquisition of the same URL is a valid URL cache.
  // The query snapshot always stores its own exact page bytes and digest.
  result = index.Publish(access, id, {&receipt, 1}, prior.revision, ttl_seconds,
                         &prior);
  if (result.code == "object_revision_conflict")
    return index.Read(access, id, &prior);
  return result;
}
bool SkippableSource(const ObjectReply& result) {
  return result.code == "file_fetch_http_error" ||
         result.code == "file_fetch_target_denied" ||
         result.code == "file_fetch_transport_failed" ||
         result.code == "file_fetch_redirect_limit" ||
         result.code == "file_fetch_redirect_invalid" ||
         result.code == "web_page_media_type" ||
         result.code == "web_page_invalid";
}
ObjectReply Index(ObjectStore& objects, const BinaryAccess& access,
                  const WebDeploymentPolicy& policy,
                  const WebSearchOptions& options, WebExecutionBudget* budget,
                  std::span<const std::string> queries,
                  WebIndexSnapshot* snapshot, bool* cached, size_t* skipped,
                  GrammarControl control) {
  // With no permitted sources there is nothing to fetch or read from cache.
  if (options.empty_domain_allowlist) return {};
  WebIndexStore index(objects);
  const auto base = WebIndexIdentity(policy, options);
  auto identity = base;
  if (policy.provider) {
    auto key = Array({String("search"), String(base)});
    for (const auto& query : queries) key.array.push_back(String(query));
    identity = Digest(OutputJson(key));
  }
  auto result = index.Read(access, identity, snapshot);
  if (!result.ok() && result.code != "web_index_unavailable") return result;
  if (!options.external_web_access) {
    *cached = result.ok();
    return result;
  }
  if (result.ok() && policy.refresh_seconds > 0 &&
      std::time(nullptr) - snapshot->observed_at < policy.refresh_seconds) {
    *cached = true;
    return {};
  }
  const int64_t revision = result.ok() ? snapshot->revision : 0;
  // A fixed, explicit crawl frontier bounds coverage. Cross-origin links do
  // not enlarge it. Every redirect still obeys request AND deployment policy.
  std::deque<std::pair<std::string, size_t>> queue;
  std::set<std::string> queued, acquired;
  auto seeds = policy.seeds;
  if (policy.provider) {
    result = DiscoverWebUrls(policy, options, queries, access,
                             &budget->acquisition, &seeds, control);
    if (!result.ok()) return result;
  }
  for (const auto& seed : seeds)
    if (WebSearchUrlAllowed(seed, options) && queued.insert(seed).second)
      queue.emplace_back(seed, 0);
  std::vector<WebPageReceipt> pages;
  ObjectReply failed_source;
  size_t attempts = 0;
  while (!queue.empty() && attempts < policy.max_pages) {
    auto [url, depth] = std::move(queue.front());
    queue.pop_front();
    if (acquired.contains(url)) continue;
    WebPageReceipt receipt;
    ++attempts;
    result = Acquire(url, access, policy, options, budget, &receipt, control);
    if (!result.ok()) {
      if (!policy.provider || !SkippableSource(result)) return result;
      failed_source = std::move(result);
      ++*skipped;
      continue;
    }
    if (!acquired.insert(receipt.page.url).second) continue;
    if (depth < policy.max_depth)
      for (const auto& link : receipt.page.links) {
        if (queued.size() >= policy.max_pages) break;
        if (WebSearchUrlAllowed(link, options) &&
            std::any_of(
                policy.seeds.begin(), policy.seeds.end(),
                [&](const auto& seed) { return SameOrigin(seed, link); }) &&
            queued.insert(link).second)
          queue.emplace_back(link, depth + 1);
      }
    pages.push_back(std::move(receipt));
  }
  if (policy.provider) {
    if (pages.empty() && *skipped) return failed_source;
    for (const auto& page : pages) {
      result = RetainPage(index, access, base, page, policy.retention_seconds);
      if (!result.ok()) return result;
    }
    // A dead result must not hide independently verified sources or replace
    // the last complete search. URL snapshots contain only complete pages.
    if (*skipped) {
      *snapshot = {};
      for (auto& receipt : pages)
        snapshot->pages.push_back(std::move(receipt.page));
      return {};
    }
  }
  // Failures above never publish a partial crawl or replace the prior index.
  // A lost publication receipt is returned unchanged: no fetch/write replay.
  return index.Publish(access, identity, pages, revision,
                       policy.retention_seconds, snapshot);
}
size_t Boundary(std::string_view text, size_t end) {
  end = std::min(end, text.size());
  while (end && end < text.size() &&
         (static_cast<unsigned char>(text[end]) & 0xc0) == 0x80)
    --end;
  return end;
}
Json PageContext(const WebPage& page, const WebSearchOptions& options,
                 size_t text_budget, std::string_view pattern,
                 size_t* image_budget) {
  size_t begin = 0;
  const size_t match =
      pattern.empty() ? std::string::npos : page.text.find(pattern);
  if (match != std::string::npos && match > text_budget / 3)
    begin = Boundary(page.text, match - text_budget / 3);
  const auto end = Boundary(page.text, begin + text_budget);
  auto images = Array(), matches = Array();
  if (options.images)
    for (const auto& image : page.images) {
      if (!*image_budget) break;
      if (!WebSearchUrlAllowed(image.url, options)) continue;
      --*image_budget;
      auto item = Object({{"url", String(image.url)}});
      if (options.captions) Set(&item, "caption", String(image.caption));
      images.array.push_back(std::move(item));
    }
  bool match_limit = false;
  if (!pattern.empty()) {
    size_t at = 0;
    while ((at = page.text.find(pattern, at)) != std::string::npos) {
      if (matches.array.size() == 128) {
        match_limit = true;
        break;
      }
      const auto offset =
          Utf8Characters(std::string_view(page.text).substr(0, at));
      matches.array.push_back(
          Object({{"start_index", Number(offset)},
                  {"end_index", Number(offset + Utf8Characters(pattern))}}));
      at += pattern.size();
    }
  }
  auto item = Object({{"url", String(page.url)},
                      {"title", String(page.title)},
                      {"source_sha256", String(page.source_sha256)},
                      {"images", std::move(images)}});
  if (options.text) {
    Set(&item, "text", String(page.text.substr(begin, end - begin)));
    Set(&item, "start_index",
        Number(Utf8Characters(std::string_view(page.text).substr(0, begin))));
    Set(&item, "text_truncated", Boolean(begin > 0 || end < page.text.size()));
  }
  if (!pattern.empty()) {
    Set(&item, "matches", std::move(matches));
    Set(&item, "matches_truncated", Boolean(match_limit));
  }
  return item;
}
}  // namespace

ObjectReply ExecuteWebAction(ObjectStore& objects, const BinaryAccess& access,
                             const WebDeploymentPolicy& deployment,
                             const WebSearchOptions& options,
                             const WebAction& action,
                             std::span<const WebPage> known_sources,
                             WebExecutionBudget* budget,
                             WebExecutionResult* output,
                             GrammarControl control) {
  if (!output || !budget)
    return Error(500, "web_search_state_invalid",
                 "Missing web execution state");
  *output = {};
  if (auto active = Active(access); !active.ok()) return active;
  if (std::find(deployment.scopes.begin(), deployment.scopes.end(),
                access.scope) == deployment.scopes.end())
    return Error(403, "web_search_not_configured",
                 "No authorized search source");
  if ((!deployment.provider && deployment.seeds.empty()) ||
      (deployment.provider &&
       (!deployment.seeds.empty() || deployment.max_depth)) ||
      deployment.seeds.size() > deployment.max_pages ||
      deployment.max_pages > 32 || deployment.max_depth > 2 ||
      !deployment.max_download_bytes ||
      deployment.max_download_bytes > kFileInputBytes ||
      !file_detail::ValidOptions(deployment.fetch) ||
      !file_detail::ValidDomains(
          {options.allowed_domains, options.blocked_domains}))
    return Error(500, "web_search_policy_invalid", "Invalid deployment policy");
  budget->acquisition.max_bytes =
      std::min(budget->acquisition.max_bytes, deployment.max_download_bytes);
  if (budget->actions >= 16 || known_sources.size() > 256 ||
      budget->acquisition.used_bytes > budget->acquisition.max_bytes ||
      budget->acquisition.used_files > budget->acquisition.max_files)
    return Capacity();
  ++budget->actions;
  WebExecutionResult next;
  auto results = Array();
  size_t skipped = 0;
  size_t image_budget = options.max_images;
  const size_t context_bytes = options.unlimited_return_tokens    ? 262144
                               : options.context_size == "low"    ? 8192
                               : options.context_size == "medium" ? 16384
                                                                  : 32768;
  const size_t max_results = options.context_size == "low"      ? 4
                             : options.context_size == "medium" ? 8
                                                                : 16;
  if (action.kind == WebAction::Kind::kSearch) {
    size_t remaining = 16384;
    if (!action.url.empty() || !action.pattern.empty() ||
        action.queries.empty() || action.queries.size() > 16)
      return Invalid();
    for (const auto& query : action.queries) {
      if (!Text(query, remaining)) return Invalid();
      remaining -= query.size();
    }
    WebIndexSnapshot snapshot;
    auto result =
        Index(objects, access, deployment, options, budget, action.queries,
              &snapshot, &next.cached, &skipped, control);
    if (!result.ok()) return result;
    std::vector<WebSearchHit> hits;
    if (deployment.provider) {
      for (size_t i = 0; i < snapshot.pages.size(); ++i)
        hits.push_back({i, 1.0});
    } else {
      result = SearchWebPages(
          snapshot.pages, action.queries, options, access,
          snapshot.pages.empty() ? 1 : snapshot.pages.size(), &hits);
      if (!result.ok()) return result;
    }
    // Location refines relevance only among actual query matches. It cannot
    // create matches, authorize a source, or infer the user's IP/location.
    std::string location;
    for (const auto* field : {"city", "region", "country"})
      location += ' ' + options.location.GetString(field);
    if (location.size() > 3 && !hits.empty()) {
      const std::vector<std::string> query{location};
      std::vector<WebSearchHit> localized;
      result = SearchWebPages(snapshot.pages, query, options, access,
                              snapshot.pages.size(), &localized);
      if (!result.ok()) return result;
      for (auto& hit : hits)
        if (std::any_of(
                localized.begin(), localized.end(),
                [&](const auto& local) { return hit.page == local.page; }))
          hit.score *= 1.1;
      std::stable_sort(
          hits.begin(), hits.end(),
          [](const auto& a, const auto& b) { return a.score > b.score; });
    }
    hits.resize(std::min(hits.size(), max_results));
    const auto per_page = context_bytes / std::max<size_t>(1, hits.size());
    for (const auto& hit : hits) {
      next.sources.push_back(std::move(snapshot.pages[hit.page]));
      results.array.push_back(PageContext(next.sources.back(), options,
                                          per_page, {}, &image_budget));
    }
  } else {
    if (!action.queries.empty() ||
        (action.kind != WebAction::Kind::kOpen &&
         action.kind != WebAction::Kind::kFind) ||
        (action.kind == WebAction::Kind::kOpen && !action.pattern.empty()) ||
        (action.kind == WebAction::Kind::kFind && !Text(action.pattern, 4096)))
      return Invalid();
    file_detail::Url parsed;
    if (!file_detail::ParseUrl(action.url, &parsed)) return Invalid();
    const auto url = parsed.Text();
    if (!WebSearchUrlAllowed(url, options) ||
        !file_detail::AllowedUrl(parsed, deployment.fetch))
      return Error(403, "file_url_forbidden", "Web source is not permitted");
    const auto found =
        std::find_if(known_sources.begin(), known_sources.end(),
                     [&](const auto& page) { return page.url == url; });
    if (found != known_sources.end()) {
      next.sources.push_back(*found);
      next.cached = true;
    } else {
      WebIndexSnapshot snapshot;
      WebIndexStore index(objects);
      const auto base = WebIndexIdentity(deployment, options);
      auto result = index.Read(
          access, deployment.provider ? PageIdentity(base, url) : base,
          &snapshot);
      if (!result.ok() && result.code != "web_index_unavailable") return result;
      const auto page =
          std::find_if(snapshot.pages.begin(), snapshot.pages.end(),
                       [&](const auto& item) { return item.url == url; });
      if (page != snapshot.pages.end()) {
        next.sources.push_back(*page);
        next.cached = true;
      } else {
        if (!options.external_web_access)
          return Error(404, "web_source_unavailable",
                       "No retained page for this URL");
        WebPageReceipt receipt;
        result = Acquire(url, access, deployment, options, budget, &receipt,
                         control);
        if (!result.ok()) return result;
        if (deployment.provider) {
          result = RetainPage(index, access, base, receipt,
                              deployment.retention_seconds);
          if (!result.ok()) return result;
        }
        next.sources.push_back(std::move(receipt.page));
      }
    }
    results.array.push_back(PageContext(next.sources[0], options, context_bytes,
                                        action.pattern, &image_budget));
  }
  next.context =
      Object({{"coverage",
               String(deployment.provider ? "mwmbl_v2" : "configured_sites")},
              {"cached", Boolean(next.cached)},
              {"user_location", options.location},
              {"results", std::move(results)}});
  if (skipped) Set(&next.context, "unavailable_sources", Number(skipped));
  if (OutputJson(next.context).size() > (1U << 20)) return Capacity();
  if (auto active = Active(access); !active.ok()) return active;
  *output = std::move(next);
  return {};
}
}  // namespace q4t::server
