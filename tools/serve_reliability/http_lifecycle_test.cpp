// Production HTTP accept/dispatch/connection teardown with an unloaded fake
// backend. Model/GPU resources stay empty; no CUDA API is executed. The IO
// library still carries its normal CUDA runtime link dependency.
#include "q4t/server/chat_server.h"
#include "q4t/server/device_memory.h"
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
#include <cerrno>
#include <cstring>

namespace {
std::atomic<bool> observation_failure{false};
}

Q4T_TEST(http_fatal_device_memory_before_run_fails_without_listener) {
  using namespace q4t::server;
  ChatServer server;
  ServeContractAccess::DeviceFailure(server, {DeviceMemoryState::kFailure, 700});
  Q4T_CHECK(!server.Run().ok());
  Q4T_CHECK(ServeContractAccess::Empty(server));
  return true;
}

namespace q4t::model {
ModelOwner::~ModelOwner() = default;
DecodeMetadataUpload::~DecodeMetadataUpload() = default;
MetadataUploadBatch::~MetadataUploadBatch() = default;
}  // namespace q4t::model
namespace q4t::vision {
VisionTower::~VisionTower() = default;  // Always empty in this fake backend.
}
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
void ChatServer::HandleMetrics(int fd) {
  if (observation_failure.exchange(false)) throw std::bad_alloc();
  HandleHealth(fd);
}

}  // namespace q4t::server

namespace {
using namespace std::chrono_literals;
using q4t::server::ChatServer;
using q4t::server::ServeContractAccess;
using q4t::server::ScopeExit;
ChatServer* publishing_server = nullptr;
std::atomic<int64_t> memory_available{1000};
q4t::runtime::SystemMemory ReadMemory() {
  const auto available = memory_available.load();
  if (available < 0) return {};
  return {true, 10000, static_cast<uint64_t>(available)};
}

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

Q4T_TEST(http_observation_partition_business_and_control_isolation) {
  using namespace q4t::server;
  ChatServer server;
  ServeContractAccess::Configure(server);
  const std::string token(64, 'a');
  auto control = ServeContractAccess::Register(server, token);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  std::vector<int> held;
  ScopeExit close_held([&] { for (int fd : held) if (fd >= 0) close(fd); });
  const auto hold = [&](const std::string& wire) {
    const int fd = Connect(server);
    held.push_back(fd);
    return detail::WriteAll(fd, wire);
  };
  const auto wait = [&](auto predicate) {
    const auto end = std::chrono::steady_clock::now() + 1s;
    while (!predicate() && std::chrono::steady_clock::now() < end)
      std::this_thread::sleep_for(1ms);
    return predicate();
  };
  const std::string business =
      "POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
      "Content-Type: application/json\r\nContent-Length: 1\r\n\r\n";
  Q4T_CHECK(hold(business) && hold(business));
  Q4T_CHECK(wait([&] { return ServeContractAccess::Business(server) == 2; }));
  Q4T_CHECK(Request(server, business).starts_with("HTTP/1.1 503 "));
  for (const char* path : {"/metrics", "/v1/models", "/v1/capabilities"})
    Q4T_CHECK(Request(server, std::string("GET ") + path +
        " HTTP/1.1\r\nHost: t\r\n\r\n").starts_with("HTTP/1.1 200 "));
  // Config's fixture returns 501, proving it reached dispatch, not capacity.
  Q4T_CHECK(Request(server, "GET /v1/config HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 501 "));
  for (int i = 0; i < 4; ++i)
    Q4T_CHECK(hold("GET /metrics HTTP/1.1\r\nHost: t\r\n"
                   "Content-Length: 1\r\n\r\n"));
  Q4T_CHECK(wait([&] { return ServeContractAccess::Observation(server) == 4; }));
  const auto full = Request(server,
      "GET /metrics HTTP/1.1\r\nHost: t\r\n\r\n");
  Q4T_CHECK(full.starts_with("HTTP/1.1 503 ") &&
            full.find("observation_capacity_exhausted") != full.npos);
  Q4T_CHECK(Request(server, "GET /readyz HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 200 "));
  const std::string body =
      R"({"request_id":"cancel-contract","cancel_token":")" + token + "\"}";
  Q4T_CHECK(Request(server,
      "POST /v1/requests/cancel HTTP/1.1\r\nHost: t\r\n"
      "Content-Type: application/json\r\nContent-Length: " +
      std::to_string(body.size()) + "\r\n\r\n" + body)
                .starts_with("HTTP/1.1 202 "));
  Q4T_CHECK(control->Cancelled());
  Q4T_CHECK(ServeContractAccess::ConnectionRejected(
      server, HttpConnectionClass::kBusiness) == 1);
  Q4T_CHECK(ServeContractAccess::ConnectionRejected(
      server, HttpConnectionClass::kObservation) == 1);
  Q4T_CHECK(ServeContractAccess::ConnectionRejected(
      server, HttpConnectionClass::kControl) == 0);
  for (size_t i = 2; i < held.size(); ++i) {
    close(held[i]);
    held[i] = -1;
  }
  Q4T_CHECK(wait([&] { return ServeContractAccess::Observation(server) == 0; }));
  Q4T_CHECK(Request(server, "GET /metrics HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 200 "));
  close_held.Run();
  stop.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready && run.get().ok());
  Q4T_CHECK(ServeContractAccess::Empty(server));
  return true;
}

