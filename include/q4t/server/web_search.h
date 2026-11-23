// Native web retrieval contracts. Public options never grant network access.
#pragma once

#include <span>

#include "q4t/server/chat_plan.h"
#include "q4t/server/file_fetch.h"

namespace q4t::server {
struct WebSearchOptions {
  std::string type = "web_search";
  std::string context_size = "medium";
  bool external_web_access = true;
  bool unlimited_return_tokens = false;
  bool text = true, images = false, captions = false;
  size_t max_images = 10;
  std::vector<std::string> allowed_domains, blocked_domains;
  io::Json location;
  // Agent tools distinguish a null domain restriction from an empty allowlist.
  bool empty_domain_allowlist = false;
};

Status ParseWebSearchOptions(const io::Json& value, bool chat,
                             WebSearchOptions* output, ChatPlanError* error,
                             std::string_view path = "tools");
bool IsWebSearchType(std::string_view type);
bool WebSearchDomainMatches(std::string_view host, std::string_view domain);
bool WebSearchUrlAllowed(std::string_view url, const WebSearchOptions& options);

struct WebPageLimits {
  uint64_t max_source_bytes = 2U << 20;
  uint64_t max_text_bytes = 1U << 20;
  size_t max_links = 256, max_images = 128;
  size_t max_nodes = 200000, max_depth = 128;
  int timeout_ms = 10000;
};
struct WebImage {
  std::string url, caption;
};
struct WebPage {
  std::string url, title, text, source_sha256;
  std::vector<std::string> links;
  std::vector<WebImage> images;
};
bool ValidWebPageLimits(const WebPageLimits& limits);
// An exec-only child loads the existing system HTML codec. No script, CSS,
// external entity, network or local file is evaluated by that codec. Metadata
// is data; links are not followed here. The caller owns network authorization.
ObjectReply ParseWebPage(const FileInputContent& input, std::string_view url,
                         const WebPageLimits& limits,
                         const BinaryAccess& access, WebPage* output,
                         GrammarControl control = {});
int MaybeRunWebPageWorker(int argc, char** argv);

struct WebPageReceipt {
  FileFetchReceipt source;
  WebPage page;
};
ObjectReply FetchWebPage(std::string_view url, const WebSearchOptions& request,
                         const FileFetchOptions& deployment,
                         const WebPageLimits& limits,
                         const BinaryAccess& access, FileInputBudget* budget,
                         WebPageReceipt* output, GrammarControl control = {});

struct WebSearchHit {
  size_t page = 0;
  double score = 0;
};
// Pure lexical search over caller-owned, already acquired page snapshots.
// This operation performs no network or model work. Scope/retention and live
// refresh belong to the index owner, not to a public page's metadata.
ObjectReply SearchWebPages(std::span<const WebPage> pages,
                           std::span<const std::string> queries,
                           const WebSearchOptions& options,
                           const BinaryAccess& access, size_t max_results,
                           std::vector<WebSearchHit>* output);
}  // namespace q4t::server
