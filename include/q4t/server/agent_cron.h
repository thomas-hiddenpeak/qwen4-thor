// Five-field Managed Agents schedule interpreted in the requested IANA zone.
#pragma once

#include <array>
#include <bitset>
#include <chrono>
#include <optional>
#include <string_view>

namespace q4t::server {

class AgentCron {
 public:
  static std::optional<AgentCron> Parse(std::string_view expression);
  bool Matches(std::chrono::sys_time<std::chrono::minutes> instant,
               const std::chrono::time_zone& zone) const;

 private:
  std::array<std::bitset<64>, 5> fields_;
  bool day_wildcard_ = false;
};

}  // namespace q4t::server