Q4T_TEST(http_observation_independent_of_saturated_control_partition) {
  using namespace q4t::server;
  ChatServer server;
  ServeContractAccess::Configure(server);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  std::vector<int> held;
  ScopeExit close_held([&] { for (int fd : held) close(fd); });
  for (int i = 0; i < 4; ++i) {
    held.push_back(Connect(server));
    Q4T_CHECK(detail::WriteAll(held.back(),
        "POST /v1/requests/cancel HTTP/1.1\r\nHost: t\r\n"
        "Content-Type: application/json\r\nContent-Length: 1\r\n\r\n"));
  }
  const auto end = std::chrono::steady_clock::now() + 1s;
  while (ServeContractAccess::Control(server) != 4 &&
         std::chrono::steady_clock::now() < end)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(ServeContractAccess::Control(server) == 4);
  Q4T_CHECK(Request(server, "GET /readyz HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 503 "));
  Q4T_CHECK(Request(server, "GET /metrics HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 200 "));
  Q4T_CHECK(Request(server, "GET /unknown HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 404 "));
  Q4T_CHECK(ServeContractAccess::ConnectionRejected(
      server, HttpConnectionClass::kControl) == 1);
  close_held.Run();
  stop.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready && run.get().ok());
  Q4T_CHECK(ServeContractAccess::Empty(server));
  return true;
}

Q4T_TEST(http_unclassified_total_limit_is_explicit_and_recoverable) {
  using namespace q4t::server;
  ChatServer server;
  ServeContractAccess::Configure(server);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  std::vector<int> held;
  ScopeExit close_held([&] { for (int fd : held) close(fd); });
  for (int i = 0; i < 10; ++i) held.push_back(Connect(server));
  const auto end = std::chrono::steady_clock::now() + 1s;
  while (ServeContractAccess::ActiveConnections(server) != 10 &&
         std::chrono::steady_clock::now() < end)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(ServeContractAccess::ActiveConnections(server) == 10);
  for (const char* path : {"/metrics", "/readyz"})
    Q4T_CHECK(Request(server, std::string("GET ") + path +
        " HTTP/1.1\r\nHost: t\r\n\r\n").starts_with("HTTP/1.1 503 "));
  Q4T_CHECK(ServeContractAccess::Business(server) == 0 &&
            ServeContractAccess::Observation(server) == 0 &&
            ServeContractAccess::Control(server) == 0);
  Q4T_CHECK(ServeContractAccess::ConnectionRejected(
      server, HttpConnectionClass::kUnclassified) == 2);
  close_held.Run();
  const auto cleared = std::chrono::steady_clock::now() + 1s;
  while (ServeContractAccess::ActiveConnections(server) != 0 &&
         std::chrono::steady_clock::now() < cleared)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(ServeContractAccess::ActiveConnections(server) == 0);
  Q4T_CHECK(Request(server, "GET /metrics HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 200 "));
  stop.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready && run.get().ok());
  Q4T_CHECK(ServeContractAccess::Empty(server));
  return true;
}

