// Production HTTP accept/dispatch/connection teardown with an unloaded fake
// backend. Model/GPU resources stay empty; no CUDA API is executed. The IO
// library still carries its normal CUDA runtime link dependency.
#include "q4t/server/chat_server.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/serve_fault.h"
#include "q4t/test.h"
#include "contract_access.h"

#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <future>
#include <filesystem>
#include <stdexcept>

namespace q4t::model {
ModelOwner::~ModelOwner() = default;
DecodeMetadataUpload::~DecodeMetadataUpload() = default;
MetadataUploadBatch::~MetadataUploadBatch() = default;
}  // namespace q4t::model
namespace q4t::trace {
RouterCollector::~RouterCollector() = default;
}  // namespace q4t::trace
namespace q4t::server {
ChatServer::~ChatServer() = default;
SessionCache::~SessionCache() = default;
SessionCache::Entry::~Entry() = default;
void ChatServer::StopScheduler() {}
void ChatServer::HandleChat(int, const std::string&) { throw std::bad_alloc(); }
void ChatServer::HandlePreflight(int, const std::string&) {
  throw std::bad_alloc();  // Lifecycle fixture only, not planning acceptance.
}
void ChatServer::HandleConfig(int fd) {
  detail::SendError(fd, 501, "lifecycle fixture has no configuration");
}
void ChatServer::HandleHealth(int fd) {
  detail::SendSimple(fd, 200, "OK", "ok", "text/plain");
}
void ChatServer::HandleModels(int fd) { HandleHealth(fd); }
void ChatServer::HandleCapabilities(int fd) { HandleHealth(fd); }
void ChatServer::HandleMetrics(int fd) { HandleHealth(fd); }

}  // namespace q4t::server

namespace {
using namespace std::chrono_literals;
using q4t::server::ChatServer;
using q4t::server::ServeContractAccess;
using q4t::server::ScopeExit;
ChatServer* publishing_server = nullptr;

int Connect(ChatServer& server) {
  int port = -1;
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while ((port = ServeContractAccess::Port(server)) < 0 &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  if (port < 0) throw std::runtime_error("listener publication timed out");
  const int fd = socket(AF_INET, SOCK_STREAM, 0);
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  addr.sin_port = htons(port);
  if (connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr))) {
    close(fd);
    throw std::runtime_error("connect failed");
  }
  const timeval timeout{3, 0};
  setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  return fd;
}

std::string Request(ChatServer& server, const std::string& request) {
  const int fd = Connect(server);
  ScopeExit close_socket([&] { close(fd); });
  if (!q4t::server::detail::WriteAll(fd, request))
    throw std::runtime_error("request send failed");
  std::string response;
  char data[1024];
  for (;;) {
    const auto n = recv(fd, data, sizeof(data), 0);
    if (n <= 0) break;
    response.append(data, static_cast<size_t>(n));
  }
  return response;
}
}  // namespace

Q4T_TEST(http_stop_before_and_during_listener_publication) {
  for (int i = 0; i < 2; ++i) {
    ChatServer server;
    ServeContractAccess::Configure(server);
    if (i == 0) server.RequestStop();
    publishing_server = &server;
    q4t::server::detail::serve_test_hook = +[](const char* name) {
      if (std::string_view(name) == "connection.before_publish")
        publishing_server->RequestStop();
    };
    ScopeExit clear_hook([] {
      q4t::server::detail::serve_test_hook = nullptr;
      publishing_server = nullptr;
    });
    auto run = std::async(std::launch::async, [&] { return server.Run(); });
    Q4T_CHECK(run.wait_for(3s) == std::future_status::ready);
    Q4T_CHECK(run.get().ok() && ServeContractAccess::Empty(server));
  }
  return true;
}

Q4T_TEST(http_exception_keeps_server_and_releases_connections) {
  ChatServer server;
  ServeContractAccess::Configure(server);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  const auto failed =
      Request(server,
              "POST /v1/chat/completions HTTP/1.1\r\nHost: test\r\n"
              "Content-Type: application/json\r\nX-Request-Id: exception-1\r\n"
              "Content-Length: 2\r\n\r\n{}");
  const auto healthy = Request(server, "GET /healthz HTTP/1.1\r\n"
                                       "Host: test\r\n\r\n");
  stop.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready);
  Q4T_CHECK(run.get().ok() && ServeContractAccess::Empty(server));
  Q4T_CHECK(failed.starts_with("HTTP/1.1 500 "));
  Q4T_CHECK(failed.find("X-Request-Id: exception-1\r\n") != failed.npos);
  Q4T_CHECK(healthy.starts_with("HTTP/1.1 200 "));
  return true;
}

