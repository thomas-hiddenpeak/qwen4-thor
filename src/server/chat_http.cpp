// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"

#include <new>

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
#include "q4t/server/input_contract.h"
#include "q4t/server/request_json.h"
#include "q4t/server/client_request.h"

namespace q4t::server {
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
  return ValidCancelToken(key);
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
  static_assert(std::atomic<bool>::is_always_lock_free);
  stop_requested_.store(true);
}

Status ChatServer::Run() {
  if (stop_requested_.load())
    return serving_failed_.load() ? Status::Fail("serve failed") : Status();
  const int listen_fd = ::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
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
  std::fprintf(stderr, "[q4t] serving on port %d (host %s, model %s)\n",
               port_, host_.c_str(), model_name_.c_str());
  try {
    bool draining = false;
    auto drain_until = std::chrono::steady_clock::time_point::max();
    for (;;) {
      if (stop_requested_.load() && !draining) {
        draining = true;
        drain_until =
            std::chrono::steady_clock::now() + std::chrono::seconds(2);
        requests_.CancelAll();
        {
          const std::lock_guard<std::mutex> lock(seq_mu_);
          seq_stopping_ = true;
        }
        seq_cv_.notify_all();
        sched_cv_.notify_all();
      }
      if (draining && (active_conns_.load() == 0 ||
                       std::chrono::steady_clock::now() >= drain_until))
        break;
      pollfd listener{listen_fd, POLLIN, 0};
      const int ready = ::poll(&listener, 1, 25);
      if (ready == 0 || (ready < 0 && errno == EINTR)) continue;
      if (ready < 0 || (listener.revents & (POLLERR | POLLHUP | POLLNVAL))) {
        serving_failed_.store(true);
        break;
      }
      sockaddr_in client{};
      socklen_t clen = sizeof(client);
      const int fd =
          ::accept(listen_fd, reinterpret_cast<sockaddr*>(&client), &clen);
      if (fd < 0) {
        if (stop_requested_.load()) break;
        if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
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
#ifdef Q4T_SERVE_FAULT_INJECTION
      if (std::getenv("Q4T_SERVE_TEST_SNDBUF")) {
        const int small_buffer = 1024;
        ::setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &small_buffer,
                     sizeof(small_buffer));
      }
#endif
      const timeval timeout{30, 0};
      ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
      ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
      counted = true;
      if (active_conns_.fetch_add(1, std::memory_order_relaxed) >=
          conn_cap_ + kControlConnections + kObservationConnections) {
        metrics_.connection_rejections[
            static_cast<size_t>(HttpConnectionClass::kUnclassified)]
            .fetch_add(1);
        detail::HttpResponseContext context(fd, &metrics_.http_transport);
        context.RecordReceiveFailure(HttpReceiveFailure::kConnectionCapacity,
                                     503);
        SendError(fd, 503, "connection capacity exhausted",
                  {std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(250)}, "capacity_exhausted");
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
        } catch (const std::bad_alloc&) {
          detail::SendExceptionResponse(fd, detail::ResponseState::kUnsent,
                                        detail::ExceptionKind::kHostMemory);
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
  ClientLease client_lease;  // Outlives response and heartbeat cleanup.
  detail::HttpResponseContext context(fd, &metrics_.http_transport);
  bool dispatch_started = false;
  try {
    std::string method, path, body;
    HttpBodyLease lease(body_budget_);
    HttpRequestHeaders headers;
    std::atomic<int>* admission = nullptr;
    ScopeExit release_admission([&] {
      if (admission) admission->fetch_sub(1);
    });
    int failure_status = 400;
    const auto admit = [&] {
      if (!headers.request_id.empty()) context.SetTraceId(headers.request_id);
      ClientIdentity identity;
      if (!AdmitClientRequest(method, path, &headers, clients_, &client_lease,
                               &identity, &failure_status))
        return false;
      context.SetClient(identity, &client_lease);
      const auto kind = ClassifyHttpConnection(method, path);
      if (kind == HttpConnectionClass::kBusiness && stop_requested_.load()) {
        failure_status = 503;
        headers.error_code = "server_draining";
        headers.failure = HttpReceiveFailure::kDraining;
        return false;
      }
      auto& counter = kind == HttpConnectionClass::kControl
                          ? control_conns_
                          : kind == HttpConnectionClass::kObservation
                                ? observation_conns_
                                : business_conns_;
      const int cap = kind == HttpConnectionClass::kControl
                          ? kControlConnections
                          : kind == HttpConnectionClass::kObservation
                                ? kObservationConnections
                                : conn_cap_;
      if (!AcquireHttpConnection(counter, cap)) {
        metrics_.connection_rejections[static_cast<size_t>(kind)].fetch_add(1);
        failure_status = 503;
        headers.error_code = kind == HttpConnectionClass::kObservation
                                 ? "observation_capacity_exhausted"
                                 : "capacity_exhausted";
        headers.failure = HttpReceiveFailure::kConnectionCapacity;
        return false;
      }
      admission = &counter;
      if (method == "POST" && (path == "/v1/chat/completions" ||
                               path == "/v1/chat/preflight")) {
        const auto memory = memory_admission_.Observe();
        if (!memory.Allowed()) {
          RecordMemoryRejection(fd, memory.state, MemoryCheckpoint::kHeaders);
          failure_status = 503;
          headers.error_code = memory.StateName();
          headers.failure = HttpReceiveFailure::kAdmission;
          return false;
        }
      }
      return true;
    };
    if (!ReadHttpRequest(fd, &method, &path, &body, 30000, &lease,
                         &failure_status, &headers, admit)) {
      if (!headers.request_id.empty()) context.SetTraceId(headers.request_id);
      context.RecordReceiveFailure(headers.failure, failure_status,
                                   headers.system_error);
      const char* reason = failure_status == 408   ? "Request Timeout"
                           : failure_status == 503 ? "Service Unavailable"
                           : failure_status == 401 ? "Unauthorized"
                           : failure_status == 403 ? "Forbidden"
                           : failure_status == 413 ? "Content Too Large"
                           : failure_status == 429 ? "Too Many Requests"
                                                   : "Bad Request";
      SendError(fd, failure_status, reason, {}, headers.error_code,
                headers.error_param);
      return;
    }
    if (path == "/v1/chat/completions" || path == "/v1/chat/preflight" ||
        path == "/v1/requests/cancel") {
      if (method == "POST") {
        std::string type = headers.content_type;
        for (char& c : type)
          if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
        const auto semi = type.find(';');
        std::string media = type.substr(0, semi);
        while (!media.empty() && (media.back() == ' ' || media.back() == '\t'))
          media.pop_back();
        std::string charset = semi == type.npos ? "" : type.substr(semi + 1);
        while (!charset.empty() &&
               (charset.front() == ' ' || charset.front() == '\t'))
          charset.erase(0, 1);
        if (media != "application/json" ||
            (!charset.empty() && charset != "charset=utf-8" &&
             charset != "charset=\"utf-8\"")) {
          context.RecordReceiveFailure(HttpReceiveFailure::kMedia, 415);
          SendError(fd, 415, "Content-Type must be application/json with UTF-8",
                    {}, "unsupported_media_type", "Content-Type");
          return;
        }
      }
    }
    const auto t0 = std::chrono::steady_clock::now();
    dispatch_started = true;
    Dispatch(fd, method, path, body);
    static const bool access_log = std::getenv("Q4T_ACCESS_LOG") != nullptr;
    if (access_log) {
      const double ms = std::chrono::duration<double, std::milli>(
                            std::chrono::steady_clock::now() - t0)
                            .count();
      std::fprintf(stderr, "[q4t][http] trace_id=%s elapsed_ms=%.1f\n",
                   context.TraceId().c_str(), ms);
    }
  } catch (const std::bad_alloc&) {
    if (!dispatch_started)
      context.RecordReceiveFailure(HttpReceiveFailure::kException, 503);
    detail::SendExceptionResponse(fd, *context.State(),
                                  detail::ExceptionKind::kHostMemory);
  } catch (...) {
    if (!dispatch_started)
      context.RecordReceiveFailure(HttpReceiveFailure::kException, 500);
    detail::SendExceptionResponse(fd, *context.State());
  }
}

void ChatServer::Dispatch(int fd, const std::string& method,
                          const std::string& path, const std::string& body) {
  const auto rejected = [&](HttpReceiveFailure reason, int status) {
    if (auto* context = detail::HttpResponseContext::Current(fd))
      context->RecordReceiveFailure(reason, status);
  };
  if (path.find('?') != path.npos) {
    rejected(HttpReceiveFailure::kQuery, 400);
    SendError(fd, 400, "query parameters are not supported", {},
              "unsupported_query", "path");
    return;
  }
  std::string_view expected;
  if (path == "/healthz" || path == "/livez" || path == "/readyz" ||
      path == "/v1/models" || path == "/v1/capabilities" ||
      path == "/v1/config" || path == "/metrics")
    expected = "GET";
  else if (path == "/v1/chat/completions" || path == "/v1/chat/preflight" ||
           path == "/v1/requests/cancel")
    expected = "POST";
  if (!expected.empty() && method != expected) {
    rejected(HttpReceiveFailure::kMethod, 405);
    SendError(fd, 405, "method not allowed", {}, "method_not_allowed", "method",
              expected);
    return;
  }
  if (expected == "GET" && !body.empty()) {
    rejected(HttpReceiveFailure::kUnexpectedBody, 400);
    SendError(fd, 400, "GET body is not supported", {}, "unexpected_body",
              "body");
    return;
  }
  if (path == "/livez" && method == "GET") {
    HandleReadiness(fd, true);
    return;
  }
  if (path == "/readyz" && method == "GET") {
    HandleReadiness(fd, false);
    return;
  }
  if (path == "/healthz" && method == "GET") {
    HandleHealth(fd);
    return;
  }
  if (path == "/v1/models" && method == "GET") {
    HandleModels(fd);
    return;
  }
  if (path == "/v1/capabilities" && method == "GET") {
    HandleCapabilities(fd);
    return;
  }
  if (path == "/v1/config" && method == "GET") {
    HandleConfig(fd);
    return;
  }
  if (path == "/v1/chat/preflight" && method == "POST") {
    HandlePreflight(fd, body);
    return;
  }
  if (path == "/metrics" && method == "GET") {
    HandleMetrics(fd);
    return;
  }
  if (path == "/v1/requests/cancel" && method == "POST") {
    io::Json req;
    if (!io::ParseJson(body, &req, true, kRequestJsonLimits).ok() ||
        !ValidateCancelContract(req)) {
      SendError(fd, 400, "invalid cancellation request", {},
                "invalid_cancellation");
      return;
    }
    const auto* context = detail::HttpResponseContext::Current(fd);
    const auto client = context ? context->Client().Id() : std::string_view();
    if (!requests_.Cancel(req.GetString("request_id"),
                          req.GetString("cancel_token"), client)) {
      SendError(fd, 404, "no cancellable request with these credentials", {},
                "request_not_found");
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
  rejected(HttpReceiveFailure::kRoute, 404);
  SendError(fd, 404, "route or method not found");
}

void ChatServer::HandleReadiness(int fd, bool liveness) {
  const bool draining = stop_requested_.load();
  const bool live = !serving_failed_.load();
  const auto memory = memory_admission_.Observe();
  const bool ready =
      live && model_ready_.load() && gpu_healthy_.load() && !draining &&
      memory.Allowed();
  const bool success = liveness ? live : ready;
  const char* state = !live || !gpu_healthy_.load() ? "unhealthy"
                      : draining                    ? "draining"
                      : !model_ready_.load()        ? "starting"
                      : !memory.Allowed()           ? memory.StateName()
                                                    : "ready";
  SendSimple(fd, success ? 200 : 503, success ? "OK" : "Service Unavailable",
             std::string("{\"status\":\"") + state +
                 "\",\"live\":" + (live ? "true" : "false") +
                 ",\"ready\":" + (ready ? "true" : "false") + "}",
             "application/json");
}

}  // namespace q4t::server
