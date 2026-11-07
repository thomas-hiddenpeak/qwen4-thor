#pragma once

#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"

namespace q4t::service::ingress {

class Cors {
 public:
  bool Load(const io::Json& value);
  bool Enabled() const { return !origins_.empty(); }
  bool Response(std::string_view origin, std::string* headers) const;
  bool Preflight(std::string_view method, std::string_view requested_headers,
                 std::string* headers) const;

 private:
  std::vector<std::string> origins_, methods_, headers_, exposed_;
  bool credentials_ = false;
  unsigned max_age_ = 600;
};

}  // namespace q4t::service::ingress
