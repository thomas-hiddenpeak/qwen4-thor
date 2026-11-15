#include "q4t/server/mcp_transport.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <mutex>
#include <sstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/batch_preparation.h"
#include "q4t/server/batch_state.h"
#include "q4t/server/files.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/prompt_registry.h"
#include "q4t/server/responses_mcp_state.h"
#include "q4t/server/responses_execution.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
struct Request {
  std::string verb, target, body;
  std::map<std::string, std::string> headers;
  Json rpc;
};
bool Send(int fd, std::string_view bytes) {
  const auto deadline = Clock::now() + 2s;
  while (!bytes.empty() && Clock::now() < deadline) {
    const auto count = send(fd, bytes.data(), bytes.size(), MSG_NOSIGNAL);
    if (count > 0)
      bytes.remove_prefix(count);
    else if (count < 0 && (errno == EINTR || errno == EAGAIN))
      std::this_thread::sleep_for(1ms);
    else
      return false;
  }
  return bytes.empty();
}
std::string Http(int status, std::string_view body = "",
                 std::string_view mime = "application/json",
                 std::string_view extra = "") {
  return "HTTP/1.1 " + std::to_string(status) +
         " Result\r\nContent-Type: " + std::string(mime) +
         "\r\nContent-Length: " + std::to_string(body.size()) + "\r\n" +
         std::string(extra) + "\r\n" + std::string(body);
}
bool Chunk(int fd, std::string_view bytes) {
  std::ostringstream size;
  size << std::hex << bytes.size();
  return Send(fd, size.str() + "\r\n" + std::string(bytes) + "\r\n");
}
Json Rpc(const Request& request, Json result) {
  return Object({{"jsonrpc", String("2.0")},
                 {"id", *request.rpc.Find("id")},
                 {"result", std::move(result)}});
}
Json Tool(std::string name = "read_project") {
  return Object({{"name", String(name)},
                 {"inputSchema", Object({{"type", String("object")}})},
                 {"annotations", Object({{"readOnlyHint", Boolean(true)}})}});
}
class Server {
 public:
  using Handler = std::function<bool(const Request&, int, std::stop_token)>;
  Server() {
    fd_ = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    socklen_t size = sizeof(address);
    if (fd_ < 0 || bind(fd_, reinterpret_cast<sockaddr*>(&address), size) ||
        getsockname(fd_, reinterpret_cast<sockaddr*>(&address), &size) ||
        listen(fd_, 16))
      throw std::runtime_error("MCP fixture listener");
    port = ntohs(address.sin_port);
    thread_ = std::jthread([this](std::stop_token stop) {
      while (!stop.stop_requested()) {
        pollfd event{fd_, POLLIN, 0};
        if (poll(&event, 1, 10) <= 0) continue;
        const int client =
            accept4(fd_, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK);
        if (client < 0) continue;
        clients_.emplace_back([this, client](std::stop_token child_stop) {
          ScopeExit close_client([&] { close(client); });
          Request request;
          if (!Read(client, child_stop, &request)) return;
          {
            std::lock_guard lock(mutex_);
            requests_.push_back(request);
          }
          if (handler && handler(request, client, child_stop)) return;
          if (request.verb == "DELETE") {
            Send(client, Http(204));
            return;
          }
          const auto method = request.rpc.GetString("method");
          if (method == "initialize") {
            const auto result =
                Object({{"protocolVersion", String("2025-06-18")},
                        {"capabilities", Object({{"tools", Object()}})},
                        {"serverInfo", Object({{"name", String("fixture")},
                                               {"version", String("1")}})}});
            Send(client,
                 Http(200, OutputJson(Rpc(request, result)), "application/json",
                      "Mcp-Session-Id: opaque\r\n"));
          } else if (!request.rpc.Find("id") ||
                     (method.empty() && (request.rpc.Find("result") ||
                                         request.rpc.Find("error")))) {
            Send(client, Http(202));
          } else if (method == "tools/list") {
            Send(
                client,
                Http(200, OutputJson(Rpc(
                              request, Object({{"tools", Array({Tool()})}})))));
          } else if (method == "tools/call") {
            ++calls;
            const auto result = Object(
                {{"content",
                  Array({Object({{"type", String("text")},
                                 {"text", String("document result")}})})},
                 {"isError", Boolean(false)}});
            Send(client, Http(200, OutputJson(Rpc(request, result))));
          } else {
            Send(client, Http(400));
          }
        });
      }
      for (auto& client : clients_) client.request_stop();
      for (auto& client : clients_) client.join();
    });
  }
  ~Server() {
    thread_.request_stop();
    thread_.join();
    close(fd_);
  }
  McpEndpoint Endpoint() const {
    return {"http://127.0.0.1:" + std::to_string(port) + "/mcp", {}, ""};
  }
  FileFetchOptions Options() const {
    FileFetchOptions options;
    options.public_networks = false;
    options.allowed_networks = {"127.0.0.1/32"};
    options.ports = {port};
    return options;
  }
  std::vector<Request> Requests() {
    std::lock_guard lock(mutex_);
    return requests_;
  }
  Handler handler;
  std::atomic<int> calls{0};
  int port = 0;

 private:
  bool Read(int fd, std::stop_token stop, Request* out) {
    std::string bytes;
    const auto deadline = Clock::now() + 3s;
    size_t head = std::string::npos, length = 0;
    while (!stop.stop_requested() && Clock::now() < deadline) {
      char data[4096];
      const auto count = recv(fd, data, sizeof(data), MSG_DONTWAIT);
      if (count > 0)
        bytes.append(data, count);
      else if (!count)
        return false;
      else if (errno != EAGAIN && errno != EINTR)
        return false;
      if (bytes.size() > (2U << 20)) return false;
      if (head == bytes.npos && (head = bytes.find("\r\n\r\n")) != bytes.npos) {
        std::istringstream lines(bytes.substr(0, head));
        std::string line;
        std::getline(lines, line);
        std::istringstream start(line);
        start >> out->verb >> out->target;
        while (std::getline(lines, line)) {
          if (line.ends_with('\r')) line.pop_back();
          std::string key;
          std::string_view value;
          if (!http::Field(line, &key, &value)) return false;
          out->headers[key] = value;
        }
        if (out->headers.contains("content-length"))
          length = std::stoul(out->headers.at("content-length"));
      }
      if (head != bytes.npos && bytes.size() >= head + 4 + length) {
        out->body = bytes.substr(head + 4, length);
        if (!out->body.empty() && !Parse(out->body, &out->rpc)) return false;
        return true;
      }
      std::this_thread::sleep_for(1ms);
    }
    return false;
  }
  int fd_ = -1;
  std::mutex mutex_;
  std::vector<Request> requests_;
  std::vector<std::jthread> clients_;
  std::jthread thread_;
};
BinaryAccess Access() { return {"client:mcp", Clock::now() + 3s, {}}; }
Json Call() {
  return Object({{"name", String("read_project")},
                 {"arguments", Object({{"path", String("docs/MODEL.md")}})}});
}
ObjectReply Invoke(
    Server& server, McpTransportReceipt* out,
    std::string_view method = "tools/list",
    std::function<ObjectReply()> permit = [] { return ObjectReply{}; },
    BinaryAccess access = Access(), GrammarControl control = {}) {
  return InvokeMcp(access, server.Endpoint(), server.Options(), method,
                   method == "tools/list" ? Object() : Call(), permit, out,
                   std::move(control));
}
void Stall(std::stop_token stop) {
  while (!stop.stop_requested()) std::this_thread::sleep_for(1ms);
}
}  // namespace

int McpTransportFixture(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--mcp-transport-fixture")) return -1;
  if (argc != 4) return 2;
  Json policy;
  FileFetchOptions options;
  if (!Parse(argv[3], &policy) || !ParseFileFetchOptions(policy, &options).ok())
    return 2;
  McpEndpoint endpoint{argv[2], {}, "fixture-mcp-token"};
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  McpTransportReceipt receipt;
  const auto reply = InvokeMcp(
      Access(), endpoint, options, "tools/call", Call(),
      [] { return ObjectReply{}; }, &receipt, control);
  const auto out =
      Object({{"status", Number(reply.http_status)},
              {"code", String(reply.code)},
              {"result", receipt.result},
              {"permitted", Boolean(receipt.call_permitted)},
              {"unknown", Boolean(receipt.outcome_unknown)},
              {"started", Number(control.counters->started.load())},
              {"reaped", Number(control.counters->reaped.load())},
              {"active", Number(control.counters->active.load())}});
  std::puts(OutputJson(out).c_str());
  return 0;
}

