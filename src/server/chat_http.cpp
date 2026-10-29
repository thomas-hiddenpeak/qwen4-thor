// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <thread>
#include "q4t/server/http_request.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
using detail::ValidRequestKey;
using detail::SendSimple;
using detail::SendError;

namespace detail {
// HTTP EOF ends the response lifetime, including TCP write-half-close. This
// endpoint deliberately does not support half-close-and-wait clients.
// Do not consume bytes: request parsing remains the sole socket reader.
bool ClientConnectionFailed(int fd) {
  pollfd peer{fd, POLLRDHUP, 0};
  return poll(&peer, 1, 0) > 0 &&
         (peer.revents & (POLLRDHUP | POLLERR | POLLHUP)) != 0;
}

bool RequestCancelled(RequestControl* control, int fd) {
  if (control->Cancelled()) return true;
  if (ClientConnectionFailed(fd))
    control->Cancel(RequestControl::State::kDisconnect);
  return control->Cancelled();
}

bool ValidRequestKey(const std::string& key) {
  return key.size() == 64 &&
         std::all_of(key.begin(), key.end(), [](unsigned char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}


}

void ChatServer::FinishConnection(int fd) noexcept {
  {
    const std::lock_guard<std::mutex> lock(connections_mu_);
    connections_.erase(fd);
    ::close(fd);
  }
  // The last access to server-owned state publishes all handler cleanup.
  active_conns_.fetch_sub(1, std::memory_order_release);
}

void ChatServer::RequestStop() {
  // SC store/load closes the stop-before-listener-publication handshake.
  // Both threads cannot miss the other's publication. Still signal-safe.
  static_assert(std::atomic<bool>::is_always_lock_free);
  static_assert(std::atomic<int>::is_always_lock_free);
  stop_requested_.store(true);
  const int fd = listen_fd_.load();
  if (fd >= 0) ::shutdown(fd, SHUT_RDWR);
}

Status ChatServer::Run() {
  if (stop_requested_.load()) return Status();
  const int listen_fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listen_fd < 0) return Status::Fail("socket failed");
  ScopeExit close_listener([&] {
    listen_fd_.store(-1);
    ::close(listen_fd);
  });
  int one = 1;
  ::setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  if (::inet_pton(AF_INET, host_.c_str(), &addr.sin_addr) != 1)
    return Status::Fail("bad address");
  addr.sin_port = htons(static_cast<uint16_t>(port_));
  if (::bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0)
    return Status::Fail("bind failed");
  if (::listen(listen_fd, 128) < 0) return Status::Fail("listen failed");
  detail::ServeFaultPoint("connection.before_publish");
  listen_fd_.store(listen_fd);
  if (stop_requested_.load()) ::shutdown(listen_fd, SHUT_RDWR);
  std::fprintf(stderr, "[q4t] serving on port %d (host %s, model %s)\n",
               port_, host_.c_str(), model_name_.c_str());
  try {
    while (!stop_requested_.load()) {
      sockaddr_in client{};
      socklen_t clen = sizeof(client);
      const int fd =
          ::accept(listen_fd, reinterpret_cast<sockaddr*>(&client), &clen);
      if (fd < 0) {
        if (stop_requested_.load()) break;
        if (errno == EINTR) continue;
        std::fprintf(stderr, "[q4t] accept failed: %s\n", std::strerror(errno));
        serving_failed_.store(true);
        break;
      }
      bool counted = false, registered = false;
      ScopeExit rollback_connection([&] {
        if (registered) {
          FinishConnection(fd);
        } else {
          ::close(fd);
          if (counted) active_conns_.fetch_sub(1, std::memory_order_release);
        }
      });
      int nodelay = 1;
      ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
      const timeval timeout{30, 0};
      ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
      ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
      counted = true;
      if (active_conns_.fetch_add(1, std::memory_order_relaxed) >= conn_cap_) {
        SendSimple(fd, 503, "Service Unavailable",
                   "{\"error\":{\"message\":\"server overloaded, retry\"}}",
                   "application/json",
                   {std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(250)});
        continue;
      }
      {
        const std::lock_guard<std::mutex> lock(connections_mu_);
        connections_.insert(fd);
        registered = true;
      }
      detail::ServeFaultPoint("connection.before_thread");
      std::thread handler([this, fd]() {
        ScopeExit connection_cleanup([&] { FinishConnection(fd); });
        try {
          HandleClient(fd);
        } catch (const std::exception& error) {
          std::fprintf(stderr, "[q4t] HTTP handler exception: %s\n", error.what());
          detail::SendExceptionResponse(fd, detail::ResponseState::kUnsent);
        } catch (...) {
          std::fprintf(stderr, "[q4t] unknown HTTP handler exception\n");
          detail::SendExceptionResponse(fd, detail::ResponseState::kUnsent);
        }
      });
      rollback_connection.Release();  // The constructed thread now owns fd.
      try {
        handler.detach();
      } catch (...) {
        // Never destroy a still-joinable std::thread during unwinding.
        RequestStop();
        {
          const std::lock_guard<std::mutex> lock(connections_mu_);
          if (connections_.contains(fd)) ::shutdown(fd, SHUT_RDWR);
        }
        handler.join();
        throw;
      }
    }
  } catch (const std::exception& error) {
    std::fprintf(stderr, "[q4t] accept/thread exception: %s\n", error.what());
    serving_failed_.store(true);
  } catch (...) {
    std::fprintf(stderr, "[q4t] unknown accept/thread exception\n");
    serving_failed_.store(true);
  }
  RequestStop();
  std::fprintf(stderr, "[q4t] shutting down: draining in-flight requests...\n");
  requests_.CancelAll();
  {
    const std::lock_guard<std::mutex> lock(connections_mu_);
    for (int fd : connections_) ::shutdown(fd, SHUT_RDWR);
  }
  StopScheduler();
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    seq_stopping_ = true;
  }
  seq_cv_.notify_all();
  while (active_conns_.load(std::memory_order_acquire) > 0)
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  close_listener.Run();
  std::fprintf(stderr, "[q4t] shutdown complete (0 in-flight remaining)\n");
  return serving_failed_.load() ? Status::Fail("serve failed") : Status();
}

