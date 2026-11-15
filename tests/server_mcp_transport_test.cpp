#include "q4t/server/mcp_transport.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cstring>
#include <mutex>
#include <sstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/lifetime.h"
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
  Q4T_CHECK(Invoke(server, &receipt, "tools/call").ok());
  Q4T_CHECK(receipt.call_permitted && !receipt.outcome_unknown &&
            server.calls == 1);
  Q4T_CHECK(receipt.result.GetArray("content")->array[0].GetString("text") ==
            "document result");
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
