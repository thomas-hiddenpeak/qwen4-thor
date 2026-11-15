// Optional deployment-owned MCP routes. Request JSON cannot alter these.
#pragma once

#include "q4t/server/file_fetch.h"

namespace q4t::server {
struct ResponsesMcpDefinition;
struct McpDeploymentTarget {
  std::string kind;
  std::string id;
  std::string url;
  std::string generation;
  std::vector<std::string> scopes;
};
struct McpDeploymentPolicy {
  FileFetchOptions fetch;
  std::vector<McpDeploymentTarget> targets;
  std::string sha256;
};
// Strict, transactional and offline. Loading accepts only an owned private
// regular file. No endpoint, secret or provider name is emitted on failure.
Status ParseMcpDeploymentPolicy(std::string_view raw,
                                McpDeploymentPolicy* output);
Status LoadMcpDeploymentPolicy(const std::string& path,
                               McpDeploymentPolicy* output);
// Resolve only a configured, scope-authorized route. The private target
// binding excludes rotating request credentials and includes route generation.
ObjectReply ResolveMcpDeploymentTarget(const McpDeploymentPolicy* policy,
                                       std::string_view scope,
                                       ResponsesMcpDefinition* definition);
io::Json McpDeploymentPolicyDescription(const McpDeploymentPolicy* policy);
int RunMcpDeploymentPolicyCheck(int argc, char** argv);
}  // namespace q4t::server
