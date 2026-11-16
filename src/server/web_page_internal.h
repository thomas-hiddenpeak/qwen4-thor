#pragma once

#include "q4t/server/web_search.h"

namespace q4t::server::web_detail {
// Exec child only, before sandbox entry and source-byte acquisition.
ObjectReply LoadPageRuntime(std::string_view mime);
ObjectReply DecodePage(const FileInputContent& input, std::string_view url,
                       const WebPageLimits& limits, WebPage* output);
}  // namespace q4t::server::web_detail
