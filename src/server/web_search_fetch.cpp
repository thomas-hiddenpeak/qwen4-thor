#include "q4t/server/web_search.h"

#include <algorithm>

#include "file_fetch_internal.h"
#include "object_protocol.h"

namespace q4t::server {
ObjectReply FetchWebPage(std::string_view url, const WebSearchOptions& request,
                         const FileFetchOptions& deployment,
                         const WebPageLimits& limits,
                         const BinaryAccess& access, FileInputBudget* budget,
                         WebPageReceipt* output, GrammarControl control) {
  using namespace object_detail;
  if (!output) return Error(500, "web_search_state_invalid", "Missing output");
  *output = {};
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  if ((access.cancelled && access.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return Error(499, "request_cancelled", "Web acquisition cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Web acquisition timed out");
  if (!request.external_web_access)
    return Error(409, "web_search_offline", "Live acquisition disabled");
  if (!ValidWebPageLimits(limits) || !budget ||
      budget->max_bytes > kFileInputBytes || !budget->max_files ||
      budget->max_files > 4096 || budget->used_files > budget->max_files ||
      budget->used_bytes > budget->max_bytes)
    return Error(500, "web_search_budget_invalid",
                 "Invalid web acquisition budget");
  if (budget->used_files == budget->max_files)
    return Error(413, "web_search_capacity", "Web page count exceeded");
  FileFetchDomainPolicy domains{request.allowed_domains,
                                request.blocked_domains};
  if (!file_detail::ValidDomains(domains))
    return Error(400, "invalid_request", "Invalid web search domains");
  FileInputSource source;
  source.kind = FileInputSource::Kind::kUrl;
  source.value = url;
  source.param = "web_search.url";
  FileInputBudget remaining;
  remaining.max_files = 1;
  remaining.max_bytes =
      std::min(limits.max_source_bytes, budget->max_bytes - budget->used_bytes);
  WebPageReceipt next;
  auto reply = FetchFileInput(access, source, deployment, &remaining,
                              &next.source, control, domains);
  // Retain attempted reservations on uncertainty or failure, exactly as the
  // existing fetcher. A parse failure never refunds an actual network read.
  budget->used_files += remaining.used_files;
  budget->used_bytes += remaining.used_bytes;
  if (!reply.ok()) return reply;
  if (next.source.urls.empty())
    return Error(503, "web_search_source_invalid", "Missing acquired source");
  reply = ParseWebPage(next.source.content, next.source.urls.back(), limits,
                       access, &next.page, control);
  if (!reply.ok()) return reply;
  *output = std::move(next);
  return {};
}
}  // namespace q4t::server
