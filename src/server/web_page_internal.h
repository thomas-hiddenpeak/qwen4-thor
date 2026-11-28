#pragma once

#include "q4t/server/web_search.h"

namespace q4t::server::web_detail {
// For a truncated page, retain its opening context and the most relevant
// separate passage within the same byte budget. Zero selects a contiguous
// prefix. The returned byte offset always starts at a Unicode scalar.
ObjectReply SelectSearchPassage(std::string_view text, size_t budget,
                                std::span<const std::string> queries,
                                const BinaryAccess& access, size_t& second);
io::Json Page(const WebPage& page);
bool Page(const io::Json& value, const WebPageLimits& limits, WebPage* page);
bool Text(const io::Json& value, const char* key, size_t limit,
          std::string* out);
// Exec child only, before sandbox entry and source-byte acquisition.
ObjectReply LoadPageRuntime(std::string_view mime);
ObjectReply DecodePage(const FileInputContent& input, std::string_view url,
                       const WebPageLimits& limits, WebPage* output);
}  // namespace q4t::server::web_detail
