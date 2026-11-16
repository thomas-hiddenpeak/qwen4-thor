#pragma once

#include "mcp_provider_common.h"

namespace q4t::server::mcp_detail {
// Read MIME parts returned by Gmail FULL. Metadata remains in message; bytes
// become native MCP resources, never a base64 string in the model's text.
ObjectReply GmailMessage(const io::Json& response, std::string_view id,
                         bool full, bool attachments,
                         const ProviderFetch& fetch, io::Json* message,
                         io::Json* content);
}  // namespace q4t::server::mcp_detail
