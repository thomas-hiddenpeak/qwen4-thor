// CPU-only transport for the q4t service entrypoint. No application retries.
#pragma once

#include <atomic>
#include <chrono>
#include <string>
#include <string_view>

#include "q4t/service/guardian_process.h"

namespace q4t::service::ingress {

extern std::atomic<bool> stopped;
using Clock = std::chrono::steady_clock;

struct Decision {
  int port = 0;
  std::string token;
  std::string error = "service_binding_unavailable";
  bool operator==(const Decision&) const = default;
};

Decision Authorize(const std::string& address, pid_t parent,
                   std::string_view client, bool control, int expected_port);
OwnedFd Connect(int port, int client);
bool Write(int fd, std::string_view data, Clock::time_point deadline,
           int client = -1, bool* started = nullptr);
void Error(int fd, int status, std::string_view code,
           std::string_view request_id, bool* started);
void Reply(int fd, int status, std::string_view body,
           std::string_view request_id, bool* started);
void Relay(int upstream, int client, int idle_seconds,
           std::string_view request_id, bool* started);
std::string RequestId();

}  // namespace q4t::service::ingress
