// Parent-only credential publication; no file I/O in the native event loop.
#pragma once

#include <array>
#include <chrono>
#include <string>

#include "q4t/service/guardian_process.h"
#include "q4t/service/ingress_access.h"

namespace q4t::service::ingress {

class AccessAdmin {
 public:
  bool Open(const std::string& address, pid_t owner);
  void Step(ClientAccess& access);

 private:
  struct Peer {
    OwnedFd fd;
    std::chrono::steady_clock::time_point deadline;
    bool writer = false;
    bool initialize = false;
    size_t bytes = 0;
    uint64_t expected_revision = 0;
    std::string digest;
    std::string payload;
  };
  void Receive(Peer& peer, ClientAccess& access);
  void Reply(Peer& peer, const ClientAccess& access, bool ok,
             const char* error = "");
  OwnedFd listener_;
  pid_t owner_ = -1;
  std::array<Peer, 2> peers_;
};

}  // namespace q4t::service::ingress
