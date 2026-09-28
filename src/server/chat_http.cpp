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

// Escape a string for embedding inside a JSON string literal.
std::string JsonEscape(const std::string& s) {
  std::string out;
  out.reserve(s.size() + 8);
  for (unsigned char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += static_cast<char>(c);
        }
    }
  }
  return out;
}

bool WriteAll(int fd, const char* data, size_t len) {
  size_t off = 0;
  while (off < len) {
    // A disconnected client must fail this request, not terminate the server.
    const ssize_t n = ::send(fd, data + off, len - off, MSG_NOSIGNAL);
    if (n <= 0) {
      if (n < 0 && errno == EINTR) continue;
      return false;
    }
    off += static_cast<size_t>(n);
  }
  return true;
}

bool WriteAll(int fd, const std::string& s) {
  return WriteAll(fd, s.data(), s.size());
}


void SendSimple(int fd, int code, const char* status, const std::string& body,
                const std::string& content_type) {
  std::string resp = "HTTP/1.1 " + std::to_string(code) + " " + status +
                     "\r\nContent-Type: " + content_type +
                     "\r\nContent-Length: " + std::to_string(body.size()) +
                     "\r\nConnection: close\r\n\r\n" +
                     body;
  WriteAll(fd, resp);
}

void SendError(int fd, int code, const std::string& message) {
  const std::string body =
      "{\"error\":{\"message\":\"" + JsonEscape(message) +
      "\",\"type\":\"invalid_request_error\",\"code\":null}}";
  SendSimple(fd, code, "Bad Request", body, "application/json");
}

// OpenAI SSE data chunk for a streaming completion.
std::string SseChunk(const std::string& id, const std::string& model,
                     const std::string& delta_role, const std::string& content,
                     const std::string& finish_reason, int index) {
  std::string choice;
  choice += "{\"index\":" + std::to_string(index) + ",\"delta\":{";
  if (!delta_role.empty()) {
    choice += "\"role\":\"" + delta_role + "\",";
  }
  choice += "\"content\":\"" + JsonEscape(content) + "\"}";
  if (!finish_reason.empty()) {
    choice += ",\"finish_reason\":\"" + finish_reason + "\"";
  } else {
    choice += ",\"finish_reason\":null";
  }
  choice += "}";
  std::string obj =
      "{\"id\":\"" + id + "\",\"object\":\"chat.completion.chunk\","
      "\"created\":" +
      std::to_string(std::time(nullptr)) + ",\"model\":\"" + model +
      "\",\"choices\":[" + choice + "]}";
  return "data: " + obj + "\r\n\r\n";
}

}

Status ChatServer::Run() {
  int listen_fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listen_fd < 0) {
    return Status::Fail(std::string("socket: ") + std::strerror(errno));
  }
  int one = 1;
  ::setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  if (::inet_pton(AF_INET, host_.c_str(), &addr.sin_addr) != 1) {
    ::close(listen_fd);
    return Status::Fail("invalid listen address");
  }
  addr.sin_port = htons(static_cast<uint16_t>(port_));
  if (::bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) <
      0) {
    const std::string err = std::strerror(errno);
    ::close(listen_fd);
    return Status::Fail("bind: " + err);
  }
  if (::listen(listen_fd, 128) < 0) {
    const std::string err = std::strerror(errno);
    ::close(listen_fd);
    return Status::Fail("listen: " + err);
  }
  listen_fd_ = listen_fd;  // RequestStop() shutdown(2)s this to break accept
  std::fprintf(stderr, "[q4t] serving on port %d (host %s, model %s)\n",
               port_, host_.c_str(), model_name_.c_str());
  Status run_status;
  while (true) {
    sockaddr_in client{};
    socklen_t clen = sizeof(client);
    const int fd =
        ::accept(listen_fd, reinterpret_cast<sockaddr*>(&client), &clen);
    if (fd < 0) {
      if (stop_requested_.load()) break;  // RequestStop() shutdown() the socket
      if (errno == EINTR) continue;
      run_status = Status::Fail(std::string("accept: ") + std::strerror(errno));
      break;  // Fatal accept errors must drain existing handlers too.
    }
    int nodelay = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
    // Read/write timeouts: a slow or stalled client (slowloris) must not pin a
    // request thread + seq slot indefinitely. Each blocking recv/send fails
    // after 30 s of no progress (well above a legitimate inter-write gap), so
    // ReadRequest / WriteAll return and the slot frees.
    struct timeval tv {
      30, 0
    };
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    // B1: handle each client on its own thread so multiple requests are
    // processed concurrently. The model forwards are serialized behind
    // model_mu_ (see HandleChat); CPU post-processing (D2H/argmax/tokenize/SSE)
    // overlaps across requests. Run() blocks in the accept loop for the
    // server's lifetime, so the ChatServer object outlives the detached
    // request threads.
    // Bound in-flight request threads: shed load (503) beyond conn_cap_ so a
    // connection flood cannot exhaust host memory with blocked threads (the
    // AllocSeqId queue absorbs bursts up to this cap).
    if (active_conns_.fetch_add(1, std::memory_order_relaxed) >= conn_cap_) {
      active_conns_.fetch_sub(1, std::memory_order_relaxed);
      SendSimple(fd, 503, "Service Unavailable",
                 "{\"error\":{\"message\":\"server overloaded, retry\"}}",
                 "application/json");
      ::close(fd);
      continue;
    }
    {
      const std::lock_guard<std::mutex> lock(connections_mu_);
      connections_.insert(fd);
    }
    std::thread([this, fd]() {
      HandleClient(fd);
      {
        const std::lock_guard<std::mutex> lock(connections_mu_);
        connections_.erase(fd);
        ::close(fd);
      }
      // Publish all handler cleanup before Run observes zero and destroys
      // server-owned state. This decrement is the thread's last access.
      active_conns_.fetch_sub(1, std::memory_order_release);
    }).detach();
  }
  // Graceful shutdown (RequestStop broke the accept loop): stop the scheduler
  // (in-flight decodes get next_token=-1 so their request threads exit),
  // release queued seq waiters, then wait for in-flight request threads to
  // drain before returning — they are detached and cannot be joined, so the
  // destructor's frees must not race a live request thread.
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
  // All socket reads/writes were interrupted above. Never destroy model or
  // scheduler state while a detached request thread still owns it.
  while (active_conns_.load(std::memory_order_acquire) > 0)
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  listen_fd_.store(-1, std::memory_order_relaxed);
  ::close(listen_fd);
  std::fprintf(stderr, "[q4t] shutdown complete (%d in-flight remaining)\n",
               active_conns_.load(std::memory_order_relaxed));
  return run_status;
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