Q4T_TEST(http_cancel_validates_before_registry_mutation) {
  ChatServer server;
  ServeContractAccess::Configure(server);
  const std::string token(64, 'a');
  auto control = ServeContractAccess::Register(server, token);
  Q4T_CHECK(control != nullptr);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  const std::string valid = R"({"request_id":"cancel-contract","cancel_token":")" +
                            token + R"("})";
  const auto post = [&](const std::string& body) {
    return Request(server, "POST /v1/requests/cancel HTTP/1.1\r\nHost: test\r\n"
        "Content-Type: application/json\r\nContent-Length: " +
        std::to_string(body.size()) + "\r\n\r\n" + body);
  };
  for (const auto& body : std::initializer_list<std::string>{
      valid.substr(0, valid.size() - 1) + R"(,"dry_run":true})",
      valid.substr(0, valid.size() - 1) + R"(,"reason":"test"})",
      R"({"cancel_token":")" + token + R"("})",
      R"({"request_id":null,"cancel_token":")" + token + R"("})"}) {
    const auto response = post(body);
    Q4T_CHECK(response.starts_with("HTTP/1.1 400 "));
    Q4T_CHECK(response.find("invalid_cancellation") != response.npos);
    Q4T_CHECK(!control->Cancelled());
  }
  const auto unknown = post(R"({"request_id":"cancel-contract","cancel_token":")" +
                            std::string(64, 'b') + R"("})");
  Q4T_CHECK(unknown.starts_with("HTTP/1.1 404 "));
  Q4T_CHECK(!control->Cancelled());
  const auto accepted = post(valid);
  Q4T_CHECK(accepted.starts_with("HTTP/1.1 202 "));
  Q4T_CHECK(control->Cancelled());
  stop.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready);
  Q4T_CHECK(run.get().ok() && ServeContractAccess::Empty(server));
  return true;
}

Q4T_TEST(http_thread_creation_failure_rolls_back) {
  setenv("Q4T_SERVE_FAULT", "connection.before_thread", 1);
  ScopeExit environment([] { unsetenv("Q4T_SERVE_FAULT"); });
  ChatServer server;
  ServeContractAccess::Configure(server);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  const int fd = Connect(server);
  ScopeExit close_socket([&] { close(fd); });
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready);
  Q4T_CHECK(!run.get().ok() && ServeContractAccess::Empty(server));
  char data;
  Q4T_CHECK(recv(fd, &data, 1, 0) == 0);
  return true;
}