Q4T_TEST(mcp_transport_json_pagination_session_and_credentials) {
  Server server;
  server.handler = [](const Request& r, int fd, std::stop_token) {
    if (r.rpc.GetString("method") != "tools/list") return false;
    const auto* params = r.rpc.Find("params");
    const bool next = params && params->GetString("cursor") == "next";
    auto result = Object({{"tools", Array({Tool(next ? "two" : "one")})}});
    if (!next) Set(&result, "nextCursor", String("next"));
    Send(fd, Http(200, OutputJson(Rpc(r, result))));
    return true;
  };
  auto endpoint = server.Endpoint();
  endpoint.authorization = "fixture-secret-token";
  endpoint.headers["X-Test"] = "hello";
  McpTransportReceipt receipt;
  Q4T_CHECK(InvokeMcp(Access(), endpoint, server.Options(), "tools/list",
                      Object(), {}, &receipt)
                .ok());
  Q4T_CHECK(receipt.result.GetArray("tools")->array.size() == 2);
  Q4T_CHECK(receipt.protocol == "2025-06-18" && !receipt.legacy_sse);
  const auto requests = server.Requests();
  Q4T_CHECK(requests.size() == 5 && requests.back().verb == "DELETE");
  for (size_t i = 0; i < requests.size(); ++i) {
    Q4T_CHECK(requests[i].headers.at("authorization") ==
              "Bearer fixture-secret-token");
    if (i) {
      Q4T_CHECK(requests[i].headers.at("mcp-session-id") == "opaque");
      Q4T_CHECK(requests[i].headers.at("mcp-protocol-version") == "2025-06-18");
    } else {
      Q4T_CHECK(!requests[i].headers.contains("mcp-session-id"));
    }
  }
  Q4T_CHECK(OutputJson(receipt.result).find("fixture-secret") ==
            std::string::npos);
  return true;
}

Q4T_TEST(mcp_transport_sse_returns_before_eof_and_replies_to_ping) {
  Server server;
  server.handler = [](const Request& r, int fd, std::stop_token stop) {
    if (r.rpc.GetString("method") != "tools/list") return false;
    Send(fd,
         "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
         "Transfer-Encoding: chunked\r\n\r\n");
    const auto ping = OutputJson(Object({{"jsonrpc", String("2.0")},
                                         {"id", String("ping-id")},
                                         {"method", String("ping")}}));
    const auto result =
        OutputJson(Rpc(r, Object({{"tools", Array({Tool()})}})));
    // One-byte HTTP chunks exercise framing independently of SSE line splits.
    const auto wire = "\xef\xbb\xbf: heartbeat\rdata: " + ping +
                      "\r\rdata: " + result + "\n\n";
    for (const char c : wire) Chunk(fd, std::string(1, c));
    Stall(stop);
    return true;
  };
  McpTransportReceipt receipt;
  const auto start = Clock::now();
  Q4T_CHECK(Invoke(server, &receipt).ok());
  Q4T_CHECK(Clock::now() - start < 2s);
  bool ping = false;
  for (const auto& r : server.Requests())
    ping |= r.rpc.GetString("id") == "ping-id" && r.rpc.Find("result");
  Q4T_CHECK(ping && receipt.result.GetArray("tools")->array.size() == 1);
  return true;
}

Q4T_TEST(mcp_transport_call_waits_for_durable_permission) {
  Server server;
  McpTransportReceipt receipt;
  bool checkpoint = false;
  auto result = Invoke(server, &receipt, "tools/call", [&] {
    checkpoint = true;
    return Error(503, "fixture_state_unavailable", "No durable record");
  });
  Q4T_CHECK(checkpoint && result.code == "fixture_state_unavailable");
  Q4T_CHECK(!receipt.call_permitted && !receipt.outcome_unknown &&
            !server.calls);
  result = Invoke(server, &receipt, "tools/call", [] {
    ObjectReply unknown;
    unknown.commit_unknown = true;
    return unknown;
  });
  Q4T_CHECK(result.code == "mcp_state_commit_unknown" &&
            result.commit_unknown && !receipt.call_permitted && !server.calls);
  Q4T_CHECK(Invoke(server, &receipt, "tools/call").ok());
  Q4T_CHECK(receipt.call_permitted && !receipt.outcome_unknown &&
            server.calls == 1);
  Q4T_CHECK(receipt.result.GetArray("content")->array[0].GetString("text") ==
            "document result");
  return true;
}

Q4T_TEST(mcp_transport_actual_call_requires_owned_persisted_approval) {
  const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto root = std::filesystem::absolute(
      output ? output : ".q4t-work/mcp-state-transport-cpu");
  std::filesystem::create_directories(root);
  auto directory = (root / "mcp-transport-state-XXXXXX").string();
  Q4T_CHECK(mkdtemp(directory.data()));
  ObjectStoreOptions options;
  options.directory = directory + "/objects";
  ObjectStore objects(options);
  ChatPlanConfig config;
  config.model = "mcp-transport-fixture";
  ResponsesMcpState state(objects, config);
  const auto access = [] {
    return StateAccess{"client:mcp", Clock::now() + 5s, {}};
  };
  Server server;
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(
      Object({{"type", String("mcp")},
              {"server_label", String("project")},
              {"server_url", String(server.Endpoint().url)}}),
      &definition, &error));
  McpTransportReceipt receipt;
  Q4T_CHECK(Invoke(server, &receipt).ok());
  auto catalog =
      state.Catalog(definition, *receipt.result.Find("tools"), access());
  Q4T_CHECK(catalog.ok());
  auto proposed = state.Propose(catalog.value, definition, "read_project",
                                *Call().Find("arguments"), "direct", access());
  Q4T_CHECK(proposed.ok());
  const auto id = proposed.value.GetString("id");
  const auto permit = [&] { return state.Permit(id, definition, access()); };
  Q4T_CHECK(Invoke(server, &receipt, "tools/call", permit).http_status == 409 &&
            !receipt.call_permitted && !server.calls);
  Q4T_CHECK(state.Decide(id, true, "read project", definition, access()).ok());
  std::atomic<bool> saw_durable_permission{false};
  server.handler = [&](const Request& request, int, std::stop_token) {
    if (request.rpc.GetString("method") == "tools/call") {
      const auto record = state.Inspect(id, access());
      saw_durable_permission =
          record.ok() && record.value.GetString("state") == "dispatching" &&
          record.value.Find("decision")->GetBool("approve");
    }
    return false;
  };
  const auto called = Invoke(server, &receipt, "tools/call", permit);
  Q4T_CHECK(called.ok() && saw_durable_permission && server.calls == 1);
  const auto finished = state.Finish(id, receipt, called, access());
  Q4T_CHECK(finished.ok() && finished.value.GetString("status") == "completed");
  Q4T_CHECK(!Invoke(server, &receipt, "tools/call", permit).ok() &&
            !receipt.call_permitted && server.calls == 1);
  return true;
}

Q4T_TEST(mcp_transport_rejects_headers_and_targets_before_worker) {
  Server server;
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  McpTransportReceipt receipt;
  for (const auto& key : {"Host", "mcp-session-id", "MCP-Protocol-Version",
                          "Transfer-Encoding", "Connection", "Accept"}) {
    auto endpoint = server.Endpoint();
    endpoint.headers[key] = "bad";
    Q4T_CHECK(InvokeMcp(Access(), endpoint, server.Options(), "tools/list",
                        Object(), {}, &receipt, control)
                  .http_status == 400);
  }
  auto endpoint = server.Endpoint();
  endpoint.headers["X-Token"] = "a\r\nInjected: secret";
  Q4T_CHECK(InvokeMcp(Access(), endpoint, server.Options(), "tools/list",
                      Object(), {}, &receipt, control)
                .http_status == 400);
  endpoint.headers.clear();
  auto policy = server.Options();
  policy.allowed_networks.clear();
  // URL address rejection can happen after DNS in the independently owned
  // worker; no connection is made to the denied numeric address.
  Q4T_CHECK(InvokeMcp(Access(), endpoint, policy, "tools/list", Object(), {},
                      &receipt, control)
                .http_status == 403);
  Q4T_CHECK(server.Requests().empty());
  Q4T_CHECK(control.counters->started <= 1);
  return true;
}

