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

// Private model preparation, not a replacement for the public native result.
// resources are authenticated frozen Resource() views from the invocation
// owner. Unresolved URIs must be settled before model admission; returned file,
// image and audio parts still require the corresponding bounded preparation.
struct McpContentProjection {
  io::Json content;
  std::vector<std::string> unresolved_resources;
};
Status ProjectMcpToolResult(const io::Json& result, const io::Json& resources,
                            McpContentProjection* projection);
}  // namespace q4t::server
