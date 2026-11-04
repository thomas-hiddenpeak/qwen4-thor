// Actual server preparation paths under contention; never loads a model.
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <future>
#include <mutex>
#include <string>
#include <thread>

#include "q4t/server/chat_server.h"

namespace {
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
constexpr char kToken[] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                          "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

void Require(bool ok, const char* message) {
  if (ok) return;
  std::fprintf(stderr, "prepare-lock probe failed: %s\n", message);
  // Do not unlock a failed waiter into an intentionally unloaded tokenizer.
  std::fflush(nullptr);
  std::_Exit(1);
}
}  // namespace

namespace q4t::server {
struct ServeContractAccess {
  static void Configure(ChatServer& server, bool scoped) {
    server.model_name_ = "qwen3.8-flash-next";
    server.max_len_ = 8192;
    server.max_seq_ = 1;
    if (!scoped) return;
    GatewayClientPolicy policy;
    policy.gateway_token = std::string(64, 'a');
    policy.clients.push_back({"alpha", 1, 2, 4096, 512, 131072});
    Require(server.clients_.Configure(std::move(policy)).ok(), "policy");
  }
  static std::mutex& Mutex(ChatServer& server) { return server.tok_mu_; }
  static void Handle(ChatServer& server, int fd) { server.HandleClient(fd); }
  static bool Active(ChatServer& server) {
    return server.active_chats_.load() == 1;
  }
  static void Stop(ChatServer& server) {
    server.RequestStop();
    // Run performs this transition when it observes RequestStop.
    server.requests_.CancelAll();
  }
  static void CheckReleased(ChatServer& server, bool preflight, bool scoped) {
    Require(server.active_chats_.load() == 0 &&
                server.body_budget_.Used() == 0 &&
                server.business_conns_.load() == 0 &&
                server.control_conns_.load() == 0 &&
                server.metrics_.structured_active.load() == 0,
            "request resources retained");
    Require(!server.requests_.Cancel("prepare-wait", kToken,
                                    scoped ? "alpha" : ""),
            "request registration retained");
    if (!preflight) {
      Require(server.metrics_.requests_aborted.load() == 1 &&
                  server.metrics_.requests_error.load() == 0 &&
                  server.metrics_.requests_success.load() == 0,
              "cancellation accounting");
    }
    if (scoped) {
      const auto usage = server.clients_.Usage(0);
      Require(usage.data_active == 0 && usage.control_active == 0,
              "client lease retained");
    }
  }
};
}  // namespace q4t::server