Q4T_TEST(http_observation_draining_and_exception_release) {
  using namespace q4t::server;
  ChatServer server;
  ServeContractAccess::Configure(server);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  observation_failure = true;
  ScopeExit clear_failure([] { observation_failure = false; });
  Q4T_CHECK(Request(server, "GET /metrics HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 503 "));
  clear_failure.Run();
  const int fd = Connect(server);
  ScopeExit close_socket([&] { close(fd); });
  Q4T_CHECK(detail::WriteAll(fd,
      "POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
      "Content-Type: application/json\r\nContent-Length: 1\r\n\r\n"));
  const auto end = std::chrono::steady_clock::now() + 1s;
  while (ServeContractAccess::Business(server) != 1 &&
         std::chrono::steady_clock::now() < end)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(ServeContractAccess::Business(server) == 1);
  stop.Run();
  Q4T_CHECK(Request(server, "GET /metrics HTTP/1.1\r\nHost: t\r\n\r\n")
                .starts_with("HTTP/1.1 200 "));
  const auto ready = Request(server, "GET /readyz HTTP/1.1\r\nHost: t\r\n\r\n");
  Q4T_CHECK(ready.starts_with("HTTP/1.1 503 ") &&
            ready.find("draining") != ready.npos);
  close_socket.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready && run.get().ok());
  Q4T_CHECK(ServeContractAccess::Empty(server));
  return true;
}

Q4T_TEST(http_memory_gate_before_body_controls_and_recovery) {
  ChatServer server;
  ServeContractAccess::Configure(server);
  ServeContractAccess::Memory(server, 100, 50, ReadMemory);
  auto control = ServeContractAccess::Register(server, std::string(64, 'a'));
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  const auto get = [&](const char* path) {
    return Request(server, std::string("GET ") + path +
                               " HTTP/1.1\r\nHost: test\r\n\r\n");
  };
  for (const auto available : {99, -1}) {
    memory_available = available;
    ServeContractAccess::RefreshMemory(server);
    const char* code = available < 0 ? "memory_observation_unavailable"
                                     : "memory_pressure";
    for (const char* path : {"/v1/chat/completions", "/v1/chat/preflight"}) {
      // No body sent: rejection must precede both reservation and reception.
      const auto wire = Request(server, std::string("POST ") + path +
          " HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
          "Content-Length: 16777216\r\n\r\n");
      Q4T_CHECK(wire.starts_with("HTTP/1.1 503 ") &&
                wire.find(code) != wire.npos);
      Q4T_CHECK(ServeContractAccess::BodyBudget(server).Used() == 0);
    }
    const auto ready = get("/readyz");
    Q4T_CHECK(ready.starts_with("HTTP/1.1 503 ") &&
              ready.find(code) != ready.npos);
    for (const char* path : {"/healthz", "/livez", "/metrics", "/v1/models"})
      Q4T_CHECK(get(path).starts_with("HTTP/1.1 200 "));
  }
  const std::string cancellation =
      R"({"request_id":"cancel-contract","cancel_token":")" +
      std::string(64, 'a') + R"("})";
  const auto cancelled = Request(server,
      "POST /v1/requests/cancel HTTP/1.1\r\nHost: test\r\n"
      "Content-Type: application/json\r\nContent-Length: " +
      std::to_string(cancellation.size()) + "\r\n\r\n" + cancellation);
  Q4T_CHECK(cancelled.starts_with("HTTP/1.1 202 ") && control->Cancelled());
  memory_available = 149;
  ServeContractAccess::RefreshMemory(server);
  Q4T_CHECK(get("/readyz").starts_with("HTTP/1.1 503 "));
  memory_available = 150;
  ServeContractAccess::RefreshMemory(server);
  Q4T_CHECK(get("/readyz").starts_with("HTTP/1.1 200 "));
  const auto recovered = Request(server,
      "POST /v1/chat/completions HTTP/1.1\r\nHost: test\r\n"
      "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}");
  // The unloaded fixture throws on dispatch; this is not a successful chat.
  Q4T_CHECK(recovered.starts_with("HTTP/1.1 503 "));
  using q4t::server::MemoryCheckpoint;
  for (size_t reason : {0u, 1u})
    Q4T_CHECK(ServeContractAccess::MemoryRejected(
                  server, MemoryCheckpoint::kHeaders, reason) == 2);
  Q4T_CHECK(ServeContractAccess::ReceiveFailures(
                server, q4t::server::HttpReceiveFailure::kAdmission) == 4);
  stop.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready);
  Q4T_CHECK(run.get().ok() && ServeContractAccess::Empty(server));
  return true;
}