Q4T_TEST(mcp_transport_call_failure_never_retries_or_reinitializes) {
  for (const int status : {404, 503, 0}) {
    Server server;
    server.handler = [&](const Request& r, int fd, std::stop_token) {
      if (r.rpc.GetString("method") != "tools/call") return false;
      ++server.calls;
      if (status) Send(fd, Http(status));
      return true;
    };
    McpTransportReceipt receipt;
    Q4T_CHECK(!Invoke(server, &receipt, "tools/call").ok());
    Q4T_CHECK(receipt.call_permitted && receipt.outcome_unknown &&
              server.calls == 1);
    int initialize = 0;
    for (const auto& r : server.Requests())
      initialize += r.rpc.GetString("method") == "initialize";
    Q4T_CHECK(initialize == 1);
  }
  return true;
}

Q4T_TEST(mcp_transport_rpc_error_and_structured_result_preserved) {
  for (const bool error : {false, true}) {
    Server server;
    server.handler = [=](const Request& r, int fd, std::stop_token) {
      if (r.rpc.GetString("method") != "tools/call") return false;
      Json value;
      if (error) {
        value = Object({{"jsonrpc", String("2.0")},
                        {"id", *r.rpc.Find("id")},
                        {"error", Object({{"code", Number(-32602)},
                                          {"message", String("Bad path")}})}});
      } else {
        value =
            Rpc(r, Object({{"content", Array()},
                           {"structuredContent", Object({{"n", Number(42)}})},
                           {"isError", Boolean(true)}}));
      }
      Send(fd, Http(200, OutputJson(value)));
      return true;
    };
    McpTransportReceipt receipt;
    Q4T_CHECK(Invoke(server, &receipt, "tools/call").ok());
    Q4T_CHECK(receipt.call_permitted && !receipt.outcome_unknown);
    if (error) {
      Q4T_CHECK(receipt.error.GetInt("code") == -32602);
    } else {
      Q4T_CHECK(receipt.result.GetBool("isError"));
      Q4T_CHECK(receipt.result.Find("structuredContent")->GetInt("n") == 42);
    }
  }
  return true;
}

Q4T_TEST(mcp_transport_pagination_cycles_and_duplicate_names_rejected) {
  for (const bool duplicate : {false, true}) {
    Server server;
    int page = 0;
    server.handler = [&](const Request& r, int fd, std::stop_token) {
      if (r.rpc.GetString("method") != "tools/list") return false;
      ++page;
      auto result = Object(
          {{"tools", Array({Tool(duplicate ? "same" : std::to_string(page))})},
           {"nextCursor", String("cycle")}});
      Send(fd, Http(200, OutputJson(Rpc(r, result))));
      return true;
    };
    McpTransportReceipt receipt;
    Q4T_CHECK(Invoke(server, &receipt).code == "mcp_protocol_error");
    Q4T_CHECK(page == 2 && !receipt.call_permitted);
  }
  return true;
}

Q4T_TEST(mcp_transport_invalid_rpc_and_http_frames_rejected) {
  const std::vector<std::string> responses{
      Http(200, R"({"jsonrpc":"2.0","id":999,"result":{"tools":[]}})"),
      Http(200, R"({"jsonrpc":"2.0","id":2,"id":2,"result":{}})"),
      Http(200, R"({"jsonrpc":"2.0","id":2,"result":{},"error":{}})"),
      "HTTP/1.1 200 OK\r\nContent-Length: 4\r\nTransfer-Encoding: "
      "chunked\r\n\r\n0\r\n\r\n",
      "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
      "40\r\n\r\n{}",
      "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
      "5000000\r\n\r\n"};
  for (const auto& wire : responses) {
    Server server;
    server.handler = [&](const Request& r, int fd, std::stop_token) {
      if (r.rpc.GetString("method") != "tools/list") return false;
      Send(fd, wire);
      return true;
    };
    McpTransportReceipt receipt;
    Q4T_CHECK(!Invoke(server, &receipt).ok());
    Q4T_CHECK(receipt.result.IsNull() && !receipt.call_permitted);
  }
  return true;
}

Q4T_TEST(mcp_transport_timeout_and_cancel_are_bounded) {
  for (const bool cancel : {false, true}) {
    Server server;
    server.handler = [](const Request&, int, std::stop_token stop) {
      Stall(stop);
      return true;
    };
    auto access = Access();
    const auto start = Clock::now();
    if (cancel)
      access.cancelled = [=] { return Clock::now() - start > 80ms; };
    else
      access.deadline = start + 80ms;
    McpTransportReceipt receipt;
    const auto reply = Invoke(server, &receipt, "tools/list", {}, access);
    Q4T_CHECK(reply.http_status == (cancel ? 499 : 408));
    Q4T_CHECK(Clock::now() - start < 750ms && !receipt.call_permitted);
  }
  return true;
}

Q4T_TEST(mcp_transport_unreaped_slot_isolated_from_file_and_grammar) {
  Server server;
  detail::DeferGrammarReapingForTesting(true);
  ScopeExit restore([] {
    detail::DeferGrammarReapingForTesting(false);
    const auto end = Clock::now() + 1s;
    while (detail::IsolatedCleanupPending(IsolatedWorkerDomain::kMcp) &&
           Clock::now() < end)
      std::this_thread::sleep_for(1ms);
  });
  McpTransportReceipt receipt;
  Q4T_CHECK(Invoke(server, &receipt).ok());
  Q4T_CHECK(detail::IsolatedCleanupPending(IsolatedWorkerDomain::kMcp));
  Q4T_CHECK(Invoke(server, &receipt).code == "mcp_worker_capacity");
  Q4T_CHECK(!detail::IsolatedCleanupPending(IsolatedWorkerDomain::kGrammar));
  Q4T_CHECK(!detail::IsolatedCleanupPending(IsolatedWorkerDomain::kFileFetch));
  for (auto domain :
       {IsolatedWorkerDomain::kGrammar, IsolatedWorkerDomain::kFileFetch}) {
    GrammarControl control;
    control.domain = domain;
    std::unique_ptr<detail::GrammarProcess> child;
    Q4T_CHECK(detail::GrammarProcess::Start("--cache-fixture-descriptors",
                                            control, &child));
    char byte = 0;
    Q4T_CHECK(child->Transfer(&byte, 1, false, Clock::now() + 1s) &&
              byte == 'x');
    // Only the MCP owner remains artificially unreaped across requests.
    detail::DeferGrammarReapingForTesting(false);
    child.reset();
    detail::DeferGrammarReapingForTesting(true);
  }
  return true;
}

Q4T_TEST(mcp_transport_legacy_sse_lists_and_calls_real_endpoint) {
  Server server;
  std::atomic<int> event_fd{-1};
  server.handler = [&](const Request& r, int fd, std::stop_token stop) {
    if (r.target == "/mcp" && r.verb == "POST") {
      Send(fd, Http(405));
    } else if (r.verb == "GET") {
      event_fd = fd;
      Send(fd,
           "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
           "Transfer-Encoding: chunked\r\n\r\n");
      Chunk(fd, "event: endpoint\ndata: /messages?session=fixture\n\n");
      Stall(stop);
    } else if (r.target == "/messages?session=fixture") {
      Send(fd, Http(202));
      if (!r.rpc.Find("id")) return true;
      Json result;
      const auto method = r.rpc.GetString("method");
      if (method == "initialize") {
        result = Object({{"protocolVersion", String("2024-11-05")},
                         {"capabilities", Object({{"tools", Object()}})},
                         {"serverInfo", Object({{"name", String("legacy")},
                                                {"version", String("1")}})}});
      } else if (method == "tools/list") {
        result = Object({{"tools", Array({Tool()})}});
      } else if (method == "tools/call") {
        ++server.calls;
        result = Object(
            {{"content", Array({Object({{"type", String("text")},
                                        {"text", String("legacy")}})})}});
      }
      Chunk(event_fd,
            "event: message\ndata: " + OutputJson(Rpc(r, result)) + "\n\n");
    } else {
      Send(fd, Http(404));
    }
    return true;
  };
  McpTransportReceipt receipt;
  Q4T_CHECK(Invoke(server, &receipt).ok());
  Q4T_CHECK(receipt.legacy_sse && receipt.protocol == "2024-11-05" &&
            receipt.result.GetArray("tools")->array.size() == 1);
  Q4T_CHECK(Invoke(server, &receipt, "tools/call").ok());
  Q4T_CHECK(receipt.legacy_sse && receipt.call_permitted && server.calls == 1 &&
            !receipt.outcome_unknown);
  for (const auto& r : server.Requests())
    if (r.rpc.GetString("method") == "tools/call")
      Q4T_CHECK(r.target == "/messages?session=fixture" &&
                r.headers.at("mcp-protocol-version") == "2024-11-05");
  return true;
}

