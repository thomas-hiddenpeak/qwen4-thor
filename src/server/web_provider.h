#pragma once

#include "q4t/server/web_execution.h"

namespace q4t::server {
// Each provider search yields candidate URLs. Only the separate, authorized
// page fetch can produce a source for the model or a citation.
ObjectReply DiscoverWebUrls(const WebDeploymentPolicy& policy,
                            const WebSearchOptions& options,
                            std::span<const std::string> queries,
                            const BinaryAccess& access, FileInputBudget* budget,
                            std::vector<std::string>* urls,
                            GrammarControl control);
}  // namespace q4t::server