Q4T_TEST(http_memory_recheck_uses_one_sse_error_after_headers) {
  int fd[2];
  Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
  ScopeExit sockets([&] { close(fd[0]); close(fd[1]); });
  ChatServer server;
  ServeContractAccess::Memory(server, 100, 50, ReadMemory);
  memory_available = -1;
  q4t::server::detail::HttpResponseContext context(fd[0]);
  Q4T_CHECK(q4t::server::detail::BeginStreamingResponse(fd[0], context.State()));
  Q4T_CHECK(!ServeContractAccess::CheckMemory(
      server, fd[0], q4t::server::MemoryCheckpoint::kExecute));
  shutdown(fd[0], SHUT_WR);
  std::string response;
  char buffer[4096];
  for (ssize_t n; (n = read(fd[1], buffer, sizeof(buffer))) > 0;)
    response.append(buffer, static_cast<size_t>(n));
  Q4T_CHECK(response.starts_with("HTTP/1.1 200 ") &&
            response.find("HTTP/1.1", 1) == response.npos &&
            response.find("memory_observation_unavailable") != response.npos &&
            response.find("data: [DONE]") != response.npos &&
            response.find("finish_reason") == response.npos);
  Q4T_CHECK(ServeContractAccess::MemoryRejected(
                server, q4t::server::MemoryCheckpoint::kExecute, 1) == 1);
  return true;
}

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
  Q4T_CHECK(failed.starts_with("HTTP/1.1 503 "));
  Q4T_CHECK(failed.find("host_memory_exhausted") != failed.npos);
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
                .starts_with("HTTP/1.1 503 "));
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