Q4T_TEST(http_validation_gate_is_an_explicit_handshake) {
  namespace fs = std::filesystem;
  const auto directory = fs::absolute(".q4t-work/serve-gate-cpu-" +
                                      std::to_string(getpid()));
  Q4T_CHECK(fs::create_directory(directory));
  const auto gate = (directory / "gate").string();
  setenv("Q4T_SERVE_PAUSE", "handler.before_response", 1);
  setenv("Q4T_SERVE_GATE", gate.c_str(), 1);
  ScopeExit cleanup([&] {
    unsetenv("Q4T_SERVE_PAUSE");
    unsetenv("Q4T_SERVE_GATE");
    fs::remove_all(directory);
  });
  auto paused = std::async(std::launch::async, [] {
    q4t::server::detail::ServeFaultPoint("handler.before_response");
  });
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while (!fs::exists(gate + ".entered") &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(fs::exists(gate + ".entered"));
  Q4T_CHECK(paused.wait_for(0ms) == std::future_status::timeout);
  const int fd = ::open((gate + ".release").c_str(), O_CREAT | O_WRONLY, 0600);
  Q4T_CHECK(fd >= 0);
  close(fd);
  Q4T_CHECK(paused.wait_for(3s) == std::future_status::ready);
  paused.get();
  return true;
}

Q4T_TEST(http_methods_media_queries_and_trace_headers) {
  ChatServer server;
  ServeContractAccess::Configure(server);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  const auto wrong_method = Request(
      server,
      "POST /v1/models HTTP/1.1\r\nHost: t\r\nX-Request-Id: route-1\r\n\r\n");
  Q4T_CHECK(wrong_method.starts_with("HTTP/1.1 405 "));
  Q4T_CHECK(wrong_method.find("Allow: GET\r\n") != wrong_method.npos);
  Q4T_CHECK(wrong_method.find("X-Request-Id: route-1\r\n") !=
            wrong_method.npos);
  Q4T_CHECK(Request(server, "GET /v1/models?x=1 HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 400 "));
  Q4T_CHECK(Request(server, "GET /absent HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 404 "));
  for (const char* type :
       {"", "Content-Type: text/plain\r\n",
        "Content-Type: application/json; charset=utf-16\r\n"}) {
    Q4T_CHECK(Request(server,
                      std::string(
                          "POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n") +
                          type + "Content-Length: 2\r\n\r\n{}")
                  .starts_with("HTTP/1.1 415 "));
  }
  Q4T_CHECK(Request(server,
                    "POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
                    "Content-Type: APPLICATION/JSON; charset=\"UTF-8\"\r\n"
                    "Content-Length: 2\r\n\r\n{}")
                .starts_with("HTTP/1.1 500 "));
  const auto duplicate =
      Request(server,
              "GET /v1/models HTTP/1.1\r\nHost: t\r\nX-Request-Id: first\r\n"
              "X-Request-Id: second\r\n\r\n");
  Q4T_CHECK(duplicate.starts_with("HTTP/1.1 400 "));
  Q4T_CHECK(duplicate.find("X-Request-Id: req-") != duplicate.npos);
  stop.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready && run.get().ok());
  Q4T_CHECK(ServeContractAccess::Empty(server));
  return true;
}

Q4T_TEST(http_control_reserve_and_draining_readiness) {
  ChatServer server;
  ServeContractAccess::Configure(server);
  const std::string token(64, 'a');
  auto control = ServeContractAccess::Register(server, token);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  const int a = Connect(server), b = Connect(server);
  ScopeExit close_slow([&] {
    close(a);
    close(b);
  });
  const std::string header =
      "POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
      "Content-Type: application/json\r\nContent-Length: 8\r\n\r\n";
  Q4T_CHECK(q4t::server::detail::WriteAll(a, header));
  Q4T_CHECK(q4t::server::detail::WriteAll(b, header));
  const auto until = std::chrono::steady_clock::now() + 1s;
  while (ServeContractAccess::Business(server) != 2 &&
         std::chrono::steady_clock::now() < until)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(ServeContractAccess::Business(server) == 2);
  Q4T_CHECK(Request(server, header).starts_with("HTTP/1.1 503 "));
  const std::string ready = "GET /readyz HTTP/1.1\r\nHost: t\r\n\r\n";
  Q4T_CHECK(Request(server, ready).starts_with("HTTP/1.1 200 "));
  ServeContractAccess::Healthy(server, false);
  Q4T_CHECK(Request(server, ready).starts_with("HTTP/1.1 503 "));
  Q4T_CHECK(Request(server, "GET /livez HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 200 "));
  ServeContractAccess::Healthy(server, true);
  const std::string body =
      R"({"request_id":"cancel-contract","cancel_token":")" + token + "\"}";
  Q4T_CHECK(Request(server,
                    "POST /v1/requests/cancel HTTP/1.1\r\nHost: t\r\n"
                    "Content-Type: application/json\r\nContent-Length: " +
                        std::to_string(body.size()) + "\r\n\r\n" + body)
                .starts_with("HTTP/1.1 202 "));
  Q4T_CHECK(control->Cancelled());
  stop.Run();
  const auto draining = Request(server, ready);
  Q4T_CHECK(draining.starts_with("HTTP/1.1 503 "));
  Q4T_CHECK(draining.find("\"status\":\"draining\"") != draining.npos);
  const auto rejected = Request(server, header);
  Q4T_CHECK(rejected.starts_with("HTTP/1.1 503 "));
  Q4T_CHECK(rejected.find("server_draining") != rejected.npos);
  close_slow.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready && run.get().ok());
  Q4T_CHECK(ServeContractAccess::Empty(server));
  return true;
}