Q4T_TEST(mcp_transport_does_not_forward_auth_to_cross_origin_endpoint) {
  Server source, destination;
  source.handler = [&](const Request& r, int fd, std::stop_token stop) {
    if (r.verb == "POST") {
      Send(fd, Http(405));
    } else {
      Send(fd,
           "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
           "Transfer-Encoding: chunked\r\n\r\n");
      Chunk(fd,
            "event: endpoint\ndata: " + destination.Endpoint().url + "\n\n");
      Stall(stop);
    }
    return true;
  };
  auto endpoint = source.Endpoint();
  endpoint.authorization = "private-test-credential";
  auto options = source.Options();
  options.ports.push_back(destination.port);
  McpTransportReceipt receipt;
  Q4T_CHECK(InvokeMcp(Access(), endpoint, options, "tools/list", Object(), {},
                      &receipt)
                .code == "mcp_endpoint_denied");
  Q4T_CHECK(destination.Requests().empty());
  return true;
}

Q4T_TEST(mcp_transport_versions_and_auth_failures_do_not_dispatch) {
  for (int mode = 0; mode < 3; ++mode) {
    Server server;
    server.handler = [=](const Request& r, int fd, std::stop_token) {
      if (r.rpc.GetString("method") != "initialize") return false;
      if (mode < 2) {
        Send(fd, Http(mode ? 403 : 401));
      } else {
        auto result = Object({{"protocolVersion", String("2099-01-01")},
                              {"capabilities", Object()},
                              {"serverInfo", Object()}});
        Send(fd, Http(200, OutputJson(Rpc(r, result))));
      }
      return true;
    };
    McpTransportReceipt receipt;
    Q4T_CHECK(!Invoke(server, &receipt, "tools/call").ok());
    Q4T_CHECK(!receipt.call_permitted && !server.calls);
    Q4T_CHECK(server.Requests().size() == 1);
  }
  return true;
}

Q4T_TEST(mcp_transport_cancel_after_dispatch_reports_unknown) {
  Server server;
  server.handler = [&](const Request& r, int, std::stop_token stop) {
    if (r.rpc.GetString("method") != "tools/call") return false;
    ++server.calls;
    Stall(stop);
    return true;
  };
  auto access = Access();
  access.cancelled = [&] { return server.calls.load() > 0; };
  McpTransportReceipt receipt;
  Q4T_CHECK(
      Invoke(
          server, &receipt, "tools/call", [] { return ObjectReply{}; }, access)
          .http_status == 499);
  Q4T_CHECK(receipt.call_permitted && receipt.outcome_unknown &&
            server.calls == 1);
  return true;
}

Q4T_TEST(mcp_transport_session_cleanup_failure_preserves_known_result) {
  Server server;
  server.handler = [](const Request& r, int fd, std::stop_token) {
    if (r.verb != "DELETE") return false;
    Send(fd, Http(500));
    return true;
  };
  McpTransportReceipt receipt;
  Q4T_CHECK(Invoke(server, &receipt, "tools/call").ok());
  Q4T_CHECK(receipt.call_permitted && !receipt.outcome_unknown &&
            server.calls == 1);
  return true;
}

namespace {
struct ExecutionFixture {
  ObjectStoreOptions options;
  explicit ExecutionFixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root =
        std::filesystem::absolute(output ? output : ".q4t-work/mcp-cpu");
    std::filesystem::create_directories(root);
    auto directory = (root / "mcp-execution-XXXXXX").string();
    if (!mkdtemp(directory.data()))
      throw std::runtime_error("MCP fixture path");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
  }
};
StateAccess ExecutionAccess() {
  return {"client:mcp-execution", Clock::now() + 10s, {}};
}
ChatPlanConfig ExecutionConfig(const Server& server) {
  ChatPlanConfig config;
  config.model = "cpu-mcp";
  config.structured = true;
  auto files = std::make_shared<FileExpansionOptions>();
  files->fetch = server.Options();
  config.file_inputs = files;
  return config;
}
Json ExecutionRequest(const Server& server, std::string approval = "never",
                      bool credentials = true) {
  auto tool = Object({{"type", String("mcp")},
                      {"server_label", String("project")},
                      {"server_url", String(server.Endpoint().url)},
                      {"require_approval", String(std::move(approval))}});
  if (credentials) {
    Set(&tool, "authorization", String("mcp-request-secret"));
    Set(&tool, "headers", Object({{"X-Private", String("mcp-header-secret")}}));
  }
  return Object({{"input", String("Read the project document")},
                 {"reasoning", Object({{"effort", String("none")}})},
                 {"tools", Array({std::move(tool)})}});
}
bool ExecutionOk(const ObjectReply& reply) {
  if (!reply.ok() || reply.commit_unknown)
    std::fprintf(stderr, "MCP execution: %s: %s (%s)\n", reply.code.c_str(),
                 reply.message.c_str(), reply.value.GetString("param").c_str());
  return reply.ok() && !reply.commit_unknown;
}
bool ModelCall(ResponsesExecution* run, bool partial, std::string* wire) {
  const auto* tools = run->Generation().generation_request.GetArray("tools");
  if (!tools || tools->array.size() != 1) return false;
  ResponseDelta start;
  start.kind = ResponseDelta::Kind::kTool;
  start.tool_start = true;
  start.call.index = 0;
  start.call.id = "model_call_1";
  start.call.name = tools->array[0].Find("function")->GetString("name");
  start.call.arguments = " { \"path\" : ";
  if (!run->Append(start, "", wire)) return false;
  if (partial) return true;
  start.tool_start = false;
  start.call.arguments = "\"docs/MODEL.md\" } ";
  std::string part;
  if (!run->Append(start, "", &part)) return false;
  *wire += part;
  return true;
}
bool FinishExecution(ResponsesExecution* run, size_t calls = 0) {
  ResponseDelta text;
  text.kind = ResponseDelta::Kind::kContent;
  text.text = "CPU executor final reply";
  std::string wire;
  return run->Append(text, "", &wire) &&
         ExecutionOk(run->Finish(false, calls, {}, 43, &wire));
}
bool ContinueExecution(ResponsesExecution* run, const ChatPlanConfig& config) {
  ResponsesGenerationPlan next;
  return ExecutionOk(run->ContinueFileSearch(
      8, config, [](const auto&, auto*) { return q4t::Status{}; }, &next));
}
Json McpPrompt(const Server& server, std::string name = "read_project") {
  auto request = ExecutionRequest(server, "never", false);
  Set(&request, "tool_choice",
      Object({{"type", String("mcp")}, {"server_label", String("project")},
              {"name", String(std::move(name))}}));
  return Object({{"request", std::move(request)}, {"variables", Object()}});
}
Json ApprovalResponse(const Json& request, const Json& approval, bool approve,
                      const std::string& previous) {
  auto next = request;
  Set(&next, "previous_response_id", String(previous));
  Set(&next, "input",
      Array({Object({{"type", String("mcp_approval_response")},
                     {"approval_request_id", *approval.Find("id")},
                     {"approve", Boolean(approve)}})}));
  return next;
}
}  // namespace

