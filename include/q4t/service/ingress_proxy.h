// Explicit native HTTP/proxy policy. A proxy never establishes API identity.
#pragma once

#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/http_request.h"
#include "q4t/service/ingress_address.h"

namespace q4t::service::ingress {

struct ResolvedRequest {
  Address source;
  std::string host;
  std::string scheme;
  std::string target;
  bool proxied = false;
};

class HttpPolicy {
 public:
  bool Load(const io::Json& value);
  bool Resolve(const server::HttpRequestHeaders& headers, const Address& peer,
               const std::string& target, bool tls,
               ResolvedRequest* result) const;
  int keep_alive_requests = 100;
  int keep_alive_idle_ms = 5000;
  int request_timeout_ms = 10000;
  std::string path_prefix;

 private:
  bool Trusted(const Address& peer) const;
  std::vector<Network> trusted_;
  std::string forwarding_;
  bool allow_stripped_prefix_ = false;
};

}  // namespace q4t::service::ingress