namespace {
using Access = q4t::server::ServeContractAccess;
using Server = q4t::server::ChatServer;

void Send(int fd, const std::string& path, const std::string& body,
          bool scoped) {
  std::string wire = "POST " + path +
                     " HTTP/1.1\r\nHost: localhost\r\n"
                     "Content-Type: application/json\r\nConnection: close\r\n";
  if (scoped)
    wire += "X-Q4T-Client-Id: alpha\r\nX-Q4T-Gateway-Token: " +
            std::string(64, 'a') + "\r\n";
  wire += "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n" + body;
  size_t offset = 0;
  while (offset < wire.size()) {
    const auto size =
        send(fd, wire.data() + offset, wire.size() - offset, MSG_NOSIGNAL);
    Require(size > 0, "request send");
    offset += size;
  }
}

std::string Read(int fd) {
  std::string wire;
  char data[4096];
  for (;;) {
    const auto size = recv(fd, data, sizeof(data), 0);
    Require(size >= 0, "response read");
    if (!size) return wire;
    wire.append(data, size);
    Require(wire.size() < 65536, "response budget");
  }
}

void ExplicitCancel(Server& server, bool scoped) {
  int pair[2];
  Require(socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0, "cancel socket");
  Send(pair[0], "/v1/requests/cancel",
       std::string(R"({"request_id":"prepare-wait","cancel_token":")") +
           kToken + "\"}",
       scoped);
  Access::Handle(server, pair[1]);
  close(pair[1]);
  const auto wire = Read(pair[0]);
  close(pair[0]);
  Require(wire.starts_with("HTTP/1.1 202"), "cancel route rejected");
}

void Case(const std::string& mode, const std::string& cause) {
  const bool preflight = mode == "preflight";
  const bool thinking = mode.starts_with("thinking");
  const bool scoped = mode == "scoped-sse";
  const bool stream = mode.ends_with("sse");
  Server server;
  Access::Configure(server, scoped);
  std::unique_lock owner(Access::Mutex(server));
  int pair[2];
  Require(socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0, "chat socket");
  std::string body =
      R"({"model":"qwen3.8-flash-next",)"
      R"("messages":[{"role":"user","content":"hello"}],)";
  body += std::string(R"("request_id":"prepare-wait","cancel_token":")") +
      kToken + R"(","max_tokens":64,"request_timeout_ms":)" +
      std::string(cause == "deadline" ? "200" : "5000") + R"(,"stream":)" +
      (stream ? "true" : "false");
  if (thinking) body += R"(,"thinking_token_budget":0)";
  body += '}';
  if (preflight) body = "{\"request\":" + body + '}';
  Send(pair[0], preflight ? "/v1/chat/preflight" : "/v1/chat/completions", body,
       scoped);
  auto waiter = std::async(std::launch::async, [&] {
    Access::Handle(server, pair[1]);
    close(pair[1]);
  });
  const auto until = Clock::now() + 1s;
  while (!Access::Active(server)) {
    Require(waiter.wait_for(0ms) != std::future_status::ready &&
                Clock::now() < until,
            "handler did not enter preparation");
    std::this_thread::sleep_for(1ms);
  }
  Require(waiter.wait_for(50ms) != std::future_status::ready,
          "handler escaped held tokenizer");
  const auto cancelled_at = Clock::now();
  if (cause == "explicit") ExplicitCancel(server, scoped);
  if (cause == "disconnect") {
    close(pair[0]);
    pair[0] = -1;
  }
  if (cause == "shutdown") Access::Stop(server);
  Require(waiter.wait_for(1s) == std::future_status::ready,
          "cancelled waiter requires owner release");
  waiter.get();
  const auto seconds =
      std::chrono::duration<double>(Clock::now() - cancelled_at);
  // The original owner is still holding its mutex throughout response/cleanup.
  Require(owner.owns_lock(), "original owner lost its lock");
  Access::CheckReleased(server, preflight, scoped);
  if (pair[0] >= 0) {
    const auto wire = Read(pair[0]);
    close(pair[0]);
    const bool sse = stream && !thinking && !scoped;
    Require(wire.starts_with(sse ? "HTTP/1.1 200" : "HTTP/1.1 409"),
            "unexpected HTTP status");
    const std::string reason = cause == "deadline"   ? "deadline_exceeded"
                               : cause == "shutdown" ? "server_shutdown"
                                                     : "explicit_cancel";
    Require(wire.find("\"code\":\"" + reason + "\"") != std::string::npos,
            "terminal reason");
    if (!preflight)
      Require(wire.find(thinking
                            ? "waiting for thinking tokenizer"
                            : "cancelled during encoding") != std::string::npos,
              "wrong preparation branch");
    const auto done = wire.find("data: [DONE]");
    Require(sse ? done != std::string::npos &&
                      wire.find("data: [DONE]", done + 1) == std::string::npos
                : done == std::string::npos,
            "terminal framing");
    Require(wire.find("\"finish_reason\":\"stop\"") == std::string::npos,
            "false successful generation");
  }
  owner.unlock();
  Require(Access::Mutex(server).try_lock(), "mutex not reusable");
  Access::Mutex(server).unlock();
  std::printf(
      "{\"mode\":\"%s\",\"cause\":\"%s\",\"passed\":true,"
      "\"returned_before_owner_release\":true,\"seconds\":%.6f}\n",
      mode.c_str(), cause.c_str(), seconds.count());
  std::fflush(stdout);
}
}  // namespace

int main() {
  for (const auto* mode : {"encode-json", "encode-sse", "thinking-json",
                           "thinking-sse", "scoped-sse", "preflight"})
    for (const auto* cause : {"explicit", "deadline", "disconnect", "shutdown"})
      if (!(std::string(mode) == "preflight" &&
            std::string(cause) == "explicit"))
        Case(mode, cause);
}
