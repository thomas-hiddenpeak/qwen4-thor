#pragma once

#include <string>

namespace q4t::server {
struct ChatPlanError {
  std::string code = "invalid_request";
  std::string param;
  int http_status = 400;
};
}  // namespace q4t::server
