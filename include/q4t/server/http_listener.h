// Own both listening sockets across model startup and the accept loop.
#pragma once

#include <arpa/inet.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <string>
#include <utility>

#include "q4t/server/serve_fault.h"
#include "q4t/status.h"

namespace q4t::server {

// Single-threaded owner. Listening reserves the endpoints exclusively even
// against SO_REUSEADDR binders; a bind/close probe cannot provide that promise.
// Until Run starts accepting, TCP connections may queue (backlog 128) but no
// HTTP response or readiness is published. Clients must use bounded deadlines.
class HttpListeners {
 public:
  HttpListeners() = default;
  ~HttpListeners() { Close(); }
  HttpListeners(const HttpListeners&) = delete;
  HttpListeners& operator=(const HttpListeners&) = delete;
  HttpListeners(HttpListeners&& other) noexcept { *this = std::move(other); }
  HttpListeners& operator=(HttpListeners&& other) noexcept {
    if (this != &other) {
      Close();
      public_fd_ = std::exchange(other.public_fd_, -1);
      operations_fd_ = std::exchange(other.operations_fd_, -1);
    }
    return *this;
  }

  Status Open(const std::string& host, int port, int operations_port) {
    if (public_fd_ >= 0 || operations_fd_ >= 0)
      return Status::Fail("listeners already reserved");
    HttpListeners pending;
    auto status = OpenOne(host.c_str(), port, "public", &pending.public_fd_);
    if (!status) return status;
    detail::ServeFaultPoint("listener.after_public");
    if (operations_port) {
      status = OpenOne("127.0.0.1", operations_port, "operations",
                       &pending.operations_fd_);
      if (!status) return status;
    }
    detail::ServeFaultPoint("listener.after_pair");
    *this = std::move(pending);
    return Status();
  }

  int Public() const noexcept { return public_fd_; }
  int Operations() const noexcept { return operations_fd_; }
  void Close() noexcept {
    // Linux releases the descriptor even when close reports EINTR. Never retry
    // a close against a descriptor that could already have been reused.
    if (public_fd_ >= 0) ::close(std::exchange(public_fd_, -1));
    if (operations_fd_ >= 0) ::close(std::exchange(operations_fd_, -1));
  }

 private:
  static Status OpenOne(const char* host, int port, const char* role, int* fd) {
    const auto fail = [&](const char* operation, int error) {
      return Status::Fail(std::string(role) + " listener " + operation +
                          " failed at " + host + ":" + std::to_string(port) +
                          " (errno " + std::to_string(error) + ": " +
                          std::strerror(error) + ")");
    };
    sockaddr_in address{};
    address.sin_family = AF_INET;
    if (port < 0 || port > 65535 ||
        ::inet_pton(AF_INET, host, &address.sin_addr) != 1)
      return fail("address", EINVAL);
    address.sin_port = htons(static_cast<uint16_t>(port));
    *fd = ::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (*fd < 0) return fail("socket", errno);
    const int one = 1;
    if (::setsockopt(*fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) < 0)
      return fail("setsockopt", errno);
    if (::bind(*fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0)
      return fail("bind", errno);
    if (::listen(*fd, 128) < 0) return fail("listen", errno);
    return Status();
  }

  int public_fd_ = -1;
  int operations_fd_ = -1;
};

}  // namespace q4t::server
