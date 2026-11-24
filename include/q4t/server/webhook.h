#pragma once

#include "q4t/server/file_fetch.h"
#include "q4t/server/responses_state.h"

namespace q4t::server {
// The roots are internal names from the pinned SDK graph.
Status ValidateWebhook(const io::Json& value, const char* root,
                       ChatPlanError* error);

// Returns the subscriber's HTTP status; a non-2xx is an observed response.
// The caller owns the deadline and may discard a worker stalled in DNS/TLS.
ObjectReply DeliverWebhook(const StateAccess& access,
                           const FileFetchOptions& network,
                           const std::string& url, const io::Json& event,
                           const std::vector<std::string>& signing_secrets);
ObjectReply HandleWebhook(ObjectStore& objects, const StateAccess& access,
                          const FileFetchOptions* network,
                          std::string_view method, std::string_view target,
                          const std::string& body);
// Only explicit deployment-owned destinations may receive outbound events.
Status LoadWebhookPolicy(const std::string& path, FileFetchOptions* network,
                         std::string* source_sha256 = nullptr);
int RunWebhookPolicyCheck(int argc, char** argv);
int MaybeRunWebhookWorker(int argc, char** argv);
}  // namespace q4t::server
