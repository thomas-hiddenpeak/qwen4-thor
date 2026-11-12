#pragma once

#include <array>
#include <string>

#include "q4t/service/guardian_process.h"
#include "q4t/service/ingress_access.h"
#include "q4t/service/ingress_credentials.h"

namespace q4t::service::ingress {

// Private same-UID control endpoint, independent of public TLS/auth and model.
// Bounded, one packet per connection; no file I/O or waiting on slow clients.
class IngressAdmin {
 public:
  bool Open(const std::string& address);
  void Step(Credentials* credentials, ClientAccess* access = nullptr);

 private:
  struct Peer {
    OwnedFd fd;
    TlsDeadline deadline;
  };
  OwnedFd listener_;
  std::array<Peer, 8> peers_;
};

}  // namespace q4t::service::ingress
