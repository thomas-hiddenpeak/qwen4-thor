// Native configured-site retrieval. No external search daemon is required.
#pragma once

#include "q4t/server/web_index.h"

namespace q4t::server {
struct WebDeploymentPolicy {
  FileFetchOptions fetch;
  std::vector<std::string> seeds, scopes;
  size_t max_pages = 16, max_depth = 1;
  uint64_t max_download_bytes = 16U << 20;
  int refresh_seconds = 300, retention_seconds = 86400;
  std::string sha256;
};
// Strict offline loading, with the existing owned/private regular-file reader.
Status ParseWebDeploymentPolicy(std::string_view raw,
                                WebDeploymentPolicy* output);
Status LoadWebDeploymentPolicy(const std::string& path,
                               WebDeploymentPolicy* output);
io::Json WebDeploymentPolicyDescription(const WebDeploymentPolicy* policy);
int RunWebDeploymentPolicyCheck(int argc, char** argv);
std::string WebIndexIdentity(const WebDeploymentPolicy& deployment,
                             const WebSearchOptions& options);

struct WebAction {
  enum class Kind { kSearch, kOpen, kFind };
  Kind kind = Kind::kSearch;
  std::vector<std::string> queries;
  std::string url, pattern;
};
struct WebExecutionBudget {
  FileInputBudget acquisition;
  size_t actions = 0;
};
struct WebExecutionResult {
  // Internal tool context, not the public web_search_call item. The adapter
  // exposes only standard actions/sources; verified passages feed the model.
  io::Json context;
  std::vector<WebPage> sources;
  bool cached = false;
};
// Admission consumes one action and retains attempted acquisition charges on
// failure. The same budget belongs to the whole request/native tool loop.
// known_sources contains only this request's previously acquired pages. Other
// open/find URLs require an actual authorized acquisition, never guessed text.
ObjectReply ExecuteWebAction(ObjectStore& objects, const BinaryAccess& access,
                             const WebDeploymentPolicy& deployment,
                             const WebSearchOptions& options,
                             const WebAction& action,
                             std::span<const WebPage> known_sources,
                             WebExecutionBudget* budget,
                             WebExecutionResult* output,
                             GrammarControl control = {});
}  // namespace q4t::server
