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

// Prebuild this bounded reply before accepting clients. It also works before
// a plaintext request has a trace ID or a fully initialized connection.
inline const std::string& UnavailableResponse(bool suppress_retry = false) {
  constexpr std::string_view unavailable_body =
      R"({"error":{"message":"Service entrypoint unavailable",)"
      R"("type":"service_error","code":"gateway_unavailable",)"
      R"("param":null}})";
  constexpr std::string_view header =
      "HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
      "Content-Type: application/json\r\nCache-Control: no-store\r\n";
  static const std::string payload =
      "Content-Length: " + std::to_string(unavailable_body.size()) +
      "\r\n\r\n" + std::string(unavailable_body);
  static const std::string retryable =
      std::string(header) + "Retry-After: 1\r\n" + payload;
  static const std::string ambiguous =
      std::string(header) + "X-Should-Retry: false\r\n" + payload;
  return suppress_retry ? ambiguous : retryable;
}

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
  // Duplex-only, nonblocking. Keep read and write buffers stable across
  // -1/EAGAIN and poll the returned direction. Never call concurrently.
  ssize_t TryRead(void* data, size_t size, short* wait);
  ssize_t TryWrite(std::string_view data, short* wait);
  bool Cancelled();
  short PollEvents() const;
  std::string extra_headers;
  // Reset for each HTTP request. Once bytes of a write request have reached
  // the upstream socket, a missing response cannot prove it never executed.
  bool unsafe_request_forwarded = false;
  bool keep_alive = false;
  bool http11 = true;

 private:
  int fd_;
  bool pending_input_ = false;
  std::unique_ptr<TlsStream> tls_;
};

// Keep the write half open through error handling. Only a negotiated HTTP
// connection whose response has not started may receive a new response.
void ServeConnection(int fd, bool require_tls,
                     const std::shared_ptr<TlsContext>& context,
                     auto&& handle) {
  Connection client(fd);
  bool http_ready = !require_tls;
  bool started = false;
  try {
    if (require_tls) http_ready = client.Handshake(context);
    if (http_ready) handle(client, started);
  } catch (...) {
    if (http_ready && !started)
      client.Send(UnavailableResponse(client.unsafe_request_forwarded),
                  Clock::now() + std::chrono::milliseconds(100));
  }
}

Decision Authorize(const std::string& address, pid_t parent,
                   std::string_view client, bool control, int expected_port);
OwnedFd Connect(int port, Connection& client,
                Clock::time_point deadline = Clock::time_point::max());
// An upstream response/close interrupts upload writes instead of waiting on
// client input or a full upstream socket. No request replay or body buffering.
bool Readable(int fd);
bool WriteUpload(int fd, std::string_view data, Clock::time_point deadline,
                 Connection& client, bool* forwarded = nullptr);
bool Write(int fd, std::string_view data, Clock::time_point deadline,
           Connection* client = nullptr, bool* started = nullptr);
void Error(Connection& client, int status, std::string_view code,
           std::string_view request_id, bool* started);
bool Reply(Connection& client, int status, std::string_view body,
           std::string_view request_id, bool* started);
bool Relay(int upstream, Connection& client, int idle_seconds,
           std::string_view request_id, bool* started, bool error_only = false,
           std::string_view initial = {});
std::string RequestId();

}  // namespace q4t::service::ingress