Q4T_TEST(http_receive_observation_body_routes_and_business_boundary) {
  using namespace q4t::server;
  ChatServer server;
  ServeContractAccess::Configure(server);
  auto run = std::async(std::launch::async, [&] { return server.Run(); });
  ScopeExit stop([&] { server.RequestStop(); });
  {
    HttpBodyLease held(ServeContractAccess::BodyBudget(server));
    Q4T_CHECK(held.Acquire(kAggregateChatBodyBytes));
    const auto response =
        Request(server,
                "POST /v1/chat/preflight HTTP/1.1\r\nHost: t\r\n"
                "X-Request-Id: observation.fixture\r\nContent-Type: "
                "application/json\r\n"
                "Content-Length: 1\r\n\r\n");
    Q4T_CHECK(response.starts_with("HTTP/1.1 503 "));
    Q4T_CHECK(response.find("body_capacity_exhausted") != response.npos);
    Q4T_CHECK(response.find("\"param\":\"body\"") != response.npos);
    Q4T_CHECK(response.find("X-Request-Id: observation.fixture") !=
              response.npos);
    Q4T_CHECK(ServeContractAccess::ReceiveFailures(
                  server, HttpReceiveFailure::kBodyCapacity) == 1);
    Q4T_CHECK(ServeContractAccess::BodyBudget(server).Used() ==
              kAggregateChatBodyBytes);
  }
  for (const auto& item :
       {std::pair{"GET /unknown HTTP/1.1\r\nHost: t\r\n\r\n",
                  HttpReceiveFailure::kRoute},
        {"GET /healthz?q HTTP/1.1\r\nHost: t\r\n\r\n",
         HttpReceiveFailure::kQuery},
        {"GET /v1/chat/completions HTTP/1.1\r\nHost: t\r\n\r\n",
         HttpReceiveFailure::kMethod},
        {"GET /healthz HTTP/1.1\r\nHost: t\r\nContent-Length: 1\r\n\r\nx",
         HttpReceiveFailure::kUnexpectedBody},
        {"POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
         "Content-Type: text/plain\r\nContent-Length: 2\r\n\r\n{}",
         HttpReceiveFailure::kMedia}}) {
    Q4T_CHECK(Request(server, item.first).find("\"error\"") !=
              std::string::npos);
    Q4T_CHECK(ServeContractAccess::ReceiveFailures(server, item.second) == 1);
  }
  // The fixture deliberately throws inside chat. That is not a receive error.
  Q4T_CHECK(
      Request(server,
              "POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
              "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
          .starts_with("HTTP/1.1 503 "));
  Q4T_CHECK(ServeContractAccess::ReceiveFailures(
                server, HttpReceiveFailure::kException) == 0);
  stop.Run();
  Q4T_CHECK(run.wait_for(3s) == std::future_status::ready && run.get().ok());
  Q4T_CHECK(ServeContractAccess::Empty(server));
  Q4T_CHECK(ServeContractAccess::BodyBudget(server).Used() == 0);
  return true;
}

Q4T_TEST(observation_runtime_partial_header_exception_remains_first) {
  using namespace q4t::server;
  using namespace q4t::server::detail;
  int fd[2];
  Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
  ScopeExit close_socket([&] {
    close(fd[0]);
    close(fd[1]);
  });
  setenv("Q4T_SERVE_FAULT", "handler.partial_header", 1);
  serve_test_hook = +[](const char* name) {
    if (!std::strcmp(name, "handler.partial_header")) throw std::bad_alloc();
  };
  ScopeExit reset([] {
    serve_test_hook = nullptr;
    unsetenv("Q4T_SERVE_FAULT");
  });
  HttpTransportMetrics metrics;
  {
    HttpResponseContext context(fd[0], &metrics);
    bool thrown = false;
    try {
      BeginStreamingResponse(fd[0], context.State());
    } catch (const std::bad_alloc&) {
      thrown = true;
    }
    Q4T_CHECK(thrown && *context.State() == ResponseState::kHeadersStarted);
    const auto failure = context.WriteFailure();
    Q4T_CHECK(failure.reason == HttpWriteFailure::kException);
    Q4T_CHECK(failure.origin == HttpWriteOrigin::kHandler &&
              failure.transferred == 8);
    SendExceptionResponse(fd[0], *context.State());
    Q4T_CHECK(!WriteAll(fd[0], "duplicate"));
    Q4T_CHECK(context.WriteFailure().transferred == 8);
  }
  Q4T_CHECK(metrics.write[static_cast<size_t>(HttpWriteOrigin::kHandler)]
                         [static_cast<size_t>(HttpWriteFailure::kException)] ==
            1);
  return true;
}

Q4T_TEST(observation_runtime_heartbeat_exception_joins_and_counts_once) {
  using namespace q4t::server;
  using namespace q4t::server::detail;
  int fd[2];
  Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
  ScopeExit close_socket([&] {
    close(fd[0]);
    close(fd[1]);
  });
  int small = 1024;
  setsockopt(fd[0], SOL_SOCKET, SO_SNDBUF, &small, sizeof(small));
  serve_test_hook = +[](const char* name) {
    if (!std::strcmp(name, "write.backpressure")) throw std::bad_alloc();
  };
  ScopeExit reset([] { serve_test_hook = nullptr; });
  HttpTransportMetrics metrics;
  {
    HttpResponseContext context(fd[0], &metrics);
    Q4T_CHECK(BeginStreamingResponse(fd[0], context.State()));
    const std::string filler(4096, 'x');
    size_t total = 0;
    ssize_t n;
    while ((n = send(fd[0], filler.data(), filler.size(),
                     MSG_DONTWAIT | MSG_NOSIGNAL)) > 0) {
      total += static_cast<size_t>(n);
      Q4T_CHECK(total < 1024 * 1024);
    }
    Q4T_CHECK(n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK));
    auto control = std::make_shared<RequestControl>(
        "id", "key", std::chrono::steady_clock::now() + 2s);
    context.StartHeartbeats(control, 5ms);
    const auto until = std::chrono::steady_clock::now() + 1s;
    while (context.WriteFailure().reason == HttpWriteFailure::kNone &&
           std::chrono::steady_clock::now() < until)
      std::this_thread::sleep_for(1ms);
    context.StopHeartbeats();
    const auto failure = context.WriteFailure();
    Q4T_CHECK(failure.reason == HttpWriteFailure::kException);
    Q4T_CHECK(failure.origin == HttpWriteOrigin::kHeartbeat &&
              control->Failed());
    Q4T_CHECK(!WriteAll(fd[0], "duplicate"));
  }
  Q4T_CHECK(metrics.write[static_cast<size_t>(HttpWriteOrigin::kHeartbeat)]
                         [static_cast<size_t>(HttpWriteFailure::kException)] ==
            1);
  return true;
}
