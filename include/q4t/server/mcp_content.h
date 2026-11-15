#pragma once

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {
// A resource URI is an opaque identifier owned by its MCP server. Validation
// never resolves it as a local path, HTTP URL, or a new credential recipient.
bool McpResourceUri(std::string_view uri);
// Validate known native content types without altering the original value.
// Media pixel/audio decoding and document preparation are separate bounded
// operations; a structurally valid result is not proof of decoded media.
Status ValidateMcpToolResult(const io::Json& result);
Status ValidateMcpResourcesResult(const io::Json& result);
}  // namespace q4t::server