Q4T_TEST(responses_mcp_prompt_freezes_version_and_uses_request_credentials) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  PromptRegistry prompts(objects);
  const auto config = ExecutionConfig(server);
  auto definition = McpPrompt(server);
  const auto saved = prompts.Create(definition, config, ExecutionAccess());
  Q4T_CHECK(ExecutionOk(saved) && server.Requests().empty());
  const auto id = saved.value.GetString("id");
  auto request = Object({{"prompt", Object({{"id", String(id)}})},
                         {"tools", *ExecutionRequest(server).Find("tools")}});
  ResponsesExecution prepared(objects);
  Q4T_CHECK(
      ExecutionOk(prepared.Prepare(request, config, ExecutionAccess(), 42)));
  Json frozen;
  Q4T_CHECK(ExecutionOk(prepared.FreezeInputs(&frozen)));
  Q4T_CHECK(OutputJson(frozen).find("mcp-request-secret") == std::string::npos);
  Q4T_CHECK(OutputJson(frozen).find("mcp-header-secret") == std::string::npos);
  Set(&Field(definition, "request"), "input", String("different version"));
  Q4T_CHECK(ExecutionOk(
      prompts.Append(id, 1, definition, config, ExecutionAccess())));
  Q4T_CHECK(ExecutionOk(prompts.Erase(id, ExecutionAccess())));
  const auto listed = server.Requests().size();
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(run.RestoreInputs(frozen, config, ExecutionAccess(),
                                         42, nullptr, &request)));
  Q4T_CHECK(server.Requests().size() == listed);
  Q4T_CHECK(run.Envelope().options.Find("prompt")->GetString("version") ==
            "1");
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting &&
            server.calls == 1);
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
  for (const auto& operation : server.Requests()) {
    Q4T_CHECK(operation.headers.at("authorization") ==
              "Bearer mcp-request-secret");
    Q4T_CHECK(operation.headers.at("x-private") == "mcp-header-secret");
  }
  Q4T_CHECK(run.ResultJson().find("mcp-request-secret") == std::string::npos);
  Q4T_CHECK(run.ResultJson().find("mcp-header-secret") == std::string::npos);
  return true;
}

Q4T_TEST(responses_mcp_prompt_checks_choices_without_inventing_catalogs) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  PromptRegistry prompts(objects);
  const auto config = ExecutionConfig(server);
  auto definition = McpPrompt(server, "not_in_remote_catalog");
  auto saved = prompts.Create(definition, config, ExecutionAccess());
  Q4T_CHECK(ExecutionOk(saved) && server.Requests().empty());
  ResponsesExecution run(objects);
  const auto request = Object(
      {{"prompt", Object({{"id", *saved.value.Find("id")}})}});
  const auto rejected = run.Prepare(request, config, ExecutionAccess(), 42);
  Q4T_CHECK(rejected.http_status == 400 && !server.calls &&
            !server.Requests().empty());
  const auto previous = server.Requests().size();
  for (int variant = 0; variant < 5; ++variant) {
    auto invalid = McpPrompt(server);
    auto& req = Field(invalid, "request");
    auto& choice = Field(req, "tool_choice");
    if (variant == 0)
      Set(&choice, "server_label", String("not_declared"));
    else if (variant == 1)
      Set(&Field(req, "tools").array.front(), "allowed_callers",
          Array({String("programmatic")}));
    else if (variant == 2)
      Set(&Field(req, "tools").array.front(), "allowed_tools",
          Array({String("another_tool")}));
    else
      choice = Object({{"type", String("allowed_tools")},
                       {"mode", String(variant == 3 ? "bad" : "required")},
                       {"tools", Array({choice})}});
    if (variant == 4) Set(&choice, "unknown_field", Boolean(true));
    Q4T_CHECK(prompts.Create(invalid, config, ExecutionAccess()).http_status ==
              400);
  }
  auto allowed = McpPrompt(server);
  auto& req = Field(allowed, "request");
  const auto selector = *req.Find("tool_choice");
  Set(&req, "tool_choice",
      Object({{"type", String("allowed_tools")}, {"mode", String("required")},
              {"tools", Array({selector})}}));
  Q4T_CHECK(ExecutionOk(prompts.Create(allowed, config, ExecutionAccess())));
  Q4T_CHECK(server.Requests().size() == previous);
  return true;
}

Q4T_TEST(responses_mcp_prompt_secrets_rejected_before_persistence) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  PromptRegistry prompts(objects);
  const auto config = ExecutionConfig(server);
  const auto saved = prompts.Create(McpPrompt(server), config, ExecutionAccess());
  Q4T_CHECK(ExecutionOk(saved));
  const auto id = saved.value.GetString("id");
  const auto original = prompts.Retrieve(id, "", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(original));
  for (int variant = 0; variant < 3; ++variant) {
    auto invalid = McpPrompt(server);
    const auto private_tools = *ExecutionRequest(server).Find("tools");
    if (variant == 0)
      Set(&Field(invalid, "request"), "tools", private_tools);
    else {
      auto input = Array({Object(
          {{"type", String(variant == 1 ? "additional_tools"
                                         : "tool_search_output")},
           {"tools", private_tools}})});
      if (variant == 2) {
        Set(&input.array.front(), "call_id", String("search_external"));
        Set(&input.array.front(), "execution", String("client"));
        Set(&input.array.front(), "status", String("completed"));
      }
      Set(&Field(invalid, "request"), "input", std::move(input));
    }
    Q4T_CHECK(prompts.Create(invalid, config, ExecutionAccess()).http_status ==
              400);
    Q4T_CHECK(prompts.Append(id, 1, invalid, config, ExecutionAccess())
                  .http_status == 400);
    Q4T_CHECK(OutputJson(prompts.Retrieve(id, "", ExecutionAccess()).value) ==
              OutputJson(original.value));
  }
  ObjectRequest page;
  const auto entries = prompts.List(page, ExecutionAccess());
  Q4T_CHECK(ExecutionOk(entries) &&
            entries.value.GetArray("data")->array.size() == 1);
  Q4T_CHECK(server.Requests().empty());
  return true;
}

Q4T_TEST(
    responses_mcp_execution_streams_actual_call_and_keeps_credentials_private) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesExecution run(objects);
  const auto config = ExecutionConfig(server);
  Q4T_CHECK(ExecutionOk(
      run.Prepare(ExecutionRequest(server), config, ExecutionAccess(), 42)));
  Q4T_CHECK(run.HasMcp() && run.VariableInputSteps());
  Json frozen;
  Q4T_CHECK(ExecutionOk(run.FreezeInputs(&frozen)));
  const auto encoded = OutputJson(frozen);
  Q4T_CHECK(encoded.find("mcp-request-secret") == encoded.npos &&
            encoded.find("mcp-header-secret") == encoded.npos &&
            frozen.GetArray("mcp_catalogs")->array.size() == 1);
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)));
  Q4T_CHECK(wire.find("response.mcp_list_tools.completed") != wire.npos);
  Q4T_CHECK(ModelCall(&run, false, &wire));
  Q4T_CHECK(wire.find("response.mcp_call_arguments.delta") != wire.npos &&
            wire.find("function_call") == wire.npos && server.calls == 0);
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, [&](const auto& part) {
    wire += part;
    return true;
  })));
  Q4T_CHECK(!waiting && server.calls == 1 && !run.PendingMcp());
  const auto& call = run.Output().Snapshot().GetArray("output")->array.back();
  Q4T_CHECK(
      call.GetString("type") == "mcp_call" &&
      call.GetString("status") == "completed" &&
      call.GetString("arguments") == " { \"path\" : \"docs/MODEL.md\" } " &&
      call.GetString("output").find("document result") != std::string::npos);
  ModerationContent moderation;
  ChatPlanError moderation_error;
  Q4T_CHECK(CollectModerationOutput(*run.Output().Snapshot().Find("output"),
                                    &moderation, &moderation_error));
  Q4T_CHECK(moderation.text &&
            OutputJson(moderation.records).find("document result") !=
                std::string::npos);
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run, 1));
  Q4T_CHECK(run.ResultJson().find("mcp-request-secret") == std::string::npos);
  for (const auto& request : server.Requests())
    if (request.rpc.GetString("method") == "tools/call")
      Q4T_CHECK(request.headers.at("authorization") ==
                    "Bearer mcp-request-secret" &&
                request.headers.at("x-private") == "mcp-header-secret");
  return true;
}

