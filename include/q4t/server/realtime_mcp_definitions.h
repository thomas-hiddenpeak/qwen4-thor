// Connection-local MCP definitions. No network or executable catalog authority.
#pragma once

#include <cstdint>
#include <map>

#include "q4t/server/responses_mcp.h"

namespace q4t::server {
class RealtimeMcpDefinitions;
class RealtimeMcpSelection {
 public:
  // Safe for session/response projection: authorization and ALL headers
  // removed.
  const io::Json& Tools() const { return tools_; }
  // Private immutable execution snapshot. Never serialize into model/history.
  const std::vector<ResponsesMcpDefinition>& Active() const { return active_; }

 private:
  friend class RealtimeMcpDefinitions;
  const RealtimeMcpDefinitions* owner_ = nullptr;
  uint64_t revision_ = 0;
  io::Json tools_;
  std::vector<ResponsesMcpDefinition> active_;
  std::map<std::string, ResponsesMcpDefinition> remembered_;
  size_t retained_bytes_ = 0;
};

class RealtimeMcpDefinitions {
 public:
  static constexpr size_t kMaximumServers = 64;
  static constexpr size_t kMaximumBytes = 4ULL << 20;
  explicit RealtimeMcpDefinitions(size_t maximum_servers = kMaximumServers,
                                  size_t maximum_bytes = kMaximumBytes)
      : maximum_servers_(maximum_servers), maximum_bytes_(maximum_bytes) {}
  RealtimeMcpDefinitions(const RealtimeMcpDefinitions&) = delete;
  RealtimeMcpDefinitions& operator=(const RealtimeMcpDefinitions&) = delete;
  // Pure transaction. Full targets replace old credentials/defaults; a label
  // reference inherits its committed definition and applies explicit fields.
  // Removed tools remain referenceable within this bounded connection only.
  // Failure leaves both this owner and the caller's previous selection intact.
  Status Prepare(const io::Json& tools, RealtimeMcpSelection* selection,
                 ChatPlanError* error) const;
  // Call only after the owning session accepted all settings/response work.
  // A stale, repeated or different connection's transaction cannot commit.
  // Active()/Tools() remain available for that accepted immutable response.
  Status Commit(RealtimeMcpSelection* selection, ChatPlanError* error);
  size_t RetainedServers() const { return remembered_.size(); }
  size_t RetainedBytes() const { return retained_bytes_; }

 private:
  const size_t maximum_servers_, maximum_bytes_;
  uint64_t revision_ = 0;
  size_t retained_bytes_ = 0;
  std::map<std::string, ResponsesMcpDefinition> remembered_;
};
}  // namespace q4t::server
