// CPU-only transport for the q4t service entrypoint. No application retries.
#pragma once

#include <atomic>
#include <chrono>
#include <string>
#include <string_view>

#include "q4t/service/guardian_process.h"
#include "q4t/service/ingress_tls.h"

namespace q4t::service::ingress {

extern std::atomic<bool> stopped;
using Clock = std::chrono::steady_clock;

struct Decision {
  int port = 0;
  std::string token;
  std::string error = "service_binding_unavailable";
  bool operator==(const Decision&) const = default;
};

// Borrows the socket; the accept slot owns its lifetime. TLS state never leaks
// into the upstream model connection. extra_headers are locally validated CORS.
class Connection {
 public:
  explicit Connection(int fd) : fd_(fd) {}
  ~Connection();
  int Fd() const { return fd_; }
  bool Handshake(const std::shared_ptr<TlsContext>& context);
  ssize_t Read(void* data, size_t size, Clock::time_point deadline);
  bool Send(std::string_view data, Clock::time_point deadline,
            bool* started = nullptr);
  bool Cancelled();
  short PollEvents() const;
  std::string extra_headers;
  bool keep_alive = false;
  bool http11 = true;

 private:
  int fd_;
  bool pending_input_ = false;
  std::unique_ptr<TlsStream> tls_;
};

Decision Authorize(const std::string& address, pid_t parent,
                   std::string_view client, bool control, int expected_port);
OwnedFd Connect(int port, Connection& client,
                Clock::time_point deadline = Clock::time_point::max());
// An upstream response/close interrupts upload writes instead of waiting on
// client input or a full upstream socket. No request replay or body buffering.
bool Readable(int fd);
bool WriteUpload(int fd, std::string_view data, Clock::time_point deadline,
                 Connection& client);
bool Write(int fd, std::string_view data, Clock::time_point deadline,
           Connection* client = nullptr, bool* started = nullptr);
void Error(Connection& client, int status, std::string_view code,
           std::string_view request_id, bool* started);
bool Reply(Connection& client, int status, std::string_view body,
           std::string_view request_id, bool* started);
bool Relay(int upstream, Connection& client, int idle_seconds,
           std::string_view request_id, bool* started, bool error_only = false);
std::string RequestId();

}  // namespace q4t::service::ingress