void ChatServer::HandleClient(int fd) {
  std::string method, path, body;
  HttpBodyLease lease(body_budget_);
  int failure_status = 400;
  if (!ReadHttpRequest(fd, &method, &path, &body, 30000, &lease,
                       &failure_status)) {
    const char* reason = failure_status == 408 ? "Request Timeout" :
                         failure_status == 503 ? "Service Unavailable" :
                                                 "Bad Request";
    SendSimple(fd, failure_status, reason, reason, "text/plain");
    return;
  }
  const auto t0 = std::chrono::steady_clock::now();
  Dispatch(fd, method, path, body);
  static const bool access_log = std::getenv("Q4T_ACCESS_LOG") != nullptr;
  if (access_log) {
    const double ms = std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - t0)
                          .count();
    std::fprintf(stderr, "[q4t] %s %s %.1fms\n", method.c_str(), path.c_str(),
                 ms);
  }
}

void ChatServer::Dispatch(int fd, const std::string& method,
                          const std::string& path, const std::string& body) {
  if (path == "/healthz" && method == "GET") {
    HandleHealth(fd);
    return;
  }
  if (path == "/v1/models" && method == "GET") {
    HandleModels(fd);
    return;
  }
  if (path == "/metrics" && method == "GET") {
    HandleMetrics(fd);
    return;
  }
  if (path == "/v1/requests/cancel" && method == "POST") {
    io::Json req;
    if (!io::ParseJson(body, &req, true, kRequestJsonLimits).ok() ||
        !req.IsObject() ||
        !ValidRequestKey(req.GetString("cancel_token"))) {
      SendError(fd, 400, "invalid cancellation request");
      return;
    }
    if (!requests_.Cancel(req.GetString("request_id"),
                          req.GetString("cancel_token"))) {
      SendError(fd, 404, "no cancellable request with these credentials");
      return;
    }
    seq_cv_.notify_all();
    sched_cv_.notify_one();
    SendSimple(fd, 202, "Accepted", "{\"cancel_requested\":true}",
               "application/json");
    return;
  }
  if (path == "/v1/chat/completions" && method == "POST") {
    HandleChat(fd, body);
    return;
  }
  SendSimple(fd, 404, "Not Found",
             "{\"error\":{\"message\":\"not found\"}}", "application/json");
}

}  // namespace q4t::server