Q4T_TEST(
    responses_mcp_invalid_history_fails_before_network_and_stored_items_strip_secrets) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ExecutionRequest(server);
  auto tool = request.GetArray("tools")->array[0];
  Set(&tool, "defer_loading", Boolean(true));
  Set(&request, "tools",
      Array({tool, Object({{"type", String("tool_search")}})}));
  Set(&request, "input",
      Array({Object({{"type", String("function_call_output")},
                     {"call_id", String("orphan")},
                     {"output", String("fake result")}})}));
  ResponsesExecution run(objects);
  Q4T_CHECK(!run.Prepare(request, config, ExecutionAccess(), 42).ok());
  Q4T_CHECK(server.Requests().empty());
  ResponsesState state(objects);
  const auto resolved =
      state.ResolveItems(Array({Object({{"type", String("additional_tools")},
                                        {"role", String("developer")},
                                        {"tools", Array({tool})}})}),
                         ExecutionAccess(), 20);
  Q4T_CHECK(ExecutionOk(resolved));
  Q4T_CHECK(OutputJson(resolved.value).find("mcp-request-secret") ==
                std::string::npos &&
            OutputJson(resolved.value).find("mcp-header-secret") ==
                std::string::npos);
  return true;
}

Q4T_TEST(responses_mcp_execution_approval_resume_and_retry_never_repeat_call) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  const auto request = ExecutionRequest(server, "always");
  ResponsesExecution first(objects);
  Q4T_CHECK(ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)));
  std::string wire;
  Q4T_CHECK(ExecutionOk(first.Begin(&wire)) && ModelCall(&first, false, &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(first.RunMcp(1, &waiting, {})) && waiting &&
            !server.calls);
  const auto approval =
      first.Output().Snapshot().GetArray("output")->array.back();
  Q4T_CHECK(approval.GetString("type") == "mcp_approval_request");
  Q4T_CHECK(ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  ResponsesExecution resumed(objects);
  Q4T_CHECK(ExecutionOk(
      resumed.Prepare(ApprovalResponse(request, approval, true, first.Id()),
                      config, ExecutionAccess(), 44)));
  Q4T_CHECK(resumed.PendingMcpResume() && ExecutionOk(resumed.Begin(&wire)));
  Q4T_CHECK(ExecutionOk(resumed.RunMcpApprovals({})) && server.calls == 1 &&
            !resumed.PendingMcpResume());
  Q4T_CHECK(ContinueExecution(&resumed, config) && FinishExecution(&resumed));
  ResponsesExecution retry(objects);
  Q4T_CHECK(ExecutionOk(
      retry.Prepare(ApprovalResponse(request, approval, true, resumed.Id()),
                    config, ExecutionAccess(), 45)));
  Q4T_CHECK(ExecutionOk(retry.Begin(&wire)) &&
            ExecutionOk(retry.RunMcpApprovals({})) && server.calls == 1);
  Q4T_CHECK(ContinueExecution(&retry, config) && FinishExecution(&retry));
  return true;
}

Q4T_TEST(responses_mcp_execution_denial_and_truncation_do_not_dispatch) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  const auto request = ExecutionRequest(server, "always");
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(
      ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
      ExecutionOk(first.Begin(&wire)) && ModelCall(&first, false, &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(first.RunMcp(1, &waiting, {})) && waiting);
  const auto approval =
      first.Output().Snapshot().GetArray("output")->array.back();
  Q4T_CHECK(ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  ResponsesExecution denied(objects);
  Q4T_CHECK(ExecutionOk(denied.Prepare(
                ApprovalResponse(request, approval, false, first.Id()), config,
                ExecutionAccess(), 44)) &&
            ExecutionOk(denied.Begin(&wire)));
  Q4T_CHECK(
      ExecutionOk(denied.RunMcpApprovals({})) && !server.calls &&
      denied.Output().Snapshot().GetArray("output")->array.back().GetString(
          "status") == "failed");
  Q4T_CHECK(ContinueExecution(&denied, config) && FinishExecution(&denied));
  ResponsesExecution partial(objects);
  Q4T_CHECK(ExecutionOk(partial.Prepare(ExecutionRequest(server), config,
                                        ExecutionAccess(), 42)) &&
            ExecutionOk(partial.Begin(&wire)) &&
            ModelCall(&partial, true, &wire));
  Q4T_CHECK(ExecutionOk(partial.Finish(true, 0, {}, 43, &wire)) &&
            !server.calls);
  Q4T_CHECK(wire.find("response.mcp_call_arguments.done") == wire.npos);
  return true;
}

Q4T_TEST(
    responses_mcp_prepared_restore_reuses_catalog_and_requires_credentials) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  for (const bool credentials : {true, false}) {
    ResponsesExecution first(objects);
    auto request = ExecutionRequest(server, "never", credentials);
    Q4T_CHECK(
        ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)));
    Json frozen;
    Q4T_CHECK(ExecutionOk(first.FreezeInputs(&frozen)));
    const auto before = server.Requests().size();
    ResponsesExecution restored(objects);
    const auto result =
        restored.RestoreInputs(frozen, config, ExecutionAccess(), 42);
    if (credentials) {
      Q4T_CHECK(result.code == "mcp_credentials_required" &&
                result.http_status == 400);
    } else {
      Q4T_CHECK(ExecutionOk(result) && restored.HasMcp());
      std::string wire;
      Q4T_CHECK(ExecutionOk(restored.Begin(&wire)) &&
                FinishExecution(&restored));
    }
    Q4T_CHECK(server.Requests().size() == before && !server.calls);
  }
  return true;
}

Q4T_TEST(responses_mcp_list_failure_and_moderation_preserve_core_text) {
  Server server;
  server.handler = [](const Request& request, int fd, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/list") return false;
    Send(fd, Http(503));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(run.Prepare(ExecutionRequest(server), config,
                                    ExecutionAccess(), 42)) &&
            !run.HasMcp());
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)));
  Q4T_CHECK(wire.find("response.mcp_list_tools.failed") != wire.npos);
  Q4T_CHECK(ExecutionOk(run.HoldUnreviewedOutput()));
  run.DiscardUnreviewedOutput();
  Q4T_CHECK(FinishExecution(&run) && !server.calls);
  return true;
}

Q4T_TEST(responses_mcp_remote_unknown_is_durable_and_does_not_retry) {
  Server server;
  server.handler = [&](const Request& request, int, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/call") return false;
    ++server.calls;
    return true;  // Connection lost after accepting the call.
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(
      run.Prepare(ExecutionRequest(server), config, ExecutionAccess(), 42)));
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting &&
            server.calls == 1);
  const auto call = run.Output().Snapshot().GetArray("output")->array.back();
  Q4T_CHECK(call.GetString("status") == "failed" &&
            OutputJson(*call.Find("error")).find("mcp_result_unknown") !=
                std::string::npos);
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run, 1));
  ResponsesMcpState state(objects, config);
  const auto durable = state.Inspect(call.GetString("id"), ExecutionAccess());
  Q4T_CHECK(ExecutionOk(durable) &&
            durable.value.GetString("state") == "unknown");
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(
      ExecutionRequest(server).GetArray("tools")->array.front(), &definition,
      &error));
  Q4T_CHECK(
      !state.Permit(call.GetString("id"), definition, ExecutionAccess()).ok() &&
      server.calls == 1);
  return true;
}

