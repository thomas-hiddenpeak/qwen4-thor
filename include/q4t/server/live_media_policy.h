// Startup authority for the untrusted WebRTC frontend data channel.
#pragma once

#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <utility>

#include "q4t/server/chat_plan.h"

namespace q4t::server {
class LiveMediaPolicy {
 public:
  // Absent permissions allow every applicable event, empty lists allow none.
  // Replacing an existing policy is atomic with respect to validation.
  Status Configure(const io::Json* client, ChatPlanError* error);
  bool AllowsClient(std::string_view type) const;
  bool AllowsServer(const io::Json& event) const;

 private:
  std::optional<std::set<std::string, std::less<>>> client_;
  std::optional<std::set<std::pair<std::string, std::string>>> server_;
};
}  // namespace q4t::server
