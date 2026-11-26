// Correlate client results with the original model-issued local shell call.
#pragma once

#include <string>
#include <unordered_map>

#include "q4t/io/json.h"

namespace q4t::server {

inline constexpr char kLocalShellToolName[] = "q4t_builtin_local_shell";

class LocalShellHistory {
 public:
  bool Register(const io::Json& call) {
    const auto id = call.GetString("id");
    const auto target = call.GetString("call_id");
    if (id.empty() || target.empty() || aliases_.contains(id) ||
        aliases_.contains(target))
      return false;
    aliases_.emplace(id, target);
    aliases_.emplace(target, target);
    return true;
  }

  std::string Target(const io::Json& result) const {
    const auto legacy = aliases_.find(result.GetString("id"));
    if (!result.Has("call_id"))
      return legacy == aliases_.end() ? "" : legacy->second;
    const auto target = result.GetString("call_id");
    const auto found = aliases_.find(target);
    if (found == aliases_.end() || found->second != target ||
        (legacy != aliases_.end() && legacy->second != target))
      return {};
    return target;
  }

 private:
  std::unordered_map<std::string, std::string> aliases_;
};

}  // namespace q4t::server
