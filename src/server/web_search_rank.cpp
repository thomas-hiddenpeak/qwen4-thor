#include "q4t/server/web_search.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Terms = std::map<std::string, size_t>;
ObjectReply Active(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Web search cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Web search timed out");
  return {};
}
ObjectReply Capacity() {
  return Error(413, "web_search_capacity", "Web search working set exceeded");
}
// ASCII words, Unicode scalars and adjacent Unicode pairs provide a sparse
// index without a downloaded tokenizer. This is lexical, not embedding rank.
ObjectReply Tokenize(std::string_view text, size_t* remaining,
                     const BinaryAccess& access, Terms* output) {
  std::string word, previous;
  const auto add = [&](const std::string& term) {
    if (term.empty()) return true;
    if (!*remaining) return false;
    --*remaining;
    ++(*output)[term];
    return true;
  };
  for (size_t i = 0, next_check = 0; i < text.size();) {
    if (i >= next_check) {
      if (auto result = Active(access); !result.ok()) return result;
      next_check = i + 4096;
    }
    const auto c = static_cast<unsigned char>(text[i]);
    if (c < 128) {
      previous.clear();
      if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '_')
        word += c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c;
      else {
        if (!add(word)) return Capacity();
        word.clear();
      }
      ++i;
    } else {
      if (!add(word)) return Capacity();
      word.clear();
      const size_t bytes = c < 0xe0 ? 2 : c < 0xf0 ? 3 : 4;
      std::string current(text.substr(i, bytes));
      if (!add(current) || (!previous.empty() && !add(previous + current)))
        return Capacity();
      previous = std::move(current);
      i += bytes;
    }
  }
  return add(word) ? ObjectReply{} : Capacity();
}
}  // namespace

ObjectReply SearchWebPages(std::span<const WebPage> pages,
                           std::span<const std::string> queries,
                           const WebSearchOptions& options,
                           const BinaryAccess& access, size_t max_results,
                           std::vector<WebSearchHit>* output) {
  if (!output) return Error(500, "web_search_state_invalid", "Missing output");
  output->clear();
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  if (auto result = Active(access); !result.ok()) return result;
  if (pages.size() > 256 || queries.empty() || queries.size() > 16 ||
      !max_results || max_results > 50)
    return Capacity();
  Terms query_terms;
  size_t terms_remaining = 1U << 20, bytes_remaining = 16U << 20;
  size_t query_remaining = 16384;
  for (const auto& query : queries) {
    if (query.empty() || query.size() > query_remaining ||
        query.find('\0') != query.npos || detail::NormalizeUtf8(query) != query)
      return Error(400, "web_search_query_invalid", "Invalid search query");
    query_remaining -= query.size();
    auto reply = Tokenize(query, &terms_remaining, access, &query_terms);
    if (!reply.ok()) return reply;
  }
  std::set<std::string> urls;
  std::vector<Terms> documents(pages.size());
  std::vector<double> lengths(pages.size());
  std::map<std::string, size_t> frequency;
  std::vector<size_t> selected;
  double total_length = 0;
  for (size_t i = 0; i < pages.size(); ++i) {
    const auto& page = pages[i];
    file_detail::Url parsed;
    if (auto result = Active(access); !result.ok()) return result;
    if (page.text.size() > bytes_remaining ||
        page.title.size() > bytes_remaining - page.text.size())
      return Capacity();
    bytes_remaining -= page.text.size() + page.title.size();
    if (!file_detail::ParseUrl(page.url, &parsed) ||
        parsed.Text() != page.url || !urls.insert(page.url).second ||
        page.source_sha256.size() != 64 ||
        !std::all_of(page.source_sha256.begin(), page.source_sha256.end(),
                     [](char c) {
                       return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
                     }) ||
        page.text.find('\0') != page.text.npos ||
        page.title.find('\0') != page.title.npos ||
        detail::NormalizeUtf8(page.text) != page.text ||
        detail::NormalizeUtf8(page.title) != page.title)
      return Error(500, "web_search_index_invalid", "Invalid page snapshot");
    if (!WebSearchUrlAllowed(page.url, options)) continue;
    auto reply = Tokenize(page.title + ' ' + page.text, &terms_remaining,
                          access, &documents[i]);
    if (!reply.ok()) return reply;
    for (const auto& [term, count] : documents[i]) {
      lengths[i] += count;
      if (query_terms.contains(term)) ++frequency[term];
    }
    total_length += lengths[i];
    selected.push_back(i);
  }
  if (selected.empty() || query_terms.empty() || !total_length) return {};
  const double average = total_length / selected.size();
  std::vector<WebSearchHit> hits;
  for (size_t i : selected) {
    if (auto result = Active(access); !result.ok()) return result;
    double score = 0;
    for (const auto& [term, unused] : query_terms) {
      (void)unused;
      const auto found = documents[i].find(term);
      if (found == documents[i].end()) continue;
      const double matches = frequency[term];
      const double idf =
          std::log(1 + (selected.size() - matches + 0.5) / (matches + 0.5));
      const double count = found->second;
      score += idf * count * 2.2 /
               (count + 1.2 * (0.25 + 0.75 * lengths[i] / average));
    }
    if (score > 0) hits.push_back({i, score});
  }
  std::sort(hits.begin(), hits.end(), [&](const auto& a, const auto& b) {
    return a.score != b.score ? a.score > b.score
                              : pages[a.page].url < pages[b.page].url;
  });
  if (hits.size() > max_results) hits.resize(max_results);
  if (auto result = Active(access); !result.ok()) return result;
  *output = std::move(hits);
  return {};
}
}  // namespace q4t::server