Q4T_TEST(responses_mcp_batch_restart_rebinds_original_file_and_counts_input) {
  Server server;
  ExecutionFixture fixture;
  const auto config = ExecutionConfig(server);
  const auto access = [] {
    const auto state = ExecutionAccess();
    return BinaryAccess{state.scope, state.deadline, state.cancelled};
  };
  BatchInputRow row;
  row.line = 1;
  row.custom_id = "mcp-authenticated-batch";
  row.body = ExecutionRequest(server);
  const auto line = OutputJson(Object({{"custom_id", String(row.custom_id)},
                                       {"method", String("POST")},
                                       {"url", String("/v1/responses")},
                                       {"body", row.body}})) +
                    '\n';
  row.wire_bytes = line.size();
  std::string id;
  {
    ObjectStore objects(fixture.options);
    FileUpload upload(objects, access(), "multipart/form-data; boundary=mcp");
    const auto multipart =
        "--mcp\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n"
        "batch\r\n--mcp\r\nContent-Disposition: form-data; name=\"file\"; "
        "filename=\"mcp.jsonl\"\r\n\r\n" +
        line + "\r\n--mcp--\r\n";
    Q4T_CHECK(ExecutionOk(upload.Feed(multipart, access().deadline)));
    const auto file = upload.Finish();
    Q4T_CHECK(ExecutionOk(file));
    const auto batch = BatchState(objects).Create(
        access(), {file.value.GetString("id"), "/v1/responses", Object()});
    Q4T_CHECK(ExecutionOk(batch));
    id = batch.value.GetString("id");
    BatchPreparedInput prepared;
    Q4T_CHECK(ExecutionOk(BatchPreparation(objects).Prepare(
        access(), id, "/v1/responses", row, config, &prepared)));
    Q4T_CHECK(OutputJson(prepared.inline_input).find("mcp-request-secret") ==
              std::string::npos);
    BatchRowPosition position{row.line,
                              row.offset,
                              row.wire_bytes,
                              Digest(row.custom_id),
                              prepared.reference,
                              prepared.binding_sha256,
                              prepared.inline_input};
    Q4T_CHECK(ExecutionOk(BatchState(objects).AppendValidationPage(
        access(), id, std::span(&position, 1))));
    BatchPreparation(objects).Published(access(), &prepared, false);
  }
  // The explicit user-uploaded Batch File is the only credential source;
  // neither the encrypted catalog nor the prepared Response contains a key.
  ObjectStore objects(fixture.options);
  BatchInputRow original;
  Q4T_CHECK(ExecutionOk(
      BatchState(objects).ReadInputRow(access(), id, 0, &original)));
  Q4T_CHECK(original.body.GetArray("tools")->array.front().GetString(
                "authorization") == "mcp-request-secret");
  const auto checkpoint = BatchState(objects).Row(access(), id, 0);
  Q4T_CHECK(ExecutionOk(checkpoint));
  Json frozen;
  Q4T_CHECK(ExecutionOk(BatchPreparation(objects).Read(
      access(), id, "/v1/responses", original, checkpoint.value, &frozen)));
  Q4T_CHECK(OutputJson(frozen).find("mcp-request-secret") ==
                std::string::npos &&
            OutputJson(frozen).find("mcp-header-secret") == std::string::npos);
  auto changed = original;
  Set(&changed.body, "input", String("substituted row"));
  Json rejected;
  Q4T_CHECK(BatchPreparation(objects)
                .Read(access(), id, "/v1/responses", changed, checkpoint.value,
                      &rejected)
                .code == "batch_prepared_input_invalid");
  const auto requests = server.Requests().size();
  BatchTokenCodec codec;
  codec.encode = [](std::string_view text, std::vector<uint32_t>* tokens) {
    tokens->assign(text.begin(), text.end());
    return q4t::Status{};
  };
  codec.decode = [](const auto& tokens, std::string* text) {
    text->assign(tokens.begin(), tokens.end());
    return q4t::Status{};
  };
  BatchInputBudget input;
  BatchRateBudget rate;
  Q4T_CHECK(
      ExecutionOk(MeasureBatchInput(objects, "/v1/responses", original, frozen,
                                    config, access(), codec, &input, &rate)));
  Q4T_CHECK(input.tokens > 0 && input.exact && !rate.input_exact &&
            rate.prompt_capacity >= input.tokens && rate.output > 0);
  ResponsesExecution restored(objects);
  Q4T_CHECK(ExecutionOk(restored.RestoreInputs(
      frozen, config, ExecutionAccess(), 42, nullptr, &original.body)));
  Q4T_CHECK(server.Requests().size() == requests && !server.calls);
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(restored.Begin(&wire)) &&
            ModelCall(&restored, false, &wire));
  Q4T_CHECK(ExecutionOk(restored.RunMcp(1, &waiting, {})) && !waiting &&
            server.calls == 1);
  Q4T_CHECK(ContinueExecution(&restored, config) && FinishExecution(&restored));
  for (const auto& request : server.Requests()) {
    if (request.rpc.GetString("method") != "tools/call") continue;
    Q4T_CHECK(request.headers.at("authorization") ==
                  "Bearer mcp-request-secret" &&
              request.headers.at("x-private") == "mcp-header-secret");
  }
  return true;
}

Q4T_TEST(responses_mcp_frozen_credential_source_must_match_exact_request) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  const auto request = ExecutionRequest(server);
  ResponsesExecution first(objects);
  Q4T_CHECK(ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)));
  Json frozen;
  Q4T_CHECK(ExecutionOk(first.FreezeInputs(&frozen)));
  const auto requests = server.Requests().size();
  for (int mutation = 0; mutation < 3; ++mutation) {
    auto changed = request;
    if (mutation == 0)
      Set(&changed, "input", String("different authenticated row"));
    else
      Set(&Field(changed, "tools").array.front(),
          mutation == 1 ? "authorization" : "server_url",
          String(mutation == 1 ? "different-key" : "https://example.test"));
    ResponsesExecution restored(objects);
    Q4T_CHECK(restored
                  .RestoreInputs(frozen, config, ExecutionAccess(), 42, nullptr,
                                 &changed)
                  .code == "mcp_credential_source_changed");
  }
  Q4T_CHECK(server.Requests().size() == requests && !server.calls);
  return true;
}

Q4T_TEST(responses_mcp_missing_live_credentials_never_initialize_remote) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(ExecutionOk(first.Prepare(ExecutionRequest(server), config,
                                      ExecutionAccess(), 42)) &&
            ExecutionOk(first.Begin(&wire)) && FinishExecution(&first));
  auto request = ExecutionRequest(server, "never", false);
  Set(&request, "previous_response_id", String(first.Id()));
  ResponsesExecution next(objects);
  const auto requests = server.Requests().size();
  Q4T_CHECK(ExecutionOk(next.Prepare(request, config, ExecutionAccess(), 44)) &&
            ExecutionOk(next.Begin(&wire)) && ModelCall(&next, false, &wire));
  bool waiting = false;
  Q4T_CHECK(next.RunMcp(1, &waiting, {}).code == "mcp_credentials_required");
  Q4T_CHECK(server.Requests().size() == requests && !server.calls);
  Q4T_CHECK(ExecutionOk(next.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_mcp_discovery_loads_remote_only_when_activated) {
  for (const int variant : {0, 1, 2}) {
    Server server;
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    auto request = ExecutionRequest(server);
    Set(&Field(request, "tools").array.front(), "defer_loading", Boolean(true));
    Field(request, "tools")
        .array.push_back(Object({{"type", String("tool_search")}}));
    if (variant == 2) {
      auto definition = request;
      for (auto& tool : Field(definition, "tools").array)
        std::erase_if(tool.object, [](const auto& field) {
          return field.first == "authorization" || field.first == "headers";
        });
      auto saved = PromptRegistry(objects).Create(
          Object({{"request", std::move(definition)},
                  {"variables", Object()}}),
          config, ExecutionAccess());
      Q4T_CHECK(ExecutionOk(saved) && server.Requests().empty());
      request = Object({{"prompt", Object({{"id", *saved.value.Find("id")}})},
                         {"tools", *request.Find("tools")}});
    }
    auto run = std::make_unique<ResponsesExecution>(objects);
    Q4T_CHECK(
        ExecutionOk(run->Prepare(request, config, ExecutionAccess(), 42)));
    Q4T_CHECK(server.Requests().empty() && !run->HasMcp());
    if (variant == 1) {
      Json prepared;
      Q4T_CHECK(ExecutionOk(run->FreezeInputs(&prepared)));
      run = std::make_unique<ResponsesExecution>(objects);
      Q4T_CHECK(ExecutionOk(run->RestoreInputs(
          prepared, config, ExecutionAccess(), 42, nullptr, &request)));
      Q4T_CHECK(server.Requests().empty() && !run->HasMcp());
    }
    std::string wire;
    Q4T_CHECK(ExecutionOk(run->Begin(&wire)));
    ResponseDelta search;
    search.kind = ResponseDelta::Kind::kTool;
    search.tool_start = true;
    search.call = {
        0, "discover_mcp", std::string(kResponsesToolSearchModelName),
        OutputJson(
            Object({{"arguments_json", String(R"({"paths":["project"]})")}})),
        false};
    Q4T_CHECK(run->Append(search, {}, &wire));
    bool waiting = true;
    Q4T_CHECK(ExecutionOk(run->RunToolSearches(1, &waiting, {})) && !waiting);
    Q4T_CHECK(server.Requests().empty());
    ResponsesGenerationPlan next;
    std::string listing;
    Q4T_CHECK(ExecutionOk(
        run->ContinueFileSearch(8, config, {}, &next, [&](const auto& part) {
          listing += part;
          return true;
        })));
    Q4T_CHECK(run->HasMcp() && !server.Requests().empty() && !server.calls);
    Q4T_CHECK(listing.find("response.mcp_list_tools.completed") !=
              listing.npos);
    ResponseDelta call;
    call.kind = ResponseDelta::Kind::kTool;
    call.tool_start = true;
    call.call = {0, "discovered_remote",
                 ResponsesMcpModelName("project", "read_project"),
                 R"({"path":"docs/MODEL.md"})", false};
    Q4T_CHECK(run->Append(call, {}, &wire));
    Q4T_CHECK(ExecutionOk(run->RunMcp(1, &waiting, {})) && !waiting &&
              server.calls == 1);
    Q4T_CHECK(ContinueExecution(run.get(), config) &&
              FinishExecution(run.get()));
    size_t catalogs = 0;
    for (const auto& item : run->Output().Snapshot().GetArray("output")->array)
      catalogs += item.GetString("type") == "mcp_list_tools";
    Q4T_CHECK(catalogs == 1 && run->ResultJson().find("mcp-request-secret") ==
                                   std::string::npos);
  }
  return true;
}

Q4T_TEST(responses_mcp_current_caller_policy_gets_new_catalog) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ExecutionRequest(server);
  Set(&Field(request, "tools").array.front(), "allowed_callers",
      Array({String("programmatic")}));
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(
      ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
      ExecutionOk(first.Begin(&wire)) && FinishExecution(&first));
  const auto catalog =
      first.Output().Snapshot().GetArray("output")->array[0].GetString("id");
  request = ExecutionRequest(server);
  Set(&request, "previous_response_id", String(first.Id()));
  ResponsesExecution next(objects);
  Q4T_CHECK(ExecutionOk(next.Prepare(request, config, ExecutionAccess(), 44)) &&
            ExecutionOk(next.Begin(&wire)));
  Q4T_CHECK(next.Output().Snapshot().GetArray("output")->array[0].GetString(
                "id") != catalog);
  Q4T_CHECK(ModelCall(&next, false, &wire));
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(next.RunMcp(1, &waiting, {})) && !waiting &&
            server.calls == 1);
  Q4T_CHECK(ContinueExecution(&next, config) && FinishExecution(&next));
  return true;
}

Q4T_TEST(responses_mcp_policy_refresh_does_not_approve_original_call) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(ExecutionOk(first.Prepare(ExecutionRequest(server, "always"),
                                      config, ExecutionAccess(), 42)) &&
            ExecutionOk(first.Begin(&wire)) && ModelCall(&first, false, &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(first.RunMcp(1, &waiting, {})) && waiting &&
            !server.calls);
  const auto approval =
      first.Output().Snapshot().GetArray("output")->array.back().GetString(
          "id");
  Q4T_CHECK(ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  auto request = ExecutionRequest(server);
  Set(&request, "previous_response_id", String(first.Id()));
  ResponsesExecution next(objects);
  Q4T_CHECK(ExecutionOk(next.Prepare(request, config, ExecutionAccess(), 44)) &&
            ExecutionOk(next.Begin(&wire)) && ModelCall(&next, false, &wire));
  Q4T_CHECK(ExecutionOk(next.RunMcp(1, &waiting, {})) && !waiting &&
            server.calls == 1);
  Q4T_CHECK(ContinueExecution(&next, config) && FinishExecution(&next));
  const auto original =
      ResponsesMcpState(objects, config).Inspect(approval, ExecutionAccess());
  Q4T_CHECK(ExecutionOk(original) &&
            original.value.GetString("state") == "approval" &&
            !original.value.Find("decision"));
  return true;
}

Q4T_TEST(
    responses_mcp_failed_catalog_recovers_on_new_request_not_frozen_replay) {
  Server server;
  std::atomic<bool> failed{true};
  server.handler = [&](const Request& request, int fd, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/list" || !failed)
      return false;
    Send(fd, Http(503));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ExecutionRequest(server);
  ResponsesExecution first(objects);
  Q4T_CHECK(ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)));
  Json frozen;
  Q4T_CHECK(ExecutionOk(first.FreezeInputs(&frozen)));
  const auto requests = server.Requests().size();
  failed = false;
  ResponsesExecution restored(objects);
  Q4T_CHECK(ExecutionOk(restored.RestoreInputs(
      frozen, config, ExecutionAccess(), 42, nullptr, &request)));
  Q4T_CHECK(server.Requests().size() == requests && !restored.HasMcp());
  std::string wire;
  Q4T_CHECK(ExecutionOk(restored.Begin(&wire)) && FinishExecution(&restored));
  Set(&request, "previous_response_id", String(restored.Id()));
  ResponsesExecution next(objects);
  Q4T_CHECK(ExecutionOk(next.Prepare(request, config, ExecutionAccess(), 44)) &&
            ExecutionOk(next.Begin(&wire)) && ModelCall(&next, false, &wire));
  Q4T_CHECK(server.Requests().size() > requests);
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(next.RunMcp(1, &waiting, {})) && !waiting &&
            server.calls == 1);
  Q4T_CHECK(ContinueExecution(&next, config) && FinishExecution(&next));
  return true;
}

Q4T_TEST(responses_mcp_client_discovery_binds_new_credentials_and_real_call) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  const auto search_tool = Object(
      {{"type", String("tool_search")}, {"execution", String("client")}});
  auto request = ExecutionRequest(server);
  const auto remote_tools = *request.Find("tools");
  Set(&request, "tools", Array({search_tool}));
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(
      ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
      ExecutionOk(first.Begin(&wire)));
  ResponseDelta search;
  search.kind = ResponseDelta::Kind::kTool;
  search.tool_start = true;
  search.call = {0, "client_discover",
                 std::string(kResponsesToolSearchModelName),
                 OutputJson(Object({{"arguments_json", String("{}")}})), false};
  Q4T_CHECK(first.Append(search, {}, &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(first.RunToolSearches(1, &waiting, {})) && waiting);
  Q4T_CHECK(ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  const auto call_id =
      first.Output().Snapshot().GetArray("output")->array[0].GetString(
          "call_id");
  Q4T_CHECK(server.Requests().empty());
  Set(&request, "previous_response_id", String(first.Id()));
  Set(&request, "input",
      Array({Object({{"type", String("tool_search_output")},
                     {"call_id", String(call_id)},
                     {"tools", remote_tools}})}));
  ResponsesExecution next(objects);
  Q4T_CHECK(ExecutionOk(next.Prepare(request, config, ExecutionAccess(), 44)));
  Json frozen;
  Q4T_CHECK(ExecutionOk(next.FreezeInputs(&frozen)) &&
            OutputJson(frozen).find("mcp-request-secret") == std::string::npos);
  Q4T_CHECK(ExecutionOk(next.Begin(&wire)));
  ResponseDelta call;
  call.kind = ResponseDelta::Kind::kTool;
  call.tool_start = true;
  call.call = {0, "new_mcp", ResponsesMcpModelName("project", "read_project"),
               R"({"path":"docs/MODEL.md"})", false};
  Q4T_CHECK(next.Append(call, {}, &wire));
  Q4T_CHECK(ExecutionOk(next.RunMcp(1, &waiting, {})) && !waiting &&
            server.calls == 1);
  Q4T_CHECK(ContinueExecution(&next, config) && FinishExecution(&next));
  return true;
}
