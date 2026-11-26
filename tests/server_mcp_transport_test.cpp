#include "q4t/server/mcp_transport.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/mcp_tunnel.h"
#include "q4t/server/mcp_tunnel_agent.h"
#include "../src/server/mcp_tunnel_agent_internal.h"
#include "../src/server/file_fetch_internal.h"
#include "../src/server/mcp_tunnel_transport_internal.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <mutex>
#include <set>
#include <sstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/agent_session.h"
#include "q4t/server/batch_preparation.h"
#include "q4t/server/batch_state.h"
#include "q4t/server/files.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_graph.h"
#include "q4t/server/prompt_registry.h"
#include "q4t/server/responses_mcp_state.h"
#include "q4t/server/realtime_mcp_catalog.h"
#include "q4t/server/realtime_mcp_execution.h"
#include "q4t/server/realtime_mcp_item.h"
#include "q4t/server/realtime_mcp_calls.h"
#include "q4t/server/realtime_mcp_history.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/realtime_conversation_session.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/server/responses_execution.h"
#include "q4t/server/responses_jobs.h"
#include "q4t/server/responses_owner.h"
#include "q4t/server/responses_tokens.h"
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

Q4T_TEST(mcp_transport_reads_remote_uri_only_after_owned_permission) {
  Server server;
  const std::string uri = "file:///etc/hostname";
  std::atomic<int> reads{0};
  server.handler = [&](const Request& r, int fd, std::stop_token) {
    if (r.rpc.GetString("method") == "initialize") {
      Send(fd,
           Http(200,
                OutputJson(Rpc(
                    r,
                    Object(
                        {{"protocolVersion", String("2025-06-18")},
                         {"capabilities", Object({{"resources", Object()}})},
                         {"serverInfo", Object({{"name", String("resources")},
                                                {"version", String("1")}})}}))),
                "application/json", "Mcp-Session-Id: resource-session\r\n"));
      return true;
    }
    if (r.rpc.GetString("method") != "resources/read") return false;
    ++reads;
    Send(
        fd,
        Http(200,
             OutputJson(Rpc(
                 r, Object({{"contents",
                             Array({Object(
                                 {{"uri", String(uri)},
                                  {"text", String("remote fixture content")},
                                  {"mimeType", String("text/plain")}})})}})))));
    return true;
  };
  auto endpoint = server.Endpoint();
  endpoint.authorization = "resource-test-token";
  const auto params = Object({{"uri", String(uri)}});
  McpTransportReceipt receipt;
  auto invoke = [&](const std::function<ObjectReply()>& permit) {
    return InvokeMcp(Access(), endpoint, server.Options(), "resources/read",
                     params, permit, &receipt);
  };
  Q4T_CHECK(invoke({}).code == "mcp_operation_invalid");
  Q4T_CHECK(server.Requests().empty());
  Q4T_CHECK(invoke([] {
              return Error(403, "fixture_denied", "No grant");
            }).code == "fixture_denied");
  Q4T_CHECK(!reads && !receipt.call_permitted);
  Q4T_CHECK(invoke([] { return ObjectReply{}; }).ok());
  Q4T_CHECK(reads == 1 && !server.calls && receipt.call_permitted &&
            !receipt.outcome_unknown);
  Q4T_CHECK(
      receipt.result.GetArray("contents")->array.front().GetString("text") ==
      "remote fixture content");
  size_t requests = 0;
  for (const auto& r : server.Requests()) {
    if (r.rpc.GetString("method") != "resources/read") continue;
    ++requests;
    Q4T_CHECK(r.target == "/mcp" &&
              r.rpc.Find("params")->GetString("uri") == uri);
    Q4T_CHECK(r.headers.at("authorization") == "Bearer resource-test-token");
    Q4T_CHECK(r.headers.at("mcp-session-id") == "resource-session");
  }
  Q4T_CHECK(requests == 1);
  return true;
}

Q4T_TEST(mcp_transport_resource_capability_and_operation_preflight) {
  Server server;
  McpTransportReceipt receipt;
  for (const auto& params : {Object({{"uri", String("../secret")}}),
                             Object({{"uri", String("file:///secret")},
                                     {"path", String("/secret")}})}) {
    Q4T_CHECK(InvokeMcp(
                  Access(), server.Endpoint(), server.Options(),
                  "resources/read", params, [] { return ObjectReply{}; },
                  &receipt)
                  .code == "mcp_operation_invalid");
  }
  Q4T_CHECK(server.Requests().empty());
  bool granted = false;
  const auto result = InvokeMcp(
      Access(), server.Endpoint(), server.Options(), "resources/read",
      Object({{"uri", String("file:///report")}}),
      [&] {
        granted = true;
        return ObjectReply{};
      },
      &receipt);
  Q4T_CHECK(result.code == "mcp_resources_unavailable" && !granted &&
            !receipt.call_permitted);
  for (const auto& r : server.Requests())
    Q4T_CHECK(r.rpc.GetString("method") != "resources/read");
  return true;
}

Q4T_TEST(mcp_transport_resource_result_failures_never_repeat_read) {
  for (const int variant : {0, 1, 2}) {
    Server server;
    std::atomic<int> reads{0};
    server.handler = [&](const Request& r, int fd, std::stop_token) {
      const auto method = r.rpc.GetString("method");
      if (method == "initialize") {
        const auto result =
            Object({{"protocolVersion", String("2025-06-18")},
                    {"capabilities", Object({{"resources", Object()}})},
                    {"serverInfo", Object({{"name", String("resources")},
                                           {"version", String("1")}})}});
        Send(fd, Http(200, OutputJson(Rpc(r, result))));
        return true;
      }
      if (method != "resources/read") return false;
      ++reads;
      if (variant == 2) return true;
      const auto resource =
          Object({{"uri", String("remote:report.pdf")},
                  {"mimeType", String("application/pdf")},
                  {"blob", String(variant ? "!invalid" : "JVBERg==")}});
      Send(fd, Http(200, OutputJson(Rpc(
                             r, Object({{"contents", Array({resource})}})))));
      return true;
    };
    McpTransportReceipt receipt;
    const auto result = InvokeMcp(
        Access(), server.Endpoint(), server.Options(), "resources/read",
        Object({{"uri", String("remote:report.pdf")}}),
        [] { return ObjectReply{}; }, &receipt);
    Q4T_CHECK(reads == 1 && !server.calls && receipt.call_permitted);
    Q4T_CHECK(receipt.outcome_unknown == (variant == 2));
    if (!variant) {
      Q4T_CHECK(result.ok());
      Q4T_CHECK(
          receipt.result.GetArray("contents")->array[0].GetString("blob") ==
          "JVBERg==");
    } else {
      Q4T_CHECK(!result.ok());
      if (variant == 1) Q4T_CHECK(result.code == "mcp_protocol_error");
    }
  }
  return true;
}

Q4T_TEST(mcp_transport_bad_native_content_is_known_without_tool_replay) {
  for (const auto* content :
       {R"({"type":"text","text":false})",
        R"({"type":"audio","mimeType":"audio/wav","data":"Zh=="})",
        R"({"type":"resource","resource":{"uri":"x:a"}})"}) {
    Server server;
    Json part;
    Q4T_CHECK(Parse(content, &part));
    server.handler = [&](const Request& r, int fd, std::stop_token) {
      if (r.rpc.GetString("method") != "tools/call") return false;
      ++server.calls;
      Send(fd,
           Http(200, OutputJson(Rpc(r, Object({{"content", Array({part})}})))));
      return true;
    };
    McpTransportReceipt receipt;
    Q4T_CHECK(Invoke(server, &receipt, "tools/call").code ==
              "mcp_protocol_error");
    Q4T_CHECK(server.calls == 1 && receipt.call_permitted &&
              !receipt.outcome_unknown);
  }
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

Q4T_TEST(mcp_transport_exact_target_keeps_legacy_post_on_credential_path) {
  Server source;
  source.handler = [](const Request& r, int fd, std::stop_token stop) {
    if (r.verb == "POST") {
      Send(fd, Http(405));
    } else {
      Send(fd, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
               "Transfer-Encoding: chunked\r\n\r\n");
      Chunk(fd, "event: endpoint\ndata: /other?session=fixture\n\n");
      Stall(stop);
    }
    return true;
  };
  auto endpoint = source.Endpoint();
  endpoint.authorization = "fixture-only";
  endpoint.exact_target = true;
  McpTransportReceipt receipt;
  Q4T_CHECK(InvokeMcp(Access(), endpoint, source.Options(), "tools/list", Object(),
                      {}, &receipt).code == "mcp_endpoint_denied");
  const auto seen = source.Requests();
  Q4T_CHECK(seen.size() == 2);
  for (const auto& req : seen) Q4T_CHECK(req.target == "/mcp");
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
  ~ExecutionFixture() { CloseMcpSessions(); }
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
      Object({{"type", String("mcp")},
              {"server_label", String("project")},
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
Json ProgramRequest(const Server& server, std::string approval = "never",
                    bool credentials = true) {
  auto request = ExecutionRequest(server, std::move(approval), credentials);
  Set(&Field(request, "tools").array[0], "allowed_callers",
      Array({String("programmatic")}));
  Field(request, "tools")
      .array.push_back(Object({{"type", String("programmatic_tool_calling")}}));
  return request;
}
bool ProgramCode(ResponsesExecution* run, std::string code, std::string* wire) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.tool_start = true;
  delta.call = {0, "model_program", kResponsesProgramModelName,
                OutputJson(Object({{"code", String(std::move(code))}})), false};
  return run->Append(delta, "", wire).ok();
}
Json OutputItem(const ResponsesExecution& run, std::string_view type) {
  for (const auto& item : run.Output().Snapshot().GetArray("output")->array)
    if (item.GetString("type") == type) return item;
  return {};
}
void AddClientTool(Json* request) {
  Field(*request, "tools")
      .array.push_back(
          Object({{"type", String("function")},
                  {"name", String("local")},
                  {"allowed_callers", Array({String("programmatic")})},
                  {"parameters", Object({{"type", String("object")}})}}));
}
}  // namespace

Q4T_TEST(
    responses_mcp_program_actual_call_standard_items_and_private_authority) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesExecution run(objects);
  const auto config = ExecutionConfig(server);
  auto request = ProgramRequest(server);
  Set(&request, "stream", Boolean(true));
  Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
  const auto descriptor = OutputJson(run.Generation().generation_request);
  Q4T_CHECK(descriptor.find("mcp:project") != descriptor.npos &&
            descriptor.find("mcp-request-secret") == descriptor.npos &&
            descriptor.find(server.Endpoint().url) == descriptor.npos);
  std::string wire, full;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) &&
            ProgramCode(&run,
                        "const r = await tools['mcp:project']"
                        ".read_project({path:'docs/MODEL.md'});"
                        "text(r.content[0].text);",
                        &wire));
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.RunPrograms(1, &waiting,
                                        [&](const auto& part) {
                                          full += part;
                                          return true;
                                        })) &&
            !waiting && server.calls == 1);
  const auto call = OutputItem(run, "mcp_call");
  Q4T_CHECK(call.GetString("status") == "completed" && !call.Has("caller") &&
            !call.Has("call_id") && OutputItem(run, "function_call").IsNull());
  Q4T_CHECK(OutputItem(run, "program_output").GetString("result") ==
            "document result");
  Q4T_CHECK(full.find("response.mcp_call_arguments.done") != full.npos &&
            full.find("response.mcp_call.completed") != full.npos &&
            full.find("q4t_program_mcp_intent") == full.npos &&
            full.find("program_origin") == full.npos);
  ResponsesMcpState state(objects, config);
  const auto owned = state.Inspect(call.GetString("id"), ExecutionAccess());
  Q4T_CHECK(
      ExecutionOk(owned) && owned.value.GetString("caller") == "programmatic" &&
      owned.value.Find("program_origin")->GetInt("index", -1) == 0 &&
      OutputJson(owned.value).find("mcp-request-secret") == std::string::npos);
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run, 1));
  return true;
}

Q4T_TEST(
    responses_mcp_program_approval_and_denial_resume_the_original_program) {
  for (const bool approve : {true, false}) {
    Server server;
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    const auto request = ProgramRequest(server, "always");
    ResponsesExecution first(objects);
    std::string wire;
    Q4T_CHECK(
        ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
        ExecutionOk(first.Begin(&wire)) &&
        ProgramCode(&first,
                    "const r=await tools['mcp:project']"
                    ".read_project({}); text(!!r.isError);",
                    &wire));
    bool waiting = false;
    Q4T_CHECK(ExecutionOk(first.RunPrograms(1, &waiting, {})) && waiting &&
              server.calls == 0 && !first.HasAsyncTools());
    const auto approval = OutputItem(first, "mcp_approval_request");
    Q4T_CHECK(!approval.IsNull() &&
              ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
    ResponsesExecution resumed(objects);
    Q4T_CHECK(ExecutionOk(resumed.Prepare(
                  ApprovalResponse(request, approval, approve, first.Id()),
                  config, ExecutionAccess(), 44)) &&
              resumed.PendingProgramResume() && resumed.PendingMcpResume() &&
              ExecutionOk(resumed.Begin(&wire)));
    Q4T_CHECK(ExecutionOk(resumed.RunMcpApprovals({})) &&
              ExecutionOk(resumed.RunPrograms(0, &waiting, {})) && !waiting &&
              server.calls == (approve ? 1 : 0));
    Q4T_CHECK(OutputItem(resumed, "program_output").GetString("result") ==
              (approve ? "false" : "true"));
    Q4T_CHECK(ContinueExecution(&resumed, config) && FinishExecution(&resumed));
    ResponsesExecution retry(objects);
    Q4T_CHECK(ExecutionOk(retry.Prepare(
                  ApprovalResponse(request, approval, approve, resumed.Id()),
                  config, ExecutionAccess(), 45)) &&
              !retry.PendingProgramResume() &&
              ExecutionOk(retry.Begin(&wire)) &&
              ExecutionOk(retry.RunMcpApprovals({})) &&
              server.calls == (approve ? 1 : 0));
    Q4T_CHECK(ContinueExecution(&retry, config) && FinishExecution(&retry));
  }
  return true;
}

Q4T_TEST(responses_mcp_program_parallel_client_result_preserves_causal_order) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ProgramRequest(server);
  AddClientTool(&request);
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(
      ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
      ExecutionOk(first.Begin(&wire)) &&
      ProgramCode(&first, R"js(
const remote=tools['mcp:project'].read_project({n:1});
const local=tools.local({});
const winner=await Promise.race([remote.then(()=>"remote"),local.then(()=>"local")]);
await local;
const last=await tools['mcp:project'].read_project({n:3});
text(winner+":"+last.content[0].text);
)js",
                  &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(first.RunPrograms(1, &waiting, {})) && waiting &&
            server.calls == 1);
  const auto child = OutputItem(first, "function_call");
  Q4T_CHECK(!child.IsNull() &&
            ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  Set(&request, "previous_response_id", String(first.Id()));
  Set(&request, "input",
      Array({Object({{"type", String("function_call_output")},
                     {"call_id", *child.Find("call_id")},
                     {"caller", *child.Find("caller")},
                     {"output", String("{\"value\":2}")}})}));
  ResponsesExecution resumed(objects);
  Q4T_CHECK(
      ExecutionOk(resumed.Prepare(request, config, ExecutionAccess(), 44)) &&
      ExecutionOk(resumed.Begin(&wire)) &&
      ExecutionOk(resumed.RunPrograms(0, &waiting, {})) && !waiting &&
      server.calls == 2);
  Q4T_CHECK(OutputItem(resumed, "program_output").GetString("result") ==
            "remote:document result");
  Q4T_CHECK(ContinueExecution(&resumed, config) && FinishExecution(&resumed));
  return true;
}

Q4T_TEST(responses_mcp_program_branches_bind_calls_to_prior_client_results) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ProgramRequest(server);
  AddClientTool(&request);
  ResponsesExecution parent(objects);
  std::string wire;
  bool waiting = false;
  Q4T_CHECK(
      ExecutionOk(parent.Prepare(request, config, ExecutionAccess(), 42)) &&
      ExecutionOk(parent.Begin(&wire)) &&
      ProgramCode(&parent,
                  "const v=await tools.local({});"
                  "const r=await tools['mcp:project'].read_project({n:v.n});"
                  "text(v.n+':'+r.content[0].text);",
                  &wire) &&
      ExecutionOk(parent.RunPrograms(1, &waiting, {})) && waiting);
  const auto first = OutputItem(parent, "function_call");
  Q4T_CHECK(!first.IsNull() &&
            ExecutionOk(parent.Finish(false, 1, {}, 43, &wire)));
  Set(&request, "previous_response_id", String(parent.Id()));
  ResponsesMcpState state(objects, config);
  std::set<std::string> ids, contexts;
  for (int n : {2, 3}) {
    Set(&request, "input",
        Array({Object(
            {{"type", String("function_call_output")},
             {"call_id", *first.Find("call_id")},
             {"caller", *first.Find("caller")},
             {"output", String(OutputJson(Object({{"n", Number(n)}})))}})}));
    ResponsesExecution branch(objects);
    Q4T_CHECK(
        ExecutionOk(branch.Prepare(request, config, ExecutionAccess(), 44)) &&
        ExecutionOk(branch.Begin(&wire)) &&
        ExecutionOk(branch.RunPrograms(0, &waiting, {})) && !waiting);
    const auto call = OutputItem(branch, "mcp_call");
    const auto owned = state.Inspect(call.GetString("id"), ExecutionAccess());
    Q4T_CHECK(ExecutionOk(owned));
    const auto* origin = owned.value.Find("program_origin");
    Q4T_CHECK(origin && origin->GetString("result_context").size() == 64 &&
              origin->Find("call") &&
              origin->Find("call")->GetInt("after_results") == 1 &&
              contexts.insert(origin->GetString("result_context")).second &&
              ids.insert(call.GetString("id")).second &&
              OutputItem(branch, "program_output").GetString("result") ==
                  std::to_string(n) + ":document result");
    Q4T_CHECK(ContinueExecution(&branch, config) && FinishExecution(&branch));
  }
  return server.calls == 2;
}

Q4T_TEST(responses_mcp_program_remote_error_keeps_native_structured_content) {
  Server server;
  server.handler = [&](const Request& r, int fd, std::stop_token) {
    if (r.rpc.GetString("method") != "tools/call") return false;
    ++server.calls;
    const auto result = Object(
        {{"content", Array({Object({{"type", String("text")},
                                    {"text", String("missing field")}})})},
         {"structuredContent", Object({{"reason", String("missing")}})},
         {"isError", Boolean(true)},
         {"_meta", Object({{"trace", String("original")}})}});
    Send(fd, Http(200, OutputJson(Rpc(r, result))));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesExecution run(objects);
  const auto config = ExecutionConfig(server);
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Prepare(ProgramRequest(server), config,
                                    ExecutionAccess(), 42)) &&
            ExecutionOk(run.Begin(&wire)) &&
            ProgramCode(&run, R"js(
const result=await tools['mcp:project'].read_project({});
text([result.isError,result.structuredContent.reason,result._meta.trace,
      result.content[0].text].join(":"));
)js",
                        &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(run.RunPrograms(1, &waiting, {})) && !waiting &&
            server.calls == 1);
  Q4T_CHECK(OutputItem(run, "program_output").GetString("result") ==
            "true:missing:original:missing field");
  Q4T_CHECK(OutputItem(run, "mcp_call").GetString("status") == "failed");
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
  return true;
}

Q4T_TEST(responses_mcp_program_parallel_native_calls_and_serial_policy) {
  for (const bool parallel : {true, false}) {
    Server server;
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    auto request = ProgramRequest(server);
    Set(&request, "parallel_tool_calls", Boolean(parallel));
    ResponsesExecution run(objects);
    std::string wire;
    Q4T_CHECK(
        ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)) &&
        ExecutionOk(run.Begin(&wire)) &&
        ProgramCode(
            &run,
            "const rs=await Promise.all(["
            "tools['mcp:project'].read_project({n:1}),"
            "tools['mcp:project'].read_project({n:2})]);text(rs.length);",
            &wire));
    bool waiting = true;
    Q4T_CHECK(ExecutionOk(run.RunPrograms(1, &waiting, {})) && !waiting &&
              server.calls == 2 &&
              OutputItem(run, "program_output").GetString("result") == "2");
    Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run, 1));
  }
  return true;
}

Q4T_TEST(responses_mcp_execution_media_reaches_model_and_frozen_continuation) {
  Server server;
  const std::string png =
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYoiECAA"
      "KEAPFkHeRLAAAAAElFTkSuQmCC";
  const auto native =
      Object({{"content",
               Array({Object({{"type", String("image")},
                              {"mimeType", String("image/png")},
                              {"data", String(png)}}),
                      Object({{"type", String("resource")},
                              {"resource",
                               Object({{"uri", String("project:document")},
                                       {"mimeType", String("text/plain")},
                                       {"blob", String("aGVsbG8=")}})}})})}});
  server.handler = [&](const Request& r, int fd, std::stop_token) {
    if (r.rpc.GetString("method") != "tools/call") return false;
    ++server.calls;
    Send(fd, Http(200, OutputJson(Rpc(r, native))));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  auto config = ExecutionConfig(server);
  config.allow_media = true;
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(
      run.Prepare(ExecutionRequest(server), config, ExecutionAccess(), 42)));
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
  Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)) &&
            !run.PendingFileSheets());
  Q4T_CHECK(run.FileUsage()->files == 2 && run.FileUsage()->images == 1);
  ResponsesGenerationPlan next;
  Q4T_CHECK(ExecutionOk(run.ContinueFileSearch(8, config, {}, &next)));
  const auto prompt = OutputJson(next.input.messages);
  Q4T_CHECK(prompt.find("image_url") != prompt.npos &&
            prompt.find("hello") != prompt.npos);
  const auto response = run.PublicResponse();
  const auto& output = response.GetArray("output")->array;
  const auto call = std::find_if(
      output.begin(), output.end(),
      [](const auto& v) { return v.GetString("type") == "mcp_call"; });
  Q4T_CHECK(call != output.end() &&
            call->GetString("output") == OutputJson(native));
  ModerationContent content;
  ChatPlanError error;
  Q4T_CHECK(CollectModerationOutput(*response.GetArray("output"), &content,
                                    &error, &run.Envelope().mcp_content));
  Q4T_CHECK(content.images.array.size() == 1);
  Q4T_CHECK(FinishExecution(&run));
  const auto before = server.Requests().size();
  auto request = Object({{"previous_response_id", String(run.Id())},
                         {"input", String("Explain the image and note")},
                         {"reasoning", Object({{"effort", String("none")}})}});
  ResponsesExecution resumed(objects);
  Q4T_CHECK(
      ExecutionOk(resumed.Prepare(request, config, ExecutionAccess(), 43)));
  Q4T_CHECK(resumed.FileUsage()->images == 1 &&
            OutputJson(resumed.Generation().input.messages).find("hello") !=
                std::string::npos &&
            server.calls == 1 && server.Requests().size() == before);
  auto count_config = config;
  count_config.count_input_only = true;
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan counted;
  Q4T_CHECK(ExecutionOk(PrepareResponsesTokenCount(
      request, count_config, objects, ExecutionAccess(), &envelope, &counted)));
  Q4T_CHECK(OutputJson(counted.input.messages) ==
            OutputJson(resumed.Generation().input.messages));
  Q4T_CHECK(server.Requests().size() == before && server.calls == 1);
  return true;
}

Q4T_TEST(responses_mcp_execution_csv_uses_owned_steps_and_frozen_receipts) {
  Server server;
  server.handler = [&](const Request& r, int fd, std::stop_token) {
    if (r.rpc.GetString("method") != "tools/call") return false;
    ++server.calls;
    const auto native = Object(
        {{"content",
          Array({Object(
              {{"type", String("resource")},
               {"resource",
                Object(
                    {{"uri", String("project:sheet")},
                     {"mimeType", String("text/csv")},
                     {"blob",
                      String("bmFtZSx2YWx1ZQphbHBoYSw0MgpiZXRhLDcK")}})}})})}});
    Send(fd, Http(200, OutputJson(Rpc(r, native))));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(
      run.Prepare(ExecutionRequest(server), config, ExecutionAccess(), 42)));
  Q4T_CHECK(run.FileOutputBudget() == 8192);
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
  Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)) &&
            run.PendingFileSheets());
  ResponsesGenerationPlan next;
  Q4T_CHECK(run.ContinueFileSearch(8, config, {}, &next).code ==
            "file_preparation_incomplete");
  FileSheetStep step;
  Q4T_CHECK(ExecutionOk(run.NextFileSheet(config, &step)));
  Q4T_CHECK(ExecutionOk(run.CompleteFileSheet(
      step,
      R"({"summary":"CPU native step receipt: alpha 42 and beta 7",)"
      R"("headers":["name: row label","value: quantity"]})",
      "stop", 100, 12, config)));
  Q4T_CHECK(!run.PendingFileSheets() && run.FileUsage()->model_steps == 1 &&
            run.FileOutputBudget() == 8180);
  Q4T_CHECK(ExecutionOk(run.ContinueFileSearch(8, config, {}, &next)));
  Q4T_CHECK(OutputJson(next.input.messages).find("CPU native step receipt") !=
                std::string::npos &&
            FinishExecution(&run));
  ResponsesExecution resumed(objects);
  const auto request = Object({{"previous_response_id", String(run.Id())},
                               {"input", String("Use the original sheet")}});
  Q4T_CHECK(
      ExecutionOk(resumed.Prepare(request, config, ExecutionAccess(), 43)));
  Q4T_CHECK(!resumed.PendingFileSheets() &&
            resumed.FileUsage()->model_steps == 0 && server.calls == 1);
  return true;
}

Q4T_TEST(responses_mcp_execution_resources_read_once_and_unknown_stays_frozen) {
  for (const bool disconnect : {false, true}) {
    Server server;
    std::atomic<int> reads{0};
    server.handler = [&](const Request& r, int fd, std::stop_token) {
      const auto method = r.rpc.GetString("method");
      if (method == "initialize") {
        Send(fd, Http(200,
                      OutputJson(Rpc(
                          r, Object({{"protocolVersion", String("2025-06-18")},
                                     {"capabilities",
                                      Object({{"tools", Object()},
                                              {"resources", Object()}})},
                                     {"serverInfo",
                                      Object({{"name", String("media")},
                                              {"version", String("1")}})}})))));
        return true;
      }
      if (method == "tools/call") {
        ++server.calls;
        Send(fd,
             Http(200,
                  OutputJson(Rpc(
                      r, Object({{"content",
                                  Array({Object(
                                      {{"type", String("resource_link")},
                                       {"uri", String("file:///remote")},
                                       {"name", String("document")}})})}})))));
        return true;
      }
      if (method != "resources/read") return false;
      ++reads;
      if (!disconnect)
        Send(
            fd,
            Http(
                200,
                OutputJson(Rpc(
                    r,
                    Object({{"contents",
                             Array({Object(
                                 {{"uri", String("file:///remote")},
                                  {"text",
                                   String("actual remote resource")}})})}})))));
      return true;
    };
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    ResponsesExecution run(objects);
    Q4T_CHECK(ExecutionOk(
        run.Prepare(ExecutionRequest(server), config, ExecutionAccess(), 42)));
    std::string wire;
    bool waiting = true;
    Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
    Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
    Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)) && reads == 1);
    ResponsesGenerationPlan next;
    Q4T_CHECK(ExecutionOk(run.ContinueFileSearch(8, config, {}, &next)));
    Q4T_CHECK(OutputJson(next.input.messages)
                  .find(disconnect ? "unknown" : "actual remote resource") !=
              std::string::npos);
    Q4T_CHECK(FinishExecution(&run));
    ResponsesExecution resumed(objects);
    const auto request = Object({{"previous_response_id", String(run.Id())},
                                 {"input", String("Continue")}});
    Q4T_CHECK(
        ExecutionOk(resumed.Prepare(request, config, ExecutionAccess(), 43)));
    Q4T_CHECK(reads == 1 && server.calls == 1);
  }
  return true;
}

Q4T_TEST(responses_mcp_count_prepares_multiple_sheets_on_one_output_budget) {
  Server server;
  const std::string csv = "bmFtZSx2YWx1ZQphbHBoYSw0MgpiZXRhLDcK";
  server.handler = [&](const Request& r, int fd, std::stop_token) {
    if (r.rpc.GetString("method") != "tools/call") return false;
    ++server.calls;
    const auto native = Object(
        {{"content",
          Array({Object({{"type", String("resource")},
                         {"resource", Object({{"uri", String("project:sheet")},
                                              {"mimeType", String("text/csv")},
                                              {"blob", String(csv)}})}})})}});
    Send(fd, Http(200, OutputJson(Rpc(r, native))));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  auto config = ExecutionConfig(server);
  ResponsesExecution original(objects);
  Q4T_CHECK(ExecutionOk(original.Prepare(ExecutionRequest(server), config,
                                         ExecutionAccess(), 42)));
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(original.Begin(&wire)) &&
            ModelCall(&original, false, &wire));
  Q4T_CHECK(ExecutionOk(original.RunMcp(1, &waiting, {})) && !waiting);
  Q4T_CHECK(FinishExecution(&original));
  const auto request = Object(
      {{"previous_response_id", String(original.Id())},
       {"input",
        Array({Object(
            {{"role", String("user")},
             {"content", Array({Object({{"type", String("input_file")},
                                        {"filename", String("other.csv")},
                                        {"file_data", String(csv)}})})}})})}});
  config.count_input_only = true;
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan counted;
  FileExpansionUsage usage;
  uint64_t budget = 0;
  int callbacks = 0;
  const auto prepare = [&](FileExpansion& files) {
    ++callbacks;
    if (budget != 4096)
      return Error(500, "fixture_budget", "Expected both sheet reservations");
    FileSheetStep step;
    auto result = files.NextSheet(config, &step);
    if (!result.ok()) return result;
    return files.CompleteSheet(
        step,
        R"({"summary":"CPU count receipt: alpha 42, beta 7",)"
        R"("headers":["name: row label","value: quantity"]})",
        "stop", 111, 19);
  };
  Q4T_CHECK(ExecutionOk(PrepareResponsesTokenCount(
      request, config, objects, ExecutionAccess(), &envelope, &counted, prepare,
      &usage, nullptr, false, &budget)));
  Q4T_CHECK(callbacks == 2 && server.calls == 1 && usage.model_steps == 2 &&
            usage.model_input_tokens == 222 && usage.model_output_tokens == 38);
  Q4T_CHECK(OutputJson(counted.input.messages).find("CPU count receipt") !=
            std::string::npos);
  // The MCP preparation persisted under the original invocation. Only the
  // new, unsaved input attachment needs a model step on the following count.
  budget = 0;
  const auto second = [&](FileExpansion& files) {
    if (budget != 2048)
      return Error(500, "fixture_budget", "Frozen MCP sheet was not reused");
    FileSheetStep step;
    auto result = files.NextSheet(config, &step);
    if (!result.ok()) return result;
    return files.CompleteSheet(
        step,
        R"({"summary":"CPU count receipt: alpha 42, beta 7",)"
        R"("headers":["name: row label","value: quantity"]})",
        "stop", 111, 19);
  };
  Q4T_CHECK(ExecutionOk(PrepareResponsesTokenCount(
      request, config, objects, ExecutionAccess(), &envelope, &counted, second,
      &usage, nullptr, false, &budget)));
  Q4T_CHECK(usage.model_steps == 1 && server.calls == 1);
  return true;
}

Q4T_TEST(responses_mcp_count_new_catalog_is_metadata_only_and_scope_bound) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  auto config = ExecutionConfig(server);
  config.count_input_only = true;
  auto request = ExecutionRequest(server);
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan counted;
  Q4T_CHECK(ExecutionOk(PrepareResponsesTokenCount(
      request, config, objects, ExecutionAccess(), &envelope, &counted)));
  Q4T_CHECK(server.calls == 0 &&
            counted.generation_request.GetArray("tools")->array.size() == 1);
  for (const auto& r : server.Requests())
    Q4T_CHECK(r.rpc.GetString("method") != "tools/call");
  const auto listing =
      std::find_if(counted.input.items.array.begin(),
                   counted.input.items.array.end(), [](const auto& item) {
                     return item.GetString("type") == "mcp_list_tools";
                   });
  Q4T_CHECK(listing != counted.input.items.array.end());
  Set(&request, "input", Array({*listing}));
  auto other = ExecutionAccess();
  other.scope = "client:other-count";
  const auto before = server.Requests().size();
  Q4T_CHECK(!PrepareResponsesTokenCount(request, config, objects, other,
                                        &envelope, &counted)
                 .ok());
  Q4T_CHECK(server.calls == 0 && server.Requests().size() == before);
  return true;
}

Q4T_TEST(responses_mcp_resource_real_transport_uses_sealed_single_read) {
  for (const bool disconnect : {false, true}) {
    Server server;
    std::atomic<int> reads{0};
    const std::string uri = "file:///etc/hostname";
    server.handler = [&](const Request& r, int fd, std::stop_token) {
      const auto method = r.rpc.GetString("method");
      if (method == "initialize") {
        const auto result =
            Object({{"protocolVersion", String("2025-06-18")},
                    {"capabilities",
                     Object({{"tools", Object()}, {"resources", Object()}})},
                    {"serverInfo", Object({{"name", String("resources")},
                                           {"version", String("1")}})}});
        Send(fd, Http(200, OutputJson(Rpc(r, result))));
        return true;
      }
      if (method == "tools/call") {
        ++server.calls;
        Send(fd,
             Http(200,
                  OutputJson(Rpc(
                      r, Object({{"content",
                                  Array({Object(
                                      {{"type", String("resource_link")},
                                       {"uri", String(uri)},
                                       {"name", String("hostname")}})})}})))));
        return true;
      }
      if (method != "resources/read") return false;
      ++reads;
      if (disconnect) return true;
      Send(fd,
           Http(200,
                OutputJson(Rpc(
                    r, Object({{"contents",
                                Array({Object(
                                    {{"uri", String(uri)},
                                     {"text",
                                      String("frozen remote data")}})})}})))));
      return true;
    };
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    ResponsesMcpState state(objects, config, "resp_resource");
    ResponsesMcpDefinition definition;
    ChatPlanError error;
    Q4T_CHECK(PlanResponsesMcpDefinition(
        ExecutionRequest(server).GetArray("tools")->array.front(), &definition,
        &error));
    McpTransportReceipt receipt;
    Q4T_CHECK(InvokeMcp({ExecutionAccess().scope, Clock::now() + 3s, {}},
                        definition.credentials, server.Options(), "tools/list",
                        Object(), {}, &receipt)
                  .ok());
    const auto catalog = state.Catalog(
        definition, *receipt.result.Find("tools"), ExecutionAccess());
    Q4T_CHECK(ExecutionOk(catalog));
    const auto proposed =
        state.Propose(catalog.value, definition, "read_project", Object(),
                      "direct", ExecutionAccess());
    Q4T_CHECK(ExecutionOk(proposed));
    const auto id = proposed.value.GetString("id");
    auto transport = InvokeMcp(
        {ExecutionAccess().scope, Clock::now() + 3s, {}},
        definition.credentials, server.Options(), "tools/call",
        Object({{"name", String("read_project")}, {"arguments", Object()}}),
        [&] { return state.Permit(id, definition, ExecutionAccess()); },
        &receipt);
    const auto final = state.Finish(id, receipt, transport, ExecutionAccess());
    Q4T_CHECK(ExecutionOk(final) &&
              final.value.GetString("status") == "completed");
    Q4T_CHECK(ExecutionOk(
        state.ProposeResource(id, uri, definition, ExecutionAccess())));
    transport = InvokeMcp(
        {ExecutionAccess().scope, Clock::now() + 3s, {}},
        definition.credentials, server.Options(), "resources/read",
        Object({{"uri", String(uri)}}),
        [&] {
          return state.PermitResource(id, uri, definition, ExecutionAccess());
        },
        &receipt);
    const auto resource =
        state.FinishResource(id, uri, receipt, transport, ExecutionAccess());
    Q4T_CHECK(ExecutionOk(resource) && reads == 1 && server.calls == 1);
    Q4T_CHECK(resource.value.GetString("state") ==
              (disconnect ? "unknown" : "completed"));
    if (!disconnect)
      Q4T_CHECK(resource.value.Find("result")
                    ->GetArray("contents")
                    ->array.front()
                    .GetString("text") == "frozen remote data");
    ResponsesMcpState reopened(objects, config);
    Q4T_CHECK(OutputJson(reopened.Resource(id, uri, ExecutionAccess()).value) ==
              OutputJson(resource.value));
    Q4T_CHECK(OutputJson(reopened.Authenticate(final.value, ExecutionAccess())
                             .value) == OutputJson(final.value));
    Q4T_CHECK(
        !reopened.ProposeResource(id, uri, definition, ExecutionAccess()).ok());
    Q4T_CHECK(
        !reopened.PermitResource(id, uri, definition, ExecutionAccess()).ok());
    Q4T_CHECK(reads == 1 && server.calls == 1);
  }
  return true;
}

Q4T_TEST(responses_mcp_program_unknown_result_is_reused_without_dispatch) {
  Server server;
  server.handler = [&](const Request& request, int, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/call") return false;
    ++server.calls;
    return true;  // The peer cannot know whether the tool performed its work.
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ProgramRequest(server, "never", false);
  Set(&request, "store", Boolean(false));
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(
      ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
      ExecutionOk(first.Begin(&wire)) &&
      ProgramCode(&first,
                  "const r=await tools['mcp:project']"
                  ".read_project({});text(!!r.isError);",
                  &wire));
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(first.RunPrograms(1, &waiting, {})) && !waiting &&
            server.calls == 1);
  const auto program = OutputItem(first, "program");
  Q4T_CHECK(OutputItem(first, "mcp_call")
                .Find("error")
                ->Find("content")
                ->GetString("code") == "mcp_result_unknown");
  Q4T_CHECK(ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  // Even dropping the published child from stateless history cannot replay it.
  Set(&request, "tools", Array());
  Set(&request, "input", Array({program}));
  const auto before = server.Requests().size();
  ResponsesExecution resumed(objects);
  Q4T_CHECK(
      ExecutionOk(resumed.Prepare(request, config, ExecutionAccess(), 44)) &&
      ExecutionOk(resumed.Begin(&wire)) &&
      ExecutionOk(resumed.RunPrograms(0, &waiting, {})) && !waiting &&
      server.calls == 1 && server.Requests().size() == before);
  Q4T_CHECK(OutputItem(resumed, "program_output").GetString("result") ==
            "true");
  Q4T_CHECK(ContinueExecution(&resumed, config) && FinishExecution(&resumed));
  return true;
}

Q4T_TEST(responses_mcp_program_original_input_and_output_schema_are_enforced) {
  for (const bool invalid_input : {true, false}) {
    Server server;
    server.handler = [&](const Request& request, int fd, std::stop_token) {
      if (request.rpc.GetString("method") != "tools/list") return false;
      auto tool = Tool();
      Json schema;
      if (!Parse(R"({"type":"object","properties":{"n":{"type":"integer"}},
                     "required":["n"],"additionalProperties":false})",
                 &schema))
        throw std::runtime_error("fixture schema");
      Set(&tool, "inputSchema", schema);
      Set(&tool, "outputSchema", schema);  // Default result lacks required n.
      Send(fd, Http(200, OutputJson(Rpc(request,
                                        Object({{"tools", Array({tool})}})))));
      return true;
    };
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    ResponsesExecution run(objects);
    std::string wire;
    Q4T_CHECK(ExecutionOk(run.Prepare(ProgramRequest(server), config,
                                      ExecutionAccess(), 42)) &&
              ExecutionOk(run.Begin(&wire)) &&
              ProgramCode(&run,
                          std::string("const r=await tools['mcp:project']"
                                      ".read_project({n:") +
                              (invalid_input ? "'wrong'" : "1") +
                              "});text(!!r.isError);",
                          &wire));
    bool waiting = false;
    const auto result = run.RunPrograms(1, &waiting, {});
    if (invalid_input) {
      Q4T_CHECK(result.code == "program_arguments_invalid" &&
                server.calls == 0);
      ResponsesExecution::ObserveError(&run, 502, result.message, result.code);
      Q4T_CHECK(ExecutionOk(run.CloseUnfinished()));
    } else {
      Q4T_CHECK(ExecutionOk(result) && !waiting && server.calls == 1 &&
                OutputItem(run, "mcp_call").GetString("status") == "failed" &&
                OutputItem(run, "program_output").GetString("result") ==
                    "true");
      Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run, 1));
    }
  }
  return true;
}

Q4T_TEST(
    responses_mcp_program_native_budget_and_aborted_vm_never_dispatch_extra) {
  for (const bool aborted : {true, false}) {
    Server server;
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    auto request = ProgramRequest(server);
    if (!aborted) Set(&request, "max_tool_calls", Number(1));
    ResponsesExecution run(objects);
    std::string wire;
    Q4T_CHECK(
        ExecutionOk(run.Prepare(request, ExecutionConfig(server),
                                ExecutionAccess(), 42)) &&
        ExecutionOk(run.Begin(&wire)) &&
        ProgramCode(
            &run,
            aborted
                ? "tools['mcp:project'].read_project({});throw Error('abort');"
                : "await tools['mcp:project'].read_project({});",
            &wire));
    bool waiting = false;
    const auto result = run.RunPrograms(1, &waiting, {});
    Q4T_CHECK(server.calls == 0);
    if (aborted) {
      Q4T_CHECK(ExecutionOk(result) &&
                OutputItem(run, "program_output").GetString("status") ==
                    "incomplete");
      Q4T_CHECK(ExecutionOk(run.Finish(false, 1, {}, 43, &wire)));
    } else {
      Q4T_CHECK(result.code == "mcp_execution_capacity");
      ResponsesExecution::ObserveError(&run, 503, result.message, result.code);
      Q4T_CHECK(ExecutionOk(run.CloseUnfinished()));
    }
    ResponsesExecution healthy(objects);
    Q4T_CHECK(ExecutionOk(healthy.Prepare(Object({{"input", String("hello")}}),
                                          ExecutionConfig(server),
                                          ExecutionAccess(), 44)) &&
              ExecutionOk(healthy.Begin(&wire)) && FinishExecution(&healthy));
  }
  return true;
}

Q4T_TEST(responses_mcp_program_unicode_and_nul_names_keep_exact_identity) {
  Server server;
  const std::string label("资料\0x", 8), name("read\0value", 10);
  server.handler = [&](const Request& request, int fd, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/list") return false;
    Send(fd,
         Http(200, OutputJson(Rpc(request,
                                  Object({{"tools", Array({Tool(name)})}})))));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  auto request = ProgramRequest(server);
  Set(&Field(request, "tools").array[0], "server_label", String(label));
  ResponsesExecution run(objects);
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Prepare(request, ExecutionConfig(server),
                                    ExecutionAccess(), 42)) &&
            ExecutionOk(run.Begin(&wire)) &&
            ProgramCode(&run,
                        "text((await tools[" +
                            OutputJson(String("mcp:" + label)) + "][" +
                            OutputJson(String(name)) +
                            "]({})).content[0].text);",
                        &wire));
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.RunPrograms(1, &waiting, {})) && !waiting &&
            server.calls == 1 &&
            OutputItem(run, "mcp_call").GetString("name") == name);
  for (const auto& request : server.Requests())
    if (request.rpc.GetString("method") == "tools/call")
      Q4T_CHECK(request.rpc.Find("params")->GetString("name") == name);
  Q4T_CHECK(ExecutionOk(run.Finish(false, 1, {}, 43, &wire)));
  return true;
}

Q4T_TEST(responses_mcp_program_continuation_intersects_current_authority) {
  for (const bool changed_target : {false, true}) {
    Server server;
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    auto request = ProgramRequest(server);
    AddClientTool(&request);
    ResponsesExecution first(objects);
    std::string wire;
    Q4T_CHECK(
        ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
        ExecutionOk(first.Begin(&wire)) &&
        ProgramCode(&first,
                    "await tools.local({});"
                    "text(await tools['mcp:project'].read_project({}));",
                    &wire));
    bool waiting = false;
    Q4T_CHECK(ExecutionOk(first.RunPrograms(1, &waiting, {})) && waiting);
    const auto child = OutputItem(first, "function_call");
    Q4T_CHECK(ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
    Set(&request, "previous_response_id", String(first.Id()));
    Set(&request, "input",
        Array({Object({{"type", String("function_call_output")},
                       {"call_id", *child.Find("call_id")},
                       {"caller", *child.Find("caller")},
                       {"output", String("{}")}})}));
    auto& current = Field(request, "tools").array[0];
    if (changed_target)
      Set(&current, "server_url", String(server.Endpoint().url + "/changed"));
    else
      Set(&current, "allowed_callers", Array({String("direct")}));
    ResponsesExecution resumed(objects);
    Q4T_CHECK(
        ExecutionOk(resumed.Prepare(request, config, ExecutionAccess(), 44)) &&
        ExecutionOk(resumed.Begin(&wire)));
    const auto result = resumed.RunPrograms(0, &waiting, {});
    Q4T_CHECK(result.code ==
                  (changed_target ? "mcp_server_changed" : "mcp_tool_denied") &&
              server.calls == 0);
    ResponsesExecution::ObserveError(&resumed, result.http_status,
                                     result.message, result.code);
    Q4T_CHECK(ExecutionOk(resumed.CloseUnfinished()));
  }
  return true;
}

Q4T_TEST(
    responses_mcp_program_stateless_approval_retains_original_credentials) {
  for (const bool credentials : {false, true}) {
    Server server;
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    auto request = ProgramRequest(server, "always", credentials);
    Set(&request, "store", Boolean(false));
    ResponsesExecution first(objects);
    std::string wire;
    Q4T_CHECK(
        ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
        ExecutionOk(first.Begin(&wire)) &&
        ProgramCode(&first,
                    "const r=await tools['mcp:project']"
                    ".read_project({});text(r.content[0].text);",
                    &wire));
    bool waiting = false;
    Q4T_CHECK(ExecutionOk(first.RunPrograms(1, &waiting, {})) && waiting &&
              ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
    const auto approval = OutputItem(first, "mcp_approval_request");
    auto history = *first.Output().Snapshot().GetArray("output");
    history.array.push_back(
        Object({{"type", String("mcp_approval_response")},
                {"approval_request_id", *approval.Find("id")},
                {"approve", Boolean(true)}}));
    Set(&request, "input", history);
    Set(&request, "tools", Array());
    const auto before = server.Requests().size();
    ResponsesExecution resumed(objects);
    Q4T_CHECK(
        ExecutionOk(resumed.Prepare(request, config, ExecutionAccess(), 44)) &&
        ExecutionOk(resumed.Begin(&wire)));
    const auto result = resumed.RunMcpApprovals({});
    if (credentials) {
      Q4T_CHECK(result.code == "mcp_credentials_required" &&
                server.calls == 0 && server.Requests().size() == before);
      ResponsesExecution::ObserveError(&resumed, 400, result.message,
                                       result.code);
      Q4T_CHECK(ExecutionOk(resumed.CloseUnfinished()));
      ResponsesMcpState authority(objects, config);
      Q4T_CHECK(authority.Inspect(approval.GetString("id"), ExecutionAccess())
                    .value.GetString("state") == "approval");
      Set(&request, "tools", *ProgramRequest(server, "always").Find("tools"));
      ResponsesExecution supplied(objects);
      Q4T_CHECK(ExecutionOk(
                    supplied.Prepare(request, config, ExecutionAccess(), 45)) &&
                ExecutionOk(supplied.Begin(&wire)) &&
                ExecutionOk(supplied.RunMcpApprovals({})) &&
                ExecutionOk(supplied.RunPrograms(0, &waiting, {})) &&
                !waiting && server.calls == 1 && FinishExecution(&supplied));
    } else {
      Q4T_CHECK(ExecutionOk(result) &&
                ExecutionOk(resumed.RunPrograms(0, &waiting, {})) && !waiting &&
                server.calls == 1 &&
                OutputItem(resumed, "program_output").GetString("result") ==
                    "document result");
      Q4T_CHECK(ContinueExecution(&resumed, config) &&
                FinishExecution(&resumed));
    }
  }
  return true;
}

Q4T_TEST(
    responses_mcp_program_count_and_frozen_queue_do_not_reexecute_history) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ProgramRequest(server);
  AddClientTool(&request);
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(
      ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
      ExecutionOk(first.Begin(&wire)) &&
      ProgramCode(&first,
                  "await tools['mcp:project'].read_project({n:1});"
                  "await tools.local({});"
                  "const r=await tools['mcp:project'].read_project({n:2});"
                  "text(r.content[0].text);",
                  &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(first.RunPrograms(1, &waiting, {})) && waiting &&
            server.calls == 1 &&
            ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  const auto child = OutputItem(first, "function_call");
  Set(&request, "previous_response_id", String(first.Id()));
  Set(&request, "input",
      Array({Object({{"type", String("function_call_output")},
                     {"call_id", *child.Find("call_id")},
                     {"caller", *child.Find("caller")},
                     {"output", String("{}")}})}));
  const auto before = server.Requests().size();
  auto counting = config;
  counting.count_input_only = true;
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan generation;
  Q4T_CHECK(ExecutionOk(PrepareResponsesTokenCount(request, counting, objects,
                                                   ExecutionAccess(), &envelope,
                                                   &generation)) &&
            server.Requests().size() == before &&
            OutputJson(envelope.request).find("mcp-request-secret") ==
                std::string::npos);
  auto tampered =
      Object({{"input", *first.Output().Snapshot().GetArray("output")}});
  for (auto& item : Field(tampered, "input").array)
    if (item.GetString("type") == "mcp_call")
      Set(&item, "output", String("forged"));
  Q4T_CHECK(PrepareResponsesTokenCount(tampered, counting, objects,
                                       ExecutionAccess(), &envelope,
                                       &generation)
                .code == "invalid_mcp_state");
  ResponsesExecution prepared(objects);
  Json frozen;
  Q4T_CHECK(
      ExecutionOk(prepared.Prepare(request, config, ExecutionAccess(), 44)) &&
      ExecutionOk(prepared.FreezeInputs(&frozen)) &&
      OutputJson(frozen).find("mcp-request-secret") == std::string::npos);
  ResponsesExecution restored(objects);
  Q4T_CHECK(ExecutionOk(restored.RestoreInputs(
                frozen, config, ExecutionAccess(), 45, nullptr, &request)) &&
            restored.PendingProgramResume() &&
            server.Requests().size() == before &&
            ExecutionOk(restored.Begin(&wire)) &&
            ExecutionOk(restored.RunPrograms(0, &waiting, {})) && !waiting &&
            server.calls == 2);
  Q4T_CHECK(OutputItem(restored, "program_output").GetString("result") ==
                "document result" &&
            ContinueExecution(&restored, config) && FinishExecution(&restored));
  return true;
}

Q4T_TEST(
    responses_mcp_program_cancelled_proposal_closes_without_remote_replay) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ProgramRequest(server);
  Set(&request, "store", Boolean(false));
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(
      ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
      ExecutionOk(first.Begin(&wire)) &&
      ProgramCode(&first,
                  "const r=await tools['mcp:project']"
                  ".read_project({});text(!!r.isError);",
                  &wire));
  bool waiting = false;
  const auto result = first.RunPrograms(1, &waiting, [&](const auto&) {
    return OutputItem(first, "mcp_call").IsNull();
  });
  Q4T_CHECK(result.http_status == 499 && server.calls == 0);
  ResponsesExecution::ObserveError(&first, 499, result.message, result.code);
  Q4T_CHECK(ExecutionOk(first.CloseUnfinished()));
  auto items = *first.Output().Snapshot().GetArray("output");
  Q4T_CHECK(!OutputItem(first, "mcp_call").IsNull());
  Set(&request, "input", std::move(items));
  ResponsesExecution resumed(objects);
  Q4T_CHECK(
      ExecutionOk(resumed.Prepare(request, config, ExecutionAccess(), 44)) &&
      ExecutionOk(resumed.Begin(&wire)) &&
      ExecutionOk(resumed.RunPrograms(0, &waiting, {})) && !waiting &&
      server.calls == 0 &&
      OutputItem(resumed, "program_output").GetString("result") == "true");
  Q4T_CHECK(ContinueExecution(&resumed, config) && FinishExecution(&resumed));
  return true;
}

Q4T_TEST(responses_mcp_program_file_preparation_preserves_native_result_type) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ProgramRequest(server);
  AddClientTool(&request);
  ResponsesExecution first(objects);
  std::string wire;
  Q4T_CHECK(
      ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)) &&
      ExecutionOk(first.Begin(&wire)) &&
      ProgramCode(&first,
                  "const r=await tools['mcp:project'].read_project({});"
                  "await tools.local({});text(r.content[0].text);",
                  &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(first.RunPrograms(1, &waiting, {})) && waiting &&
            server.calls == 1 &&
            ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  const auto child = OutputItem(first, "function_call");
  Set(&request, "previous_response_id", String(first.Id()));
  Set(&request, "input",
      Array(
          {Object({{"type", String("function_call_output")},
                   {"call_id", *child.Find("call_id")},
                   {"caller", *child.Find("caller")},
                   {"output", String("{}")}}),
           Object({{"role", String("user")},
                   {"content",
                    Array({Object(
                        {{"type", String("input_file")},
                         {"filename", String("note.txt")},
                         {"file_data",
                          String("data:text/plain;base64,aGVsbG8=")}})})}})}));
  ResponsesExecution resumed(objects);
  Q4T_CHECK(
      ExecutionOk(resumed.Prepare(request, config, ExecutionAccess(), 44)) &&
      ExecutionOk(resumed.Begin(&wire)) &&
      ExecutionOk(resumed.RunPrograms(0, &waiting, {})) && !waiting &&
      server.calls == 1 &&
      OutputItem(resumed, "program_output").GetString("result") ==
          "document result");
  Q4T_CHECK(ContinueExecution(&resumed, config) && FinishExecution(&resumed));
  return true;
}

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
  Q4T_CHECK(ExecutionOk(run.RestoreInputs(frozen, config, ExecutionAccess(), 42,
                                          nullptr, &request)));
  Q4T_CHECK(server.Requests().size() == listed);
  Q4T_CHECK(run.Envelope().options.Find("prompt")->GetString("version") == "1");
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
  const auto request =
      Object({{"prompt", Object({{"id", *saved.value.Find("id")}})}});
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
      Object({{"type", String("allowed_tools")},
              {"mode", String("required")},
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
  const auto saved =
      prompts.Create(McpPrompt(server), config, ExecutionAccess());
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
      auto input =
          Array({Object({{"type", String(variant == 1 ? "additional_tools"
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
    Q4T_CHECK(
        prompts.Append(id, 1, invalid, config, ExecutionAccess()).http_status ==
        400);
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

Q4T_TEST(responses_mcp_cleanup_and_reconciliation_allow_honest_continuation) {
  for (const int phase : {0, 1, 2, 3}) {
    Server server;
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    auto request = ExecutionRequest(server);
    Set(&request, "store", Boolean(phase != 0));
    ResponsesExecution run(objects);
    Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
    std::string wire;
    Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
    bool waiting = true, interrupted = false;
    try {
      Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, [&](const auto&) {
        if (phase != 3) {
          if (phase == 2 && !ExecutionOk(run.Checkpoint()))
            throw std::runtime_error("fixture checkpoint failure");
          throw std::runtime_error("fixture subscriber ended");
        }
        return true;
      })));
    } catch (const std::runtime_error& error) {
      Q4T_CHECK(std::string(error.what()) == "fixture subscriber ended");
      interrupted = true;
    }
    Q4T_CHECK(interrupted == (phase != 3));
    Json final;
    if (phase < 2) {
      Q4T_CHECK(ExecutionOk(run.CloseUnfinished()));
      final = run.Output().Snapshot();
    } else {
      if (phase == 3) Q4T_CHECK(ExecutionOk(run.Checkpoint()));
      auto reconciled = ResponsesState(objects).Interrupt(
          run.Id(), ExecutionAccess(), "fixture_interrupted", "owner ended");
      Q4T_CHECK(ExecutionOk(reconciled));
      final = reconciled.value;
    }
    Q4T_CHECK(server.calls == (phase == 3 ? 1 : 0));
    Json call;
    for (const auto& item : final.GetArray("output")->array) {
      if (item.GetString("type") == "mcp_call") call = item;
      if (item.GetString("type") == "mcp_list_tools")
        Q4T_CHECK(!item.Has("status"));
    }
    Q4T_CHECK(!call.IsNull());
    auto canonical = ResponsesMcpState(objects, config)
                         .Authenticate(call, ExecutionAccess());
    Q4T_CHECK(ExecutionOk(canonical));
    Q4T_CHECK(canonical.value.GetString("status") ==
              (phase == 3 ? "completed" : "failed"));
    if (phase == 2) Q4T_CHECK(OutputJson(canonical.value) == OutputJson(call));
    auto next = request;
    if (phase == 0)
      Set(&next, "input", *final.Find("output"));
    else {
      Set(&next, "previous_response_id", String(run.Id()));
      Set(&next, "input", String("Explain what actually happened"));
    }
    ResponsesExecution continued(objects);
    Q4T_CHECK(
        ExecutionOk(continued.Prepare(next, config, ExecutionAccess(), 44)));
    Q4T_CHECK(ExecutionOk(continued.Begin(&wire)) &&
              FinishExecution(&continued));
    Q4T_CHECK(server.calls == (phase == 3 ? 1 : 0));
  }
  return true;
}

Q4T_TEST(responses_mcp_cleanup_survives_storage_failure_without_reexecution) {
  for (const bool dispatched : {false, true}) {
    Server server;
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    const auto config = ExecutionConfig(server);
    ResponsesJobs jobs(objects, {}, [](auto&) {
      throw std::runtime_error("cleanup must not invoke model execution");
    });
    ScopeExit reset([] { DeferObjectSpawnForTesting(false); });
    ResponsesExecution run(objects, &jobs, true);
    auto request = ExecutionRequest(server);
    Set(&request, "store", Boolean(dispatched));
    Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
    std::string wire;
    Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
    bool waiting = false, interrupted = false;
    try {
      (void)run.RunMcp(1, &waiting, [](const auto&) -> bool {
        throw std::runtime_error("fixture transport owner ended");
      });
    } catch (const std::runtime_error&) {
      interrupted = true;
    }
    Q4T_CHECK(interrupted && jobs.Health().foreground_active == 1);
    Json call;
    for (const auto& item : run.Output().Snapshot().GetArray("output")->array)
      if (item.GetString("type") == "mcp_call") call = item;
    Q4T_CHECK(!call.IsNull());
    ResponsesMcpState calls(objects, config, run.Id());
    if (dispatched) {
      ResponsesMcpDefinition definition;
      ChatPlanError error;
      Q4T_CHECK(PlanResponsesMcpDefinition(
          request.GetArray("tools")->array.front(), &definition, &error));
      const auto owned = calls.Inspect(call.GetString("id"), ExecutionAccess());
      Q4T_CHECK(ExecutionOk(owned));
      const auto session = calls.Session(owned.value.GetString("catalog_id"),
                                         definition, ExecutionAccess());
      Q4T_CHECK(ExecutionOk(session));
      Q4T_CHECK(ExecutionOk(calls.Permit(call.GetString("id"), definition,
                                         ExecutionAccess(),
                                         session.value.Find("session"))));
    }
    // Keep normal operations on their production deadline. The simulated
    // storage controller failure is followed by a stuck replacement spawn.
    DeferObjectSpawnForTesting(true);
    FailObjectControllerForTesting();
    Q4T_CHECK(!run.CloseUnfinished().ok());
    Q4T_CHECK(jobs.Health().foreground_pending == 1);
    const auto starting = Clock::now() + 5s;
    while (
        (objects.Health().state != "starting" || !objects.Health().pending) &&
        Clock::now() < starting)
      std::this_thread::sleep_for(5ms);
    Q4T_CHECK(objects.Health().state == "starting" && objects.Health().pending);
    const auto errors_before = jobs.Health().foreground_errors;
    ResponsesExecution healthy(objects, &jobs, true);
    const auto plain =
        Object({{"input", String("ordinary text")}, {"store", Boolean(false)}});
    Q4T_CHECK(
        ExecutionOk(healthy.Prepare(plain, config, ExecutionAccess(), 44)));
    Q4T_CHECK(ExecutionOk(healthy.Begin(&wire)) && FinishExecution(&healthy));
    const auto first = Clock::now() + 5s;
    while (jobs.Health().foreground_errors == errors_before &&
           Clock::now() < first)
      std::this_thread::sleep_for(5ms);
    Q4T_CHECK(jobs.Health().foreground_errors > errors_before &&
              !jobs.Health().failed);
    DeferObjectSpawnForTesting(false);
    const auto until = Clock::now() + 8s;
    while (jobs.Health().foreground_owned && Clock::now() < until)
      std::this_thread::sleep_for(5ms);
    Q4T_CHECK(jobs.Health().foreground_owned == 0 &&
              jobs.Health().foreground_reconciled == 1 &&
              !jobs.Health().failed);
    auto recovered = calls.Authenticate(call, ExecutionAccess());
    Q4T_CHECK(ExecutionOk(recovered));
    Q4T_CHECK(
        recovered.value.Find("error")->Find("content")->GetString("code") ==
        (dispatched ? "mcp_result_unknown" : "mcp_execution_interrupted"));
    if (!dispatched)
      Q4T_CHECK(ResponsesState(objects)
                    .Retrieve(run.Id(), ExecutionAccess())
                    .http_status == 404);
    Q4T_CHECK(server.calls == 0);
  }
  return true;
}

Q4T_TEST(responses_mcp_cleanup_metadata_is_bounded_and_scope_bound) {
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesJobsOptions limits;
  limits.max_jobs = limits.max_client_jobs = 1;
  ResponsesJobs jobs(objects, limits, [](auto&) {
    throw std::runtime_error("unexpected executor");
  });
  uint64_t ticket = 0;
  Q4T_CHECK(ExecutionOk(
      jobs.Reserve(ExecutionAccess().scope, "resp_cleanup", &ticket)));
  const auto owner = CurrentResponseOwner();
  for (size_t i = 0; i < 64; ++i)
    Q4T_CHECK(ExecutionOk(jobs.TrackMcp(ticket, "cpu", owner,
                                        "mcp_cleanup_" + std::to_string(i))));
  Q4T_CHECK(ExecutionOk(jobs.TrackMcp(ticket, "cpu", owner, "mcp_cleanup_0")));
  Q4T_CHECK(jobs.TrackMcp(ticket, "cpu", owner, "mcp_overflow").http_status ==
            429);
  Q4T_CHECK(
      jobs.TrackMcp(ticket, "other", owner, "mcp_cleanup_0").http_status ==
      409);
  uint64_t extra = 0;
  Q4T_CHECK(jobs.Reserve("client:other", "resp_other", &extra).http_status ==
            429);
  jobs.Close(ticket, false);
  Q4T_CHECK(jobs.TrackMcp(ticket, "cpu", owner, "mcp_cleanup_0").http_status ==
            409);
  const auto until = Clock::now() + 5s;
  while (jobs.Health().foreground_owned && Clock::now() < until)
    std::this_thread::sleep_for(5ms);
  Q4T_CHECK(!jobs.Health().foreground_owned && !jobs.Health().failed);
  Q4T_CHECK(ExecutionOk(jobs.Reserve("client:other", "resp_other", &extra)));
  jobs.Close(extra, true);
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

namespace {
Json MappedRequest(const Server&, const std::string&, std::string);
ChatPlanConfig MappedConfig(const Server&, const std::string&, std::string);
}  // namespace

Q4T_TEST(responses_mcp_batch_restart_rebinds_original_file_and_counts_input) {
  for (bool mapped : {false, true}) {
    Server server;
    ExecutionFixture fixture;
    const auto config = mapped ? MappedConfig(server, "connector_id", "one")
                               : ExecutionConfig(server);
    const auto access = [] {
      const auto state = ExecutionAccess();
      return BinaryAccess{state.scope, state.deadline, state.cancelled};
    };
    BatchInputRow row;
    row.line = 1;
    row.custom_id = "mcp-authenticated-batch";
    row.body = mapped ? MappedRequest(server, "connector_id", "never")
                      : ExecutionRequest(server);
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
    Q4T_CHECK(
        OutputJson(frozen).find("mcp-request-secret") == std::string::npos &&
        OutputJson(frozen).find("mcp-header-secret") == std::string::npos);
    auto changed = original;
    Set(&changed.body, "input", String("substituted row"));
    Json rejected;
    Q4T_CHECK(BatchPreparation(objects)
                  .Read(access(), id, "/v1/responses", changed,
                        checkpoint.value, &rejected)
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
    Q4T_CHECK(ExecutionOk(MeasureBatchInput(objects, "/v1/responses", original,
                                            frozen, config, access(), codec,
                                            &input, &rate)));
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
    Q4T_CHECK(ContinueExecution(&restored, config) &&
              FinishExecution(&restored));
    for (const auto& request : server.Requests()) {
      if (request.rpc.GetString("method") != "tools/call") continue;
      Q4T_CHECK(request.headers.at("authorization") ==
                    "Bearer mcp-request-secret" &&
                request.headers.at("x-private") == "mcp-header-secret");
    }
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

Q4T_TEST(agent_mcp_required_listing_fails_before_session_publication) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto tool = Object({{"type", String("mcp")}, {"required", Boolean(true)},
                      {"server_label", String("records")},
                      {"transport", Object({{"type", String("http")},
                                            {"server_url", String(server.Endpoint().url)}})}});
  auto request = Object({{"input", String("read")},
                         {"agent", Object({{"model", String(config.model)},
                                           {"tools", Array({tool})}})},
                         {"environment", Object({{"type", String("none")}})}});
  server.handler = [](const Request&, int fd, std::stop_token) {
    Send(fd, Http(403));
    return true;
  };
  ResponsesExecution required(objects);
  Q4T_CHECK(PrepareAgentSessionExecution(objects, request, config, ExecutionAccess(),
      42, required).code == "agent_mcp_initialization_failed");
  auto rows = ListAgentSessions(objects, Object(), ExecutionAccess());
  Q4T_CHECK(ExecutionOk(rows) && rows.value.GetArray("data")->array.empty());
  auto& tools = Field(Field(request, "agent"), "tools");
  Set(&tools.array.front(), "required", Boolean(false));
  ResponsesExecution optional(objects);
  Q4T_CHECK(ExecutionOk(PrepareAgentSessionExecution(objects, request, config,
      ExecutionAccess(), 42, optional)));
  std::string wire;
  Q4T_CHECK(ExecutionOk(optional.Begin(&wire)) && ExecutionOk(optional.StartRunning(&wire)));
  Q4T_CHECK(FinishExecution(&optional) && !server.calls);
  return true;
}

Q4T_TEST(agent_mcp_queued_restore_uses_original_session_tool_configuration) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto tool = Object({{"type", String("mcp")}, {"required", Boolean(true)},
                      {"server_label", String("project")},
                      {"request_metadata", Object({{"step", String("original")}})},
                      {"transport", Object({{"type", String("http")},
                                            {"server_url", String(server.Endpoint().url)}})}});
  auto request = Object({{"input", String("read")},
                         {"agent", Object({{"model", String(config.model)},
                                           {"tools", Array({tool})}})},
                         {"environment", Object({{"type", String("none")}})}});
  std::string id, session, wire;
  {
    ResponsesExecution first(objects);
    auto created = PrepareAgentSessionExecution(objects, request, config, ExecutionAccess(), 42, first);
    Q4T_CHECK(ExecutionOk(created) && ExecutionOk(first.Begin(&wire)));
    id = first.Id();
    session = created.value.GetString("id");
  }
  auto changed = UpdateAgentSession(objects, session,
      Object({{"agent", Object({{"tools", Array()}})}}), config, ExecutionAccess());
  Q4T_CHECK(ExecutionOk(changed));
  const auto initialized = server.Requests().size();
  ResponsesExecution restored(objects);
  Q4T_CHECK(ExecutionOk(restored.RestoreQueued(id, config, ExecutionAccess())));
  Q4T_CHECK(ExecutionOk(restored.StartRunning(&wire)));
  Q4T_CHECK(server.Requests().size() == initialized && ModelCall(&restored, false, &wire));
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(restored.RunMcp(1, &waiting, {})) && !waiting && server.calls == 1);
  Q4T_CHECK(FinishExecution(&restored, 1));
  for (const auto& row : server.Requests()) {
    if (row.rpc.GetString("method") == "tools/call")
      Q4T_CHECK(row.rpc.Find("params")->Find("_meta")->GetString("step") == "original");
  }
  return true;
}

Q4T_TEST(responses_background_mcp_catalog_precedes_context_and_model_output) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore store(fixture.options);
  const auto config = ExecutionConfig(server);
  auto request = ExecutionRequest(server);
  Set(&request, "background", Boolean(true));
  Set(&request, "truncation", String("auto"));
  ResponsesExecution run(store);
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42, true)));
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ExecutionOk(run.StartRunning(&wire)));
  Q4T_CHECK(!OutputItem(run, "mcp_list_tools").IsNull());
  const auto measure = [](const auto&, auto*) { return q4t::Status{}; };
  Q4T_CHECK(ExecutionOk(run.FitContext(config, measure)));
  Q4T_CHECK(ExecutionOk(run.SelectCompaction(32, config)) && !run.CompactionSelected());
  Q4T_CHECK(ModelCall(&run, true, &wire));
  Q4T_CHECK(run.SelectCompaction(32, config).code == "response_state_invalid");
  Q4T_CHECK(run.FitContext(config, measure).code == "response_state_invalid");
  Q4T_CHECK(ExecutionOk(run.Cancel()));
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
          Object({{"request", std::move(definition)}, {"variables", Object()}}),
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

namespace {
// A stateful remote: resources exist only in the session that ran the tool.
struct StatefulMcp {
  std::mutex mutex;
  std::map<std::string, bool> sessions;
  std::atomic<int> initialized{0}, dispatched{0}, read{0}, closed{0};
  std::atomic<bool> expire_call{false}, expire_read{false};
  Server server;
  StatefulMcp() {
    server.handler = [this](const Request& r, int fd, std::stop_token) {
      const auto method = r.rpc.GetString("method");
      std::lock_guard lock(mutex);
      if (method == "initialize") {
        const auto id =
            "private-fixture-session-" + std::to_string(++initialized);
        sessions[id] = false;
        const auto result =
            Object({{"protocolVersion", String("2025-06-18")},
                    {"capabilities",
                     Object({{"tools", Object()}, {"resources", Object()}})},
                    {"serverInfo", Object({{"name", String("stateful")},
                                           {"version", String("1")}})}});
        Send(fd, Http(200, OutputJson(Rpc(r, result)), "application/json",
                      "Mcp-Session-Id: " + id + "\r\n"));
        return true;
      }
      const auto header = r.headers.find("mcp-session-id");
      if (header == r.headers.end() || !sessions.contains(header->second) ||
          !r.headers.contains("mcp-protocol-version") ||
          r.headers.at("mcp-protocol-version") != "2025-06-18") {
        Send(fd, Http(404));
        return true;
      }
      if (r.verb == "DELETE") {
        ++closed;
        sessions.erase(header->second);
        Send(fd, Http(204));
        return true;
      }
      if (method == "tools/call") {
        ++dispatched;
        if (expire_call.exchange(false)) {
          sessions.erase(header->second);
          Send(fd, Http(404));
          return true;
        }
        sessions[header->second] = true;
        const auto result = Object(
            {{"content", Array({Object({{"type", String("resource_link")},
                                        {"uri", String("resource://owned")},
                                        {"name", String("result")}})})}});
        Send(fd, Http(200, OutputJson(Rpc(r, result))));
        return true;
      }
      if (method == "resources/read") {
        ++read;
        if (expire_read.exchange(false)) {
          sessions.erase(header->second);
          Send(fd, Http(404));
          return true;
        }
        if (!sessions[header->second]) {
          Send(fd, Http(409));
          return true;
        }
        const auto result = Object(
            {{"contents",
              Array({Object({{"uri", String("resource://owned")},
                             {"text", String("same session resource")}})})}});
        Send(fd, Http(200, OutputJson(Rpc(r, result))));
        return true;
      }
      return false;
    };
  }
  ~StatefulMcp() { CloseMcpSessions(); }
  ObjectReply Invoke(
      McpSession& session, McpTransportReceipt& receipt,
      std::string_view method = "tools/list", BinaryAccess access = Access(),
      std::function<ObjectReply()> permit = [] { return ObjectReply{}; }) {
    return InvokeMcp(access, server.Endpoint(), server.Options(), method,
                     method == "tools/list" ? Object()
                     : method == "resources/read"
                         ? Object({{"uri", String("resource://owned")}})
                         : Call(),
                     permit, &receipt, {}, &session);
  }
};
bool BreakMcpOwner() {
  Server stalled;
  stalled.handler = [](const Request&, int, std::stop_token stop) {
    Stall(stop);
    return true;
  };
  auto access = Access();
  access.deadline = Clock::now() + 80ms;
  McpTransportReceipt receipt;
  return Invoke(stalled, &receipt, "tools/list", {}, access).http_status == 408;
}
}  // namespace

Q4T_TEST(mcp_session_stateful_list_call_resource_and_private_checkpoint) {
  StatefulMcp remote;
  McpSession session;
  McpTransportReceipt receipt;
  int saved = 0;
  session.checkpoint = [&](const Json& state) {
    ++saved;
    if (!state.GetString("session_id")
             .starts_with("private-fixture-session-") ||
        state.GetString("binding").size() != 64)
      return Error(500, "fixture_invalid", "Missing private state");
    return ObjectReply{};
  };
  Q4T_CHECK(remote.Invoke(session, receipt).ok());
  const auto identity = session.state.GetString("id");
  Q4T_CHECK(remote.Invoke(session, receipt, "tools/call").ok());
  Q4T_CHECK(!receipt.outcome_unknown && receipt.call_permitted);
  session.allow_new = false;
  Q4T_CHECK(remote.Invoke(session, receipt, "resources/read").ok());
  Q4T_CHECK(session.state.GetString("id") == identity && saved == 3 &&
            remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1 && remote.closed == 0);
  Q4T_CHECK(OutputJson(receipt.result).find("private-fixture-session-") ==
            std::string::npos);
  Q4T_CHECK(CloseMcpSessions() && remote.closed == 1);
  return true;
}

Q4T_TEST(mcp_session_binding_rejects_scope_target_credentials_and_policy) {
  StatefulMcp remote;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(remote.Invoke(session, receipt).ok());
  const auto requests = remote.server.Requests().size();
  for (int change = 0; change < 4; ++change) {
    auto access = Access();
    auto endpoint = remote.server.Endpoint();
    auto options = remote.server.Options();
    auto changed = session;
    if (change == 0) access.scope = "another-client";
    if (change == 1) endpoint.url += "?different=target";
    if (change == 2) endpoint.authorization = "different-credential";
    if (change == 3) options.timeout_ms -= 1;
    const auto result = InvokeMcp(access, endpoint, options, "tools/list",
                                  Object(), {}, &receipt, {}, &changed);
    Q4T_CHECK(result.code == "mcp_session_binding_changed");
  }
  Q4T_CHECK(remote.server.Requests().size() == requests);
  Q4T_CHECK(remote.Invoke(session, receipt, "tools/call").ok());
  return true;
}

Q4T_TEST(mcp_vault_renewal_binds_session_to_issuer_grant_and_exact_endpoint) {
  StatefulMcp remote;
  auto endpoint = remote.server.Endpoint();
  endpoint.url.replace(0, 4, "https");
  endpoint.exact_target = true;
  endpoint.vault_grant.assign(64, 'a');
  endpoint.authorization = "first";
  auto access = Access();
  // Parent binds the authenticated snapshot before checking cancellation.
  access.cancelled = [] { return true; };
  auto check = [&](McpSession& session) {
    McpTransportReceipt receipt;
    return InvokeMcp(access, endpoint, remote.server.Options(), "tools/list",
                     Object(), {}, &receipt, {}, &session);
  };
  McpSession session;
  Q4T_CHECK(check(session).http_status == 499);
  const auto binding = session.state.GetString("binding");
  endpoint.authorization = "replacement";
  Q4T_CHECK(check(session).http_status == 499 &&
            session.state.GetString("binding") == binding);
  endpoint.vault_grant.assign(64, 'b');
  Q4T_CHECK(check(session).code == "mcp_session_binding_changed");
  endpoint.vault_grant.assign(64, 'a');
  endpoint.headers["X-Project"] = "changed";
  Q4T_CHECK(check(session).code == "mcp_session_binding_changed");
  endpoint.headers.erase("X-Project");
  endpoint.url += "?other";
  Q4T_CHECK(check(session).code == "mcp_session_binding_changed");
  endpoint.vault_grant.clear();
  McpSession legacy;
  Q4T_CHECK(check(legacy).http_status == 499);
  endpoint.vault_grant.assign(64, 'a');
  Q4T_CHECK(check(legacy).http_status == 499);
  endpoint.authorization = "new";
  Q4T_CHECK(check(legacy).code == "mcp_session_binding_changed");
  endpoint.url.replace(0, 5, "http");
  Q4T_CHECK(check(session).code == "mcp_credentials_invalid");
  Q4T_CHECK(remote.server.Requests().empty());
  return true;
}

Q4T_TEST(mcp_session_unconfirmed_checkpoint_cannot_dispatch) {
  for (const bool unknown : {false, true}) {
    StatefulMcp remote;
    McpSession session;
    McpTransportReceipt receipt;
    Q4T_CHECK(remote.Invoke(session, receipt).ok());
    session.checkpoint = [=](const Json&) {
      auto result = Error(503, "fixture_storage_failed", "No commit receipt");
      result.commit_unknown = unknown;
      return result;
    };
    const auto result = remote.Invoke(session, receipt, "tools/call");
    Q4T_CHECK(!result.ok() && result.commit_unknown == unknown &&
              !receipt.call_permitted && remote.dispatched == 0);
    session.checkpoint = {};
    Q4T_CHECK(remote.Invoke(session, receipt, "tools/call").ok());
    Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1);
  }
  return true;
}

Q4T_TEST(mcp_session_worker_loss_restores_modern_without_reinitializing) {
  StatefulMcp remote;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(remote.Invoke(session, receipt).ok());
  Q4T_CHECK(remote.Invoke(session, receipt, "tools/call").ok());
  const auto identity = session.state.GetString("id");
  Q4T_CHECK(BreakMcpOwner());
  session.allow_new = false;
  Q4T_CHECK(remote.Invoke(session, receipt, "resources/read").ok());
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1 && session.state.GetString("id") == identity);
  return true;
}

Q4T_TEST(mcp_session_404_never_replays_and_next_independent_call_renews) {
  StatefulMcp remote;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(remote.Invoke(session, receipt).ok());
  const auto original = session.state.GetString("id");
  remote.expire_call = true;
  const auto failed = remote.Invoke(session, receipt, "tools/call");
  Q4T_CHECK(failed.code == "mcp_http_error" && receipt.call_permitted &&
            receipt.outcome_unknown && remote.dispatched == 1 &&
            remote.initialized == 1 && session.state.GetBool("lost"));
  auto resource = session;
  resource.allow_new = false;
  Q4T_CHECK(remote.Invoke(resource, receipt, "resources/read").code ==
            "mcp_session_lost");
  Q4T_CHECK(!receipt.call_permitted && remote.read == 0);
  Q4T_CHECK(BreakMcpOwner());
  Q4T_CHECK(remote.Invoke(session, receipt, "tools/call").ok());
  Q4T_CHECK(remote.initialized == 2 && remote.dispatched == 2 &&
            session.state.GetString("id") != original);
  return true;
}

Q4T_TEST(mcp_session_expiry_is_bounded_and_resources_do_not_migrate) {
  for (const bool absolute : {false, true}) {
    StatefulMcp remote;
    McpSession session;
    if (absolute)
      session.lifetime_ms = 120;
    else
      session.idle_ms = 120;
    McpTransportReceipt receipt;
    Q4T_CHECK(remote.Invoke(session, receipt).ok());
    const auto original = session.state.GetString("id");
    std::this_thread::sleep_for(160ms);
    auto resource = session;
    resource.allow_new = false;
    Q4T_CHECK(remote.Invoke(resource, receipt, "resources/read").code ==
              "mcp_session_lost");
    Q4T_CHECK(remote.initialized == 1 && !receipt.call_permitted);
    Q4T_CHECK(remote.Invoke(session, receipt, "tools/call").ok());
    Q4T_CHECK(remote.initialized == 2 && remote.dispatched == 1 &&
              session.state.GetString("id") != original);
  }
  return true;
}

Q4T_TEST(mcp_session_modern_cache_eviction_keeps_original_remote_session) {
  StatefulMcp remote;
  std::vector<McpSession> sessions(7);
  McpTransportReceipt receipt;
  for (auto& session : sessions)
    Q4T_CHECK(remote.Invoke(session, receipt).ok());
  Q4T_CHECK(remote.initialized == 7 && !remote.closed);
  Q4T_CHECK(remote.Invoke(sessions.front(), receipt, "tools/call").ok());
  Q4T_CHECK(remote.initialized == 7 && remote.dispatched == 1);
  return true;
}

Q4T_TEST(mcp_session_busy_exchange_does_not_queue_or_block_other_domains) {
  StatefulMcp remote;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(remote.Invoke(session, receipt).ok());
  std::atomic<bool> entered{false}, release{false};
  ObjectReply first;
  std::jthread owner([&] {
    McpTransportReceipt result;
    first = remote.Invoke(session, result, "tools/call", Access(), [&] {
      entered = true;
      while (!release.load()) std::this_thread::sleep_for(1ms);
      return ObjectReply{};
    });
  });
  ScopeExit finish([&] {
    release = true;
    owner.join();
  });
  const auto until = Clock::now() + 1s;
  while (!entered && Clock::now() < until) std::this_thread::sleep_for(1ms);
  Q4T_CHECK(entered);
  McpSession another;
  const auto start = Clock::now();
  Q4T_CHECK(remote.Invoke(another, receipt).code == "mcp_worker_capacity");
  Q4T_CHECK(Clock::now() - start < 100ms && !CloseMcpSessions());
  GrammarControl control;
  control.domain = IsolatedWorkerDomain::kGrammar;
  std::unique_ptr<detail::GrammarProcess> worker;
  Q4T_CHECK(detail::GrammarProcess::Start("--cache-fixture-descriptors",
                                          control, &worker));
  char value = 0;
  Q4T_CHECK(worker->Transfer(&value, 1, false, Clock::now() + 1s) &&
            value == 'x');
  worker.reset();
  release = true;
  owner.join();
  finish.Release();
  Q4T_CHECK(first.ok() && remote.dispatched == 1);
  return true;
}

namespace {
struct LegacyStatefulMcp {
  std::mutex mutex;
  std::map<std::string, int> streams;
  std::set<std::string> called;
  std::atomic<int> initialized{0}, dispatched{0}, read{0};
  Server server;
  LegacyStatefulMcp() {
    server.handler = [this](const Request& r, int fd, std::stop_token stop) {
      if (r.target == "/mcp" && r.verb == "POST") {
        Send(fd, Http(405));
        return true;
      }
      if (r.verb == "GET") {
        std::string target;
        {
          std::lock_guard lock(mutex);
          target = "/messages?session=" + std::to_string(++initialized);
          streams[target] = fd;
        }
        Send(fd,
             "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
             "Transfer-Encoding: chunked\r\n\r\n");
        Chunk(fd, "event: endpoint\ndata: " + target + "\n\n");
        Stall(stop);
        return true;
      }
      std::lock_guard lock(mutex);
      if (!streams.contains(r.target)) {
        Send(fd, Http(404));
        return true;
      }
      Send(fd, Http(202));
      if (!r.rpc.Find("id")) return true;
      Json result;
      const auto method = r.rpc.GetString("method");
      if (method == "initialize") {
        result = Object(
            {{"protocolVersion", String("2024-11-05")},
             {"capabilities",
              Object({{"tools", Object()}, {"resources", Object()}})},
             {"serverInfo",
              Object({{"name", String("legacy")}, {"version", String("1")}})}});
      } else if (method == "tools/list") {
        result = Object({{"tools", Array({Tool()})}});
      } else if (method == "tools/call") {
        ++dispatched;
        called.insert(r.target);
        result =
            Object({{"content",
                     Array({Object({{"type", String("resource_link")},
                                    {"uri", String("resource://legacy-owned")},
                                    {"name", String("result")}})})}});
      } else if (method == "resources/read" && called.contains(r.target)) {
        ++read;
        result = Object(
            {{"contents",
              Array({Object({{"uri", String("resource://legacy-owned")},
                             {"text", String("original legacy session")}})})}});
      }
      Chunk(streams.at(r.target),
            "event: message\ndata: " + OutputJson(Rpc(r, result)) + "\n\n");
      return true;
    };
  }
  ~LegacyStatefulMcp() { CloseMcpSessions(); }
  ObjectReply Invoke(McpSession& session, McpTransportReceipt& receipt,
                     std::string_view method = "tools/list",
                     BinaryAccess access = Access()) {
    return InvokeMcp(
        access, server.Endpoint(), server.Options(), method,
        method == "tools/list" ? Object()
        : method == "resources/read"
            ? Object({{"uri", String("resource://legacy-owned")}})
            : Call(),
        [] { return ObjectReply{}; }, &receipt, {}, &session);
  }
};
}  // namespace

Q4T_TEST(mcp_session_legacy_reuses_stream_with_each_operation_deadline) {
  LegacyStatefulMcp remote;
  McpSession session;
  McpTransportReceipt receipt;
  auto first = Access();
  first.deadline = Clock::now() + 1s;
  Q4T_CHECK(remote.Invoke(session, receipt, "tools/list", first).ok());
  std::this_thread::sleep_until(first.deadline + 20ms);
  Q4T_CHECK(remote.Invoke(session, receipt, "tools/call").ok());
  session.allow_new = false;
  Q4T_CHECK(remote.Invoke(session, receipt, "resources/read").ok());
  Q4T_CHECK(receipt.legacy_sse && remote.initialized == 1 &&
            remote.dispatched == 1 && remote.read == 1);
  return true;
}

Q4T_TEST(mcp_session_legacy_loss_rejects_old_resources_but_allows_new_call) {
  LegacyStatefulMcp remote;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(remote.Invoke(session, receipt).ok());
  Q4T_CHECK(remote.Invoke(session, receipt, "tools/call").ok());
  const auto original = session.state.GetString("id");
  Q4T_CHECK(BreakMcpOwner());
  auto resource = session;
  resource.allow_new = false;
  Q4T_CHECK(remote.Invoke(resource, receipt, "resources/read").code ==
            "mcp_session_lost");
  Q4T_CHECK(!receipt.call_permitted && !remote.read && remote.initialized == 1);
  Q4T_CHECK(remote.Invoke(session, receipt, "tools/call").ok());
  Q4T_CHECK(remote.initialized == 2 && remote.dispatched == 2 &&
            session.state.GetString("id") != original);
  return true;
}

Q4T_TEST(mcp_session_legacy_capacity_is_per_scope_and_frees_on_idle) {
  LegacyStatefulMcp remote;
  McpTransportReceipt receipt;
  std::vector<McpSession> sessions(5);
  for (size_t i = 0; i < 4; ++i) {
    sessions[i].idle_ms = 500;
    Q4T_CHECK(remote.Invoke(sessions[i], receipt).ok());
  }
  Q4T_CHECK(remote.Invoke(sessions.back(), receipt).code ==
            "mcp_session_capacity");
  Q4T_CHECK(remote.initialized == 4 && !receipt.call_permitted);
  McpSession other;
  auto access = Access();
  access.scope = "client:separate-scope";
  Q4T_CHECK(remote.Invoke(other, receipt, "tools/list", access).ok());
  std::this_thread::sleep_for(550ms);
  Q4T_CHECK(remote.Invoke(sessions.back(), receipt).ok());
  Q4T_CHECK(remote.initialized == 6);
  return true;
}

Q4T_TEST(
    responses_mcp_session_actual_resource_and_frozen_count_use_one_session) {
  StatefulMcp remote;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(remote.server);
  const auto request = ExecutionRequest(remote.server);
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
  Json frozen;
  Q4T_CHECK(ExecutionOk(run.FreezeInputs(&frozen)));
  Q4T_CHECK(OutputJson(frozen).find("private-fixture-session-") ==
            std::string::npos);
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
  Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)));
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1);
  Q4T_CHECK(run.ResultJson().find("private-fixture-session-") ==
            std::string::npos);
  const auto request_count = remote.server.Requests().size();
  Q4T_CHECK(CloseMcpSessions(false));
  Q4T_CHECK(remote.closed == 0);
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan counted;
  const auto count = Object({{"previous_response_id", String(run.Id())},
                             {"input", String("Continue")}});
  auto count_config = config;
  count_config.count_input_only = true;
  Q4T_CHECK(ExecutionOk(PrepareResponsesTokenCount(
      count, count_config, objects, ExecutionAccess(), &envelope, &counted)));
  Q4T_CHECK(remote.server.Requests().size() == request_count);
  Q4T_CHECK(OutputJson(counted.input.messages).find("same session resource") !=
            std::string::npos);
  Q4T_CHECK(
      OutputJson(counted.input.messages).find("private-fixture-session-") ==
      std::string::npos);
  return true;
}

Q4T_TEST(responses_mcp_session_approval_survives_store_and_owner_restart) {
  StatefulMcp remote;
  ExecutionFixture fixture;
  const auto config = ExecutionConfig(remote.server);
  const auto request = ExecutionRequest(remote.server, "always");
  Json approval;
  std::string response;
  {
    ObjectStore objects(fixture.options);
    ResponsesExecution run(objects);
    Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
    std::string wire;
    bool waiting = false;
    Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
    Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && waiting);
    approval = run.Output().Snapshot().GetArray("output")->array.back();
    Q4T_CHECK(approval.GetString("type") == "mcp_approval_request");
    Q4T_CHECK(ExecutionOk(run.Finish(false, 1, {}, 43, &wire)));
    response = run.Id();
  }
  Q4T_CHECK(CloseMcpSessions(false) && remote.closed == 0);
  ObjectStore objects(fixture.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(
      run.Prepare(ApprovalResponse(request, approval, true, response), config,
                  ExecutionAccess(), 44)));
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)));
  Q4T_CHECK(ExecutionOk(run.RunMcpApprovals({})));
  Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)));
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1);
  ResponsesExecution retry(objects);
  Q4T_CHECK(ExecutionOk(
      retry.Prepare(ApprovalResponse(request, approval, true, run.Id()), config,
                    ExecutionAccess(), 45)));
  Q4T_CHECK(ExecutionOk(retry.Begin(&wire)) &&
            ExecutionOk(retry.RunMcpApprovals({})));
  Q4T_CHECK(remote.dispatched == 1 && remote.read == 1);
  Q4T_CHECK(FinishExecution(&retry));
  return true;
}

Q4T_TEST(
    responses_mcp_session_frozen_preparation_resumes_with_fresh_credentials) {
  StatefulMcp remote;
  ExecutionFixture fixture;
  const auto config = ExecutionConfig(remote.server);
  auto request = ExecutionRequest(remote.server);
  Json frozen;
  {
    ObjectStore objects(fixture.options);
    ResponsesExecution run(objects);
    Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
    Q4T_CHECK(ExecutionOk(run.FreezeInputs(&frozen)));
    run.InputsPublished(false);
  }
  Q4T_CHECK(CloseMcpSessions(false));
  ObjectStore objects(fixture.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(run.RestoreInputs(frozen, config, ExecutionAccess(), 43,
                                          nullptr, &request)));
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
  Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)));
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1);
  return true;
}

Q4T_TEST(responses_mcp_session_rotated_credentials_use_new_remote_authority) {
  StatefulMcp remote;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(remote.server);
  auto request = ExecutionRequest(remote.server, "always");
  ResponsesExecution first(objects);
  Q4T_CHECK(ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)));
  std::string wire;
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(first.Begin(&wire)) && ModelCall(&first, false, &wire));
  Q4T_CHECK(ExecutionOk(first.RunMcp(1, &waiting, {})) && waiting);
  const auto approval =
      first.Output().Snapshot().GetArray("output")->array.back();
  Q4T_CHECK(ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  auto next = ApprovalResponse(request, approval, true, first.Id());
  Set(&Field(next, "tools").array.front(), "authorization",
      String("rotated-token"));
  ResponsesExecution resumed(objects);
  Q4T_CHECK(ExecutionOk(resumed.Prepare(next, config, ExecutionAccess(), 44)));
  Q4T_CHECK(ExecutionOk(resumed.Begin(&wire)) &&
            ExecutionOk(resumed.RunMcpApprovals({})));
  Q4T_CHECK(remote.initialized == 2 && remote.dispatched == 1);
  for (const auto& r : remote.server.Requests()) {
    if (r.rpc.GetString("method") != "tools/call") continue;
    Q4T_CHECK(r.headers.at("authorization") == "Bearer rotated-token");
    Q4T_CHECK(r.headers.at("mcp-session-id") == "private-fixture-session-2");
  }
  Q4T_CHECK(ExecutionOk(resumed.PrepareToolMedia(config)));
  Q4T_CHECK(ContinueExecution(&resumed, config) && FinishExecution(&resumed));
  return true;
}

Q4T_TEST(responses_mcp_session_legacy_resource_loss_keeps_actual_tool_result) {
  LegacyStatefulMcp remote;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(remote.server);
  const auto request = ExecutionRequest(remote.server);
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
  Q4T_CHECK(CloseMcpSessions(false));
  Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)));
  ResponsesGenerationPlan continued;
  Q4T_CHECK(ExecutionOk(run.ContinueFileSearch(8, config, {}, &continued)));
  Q4T_CHECK(OutputJson(continued.input.messages).find("mcp_session_lost") !=
            std::string::npos);
  const auto& item = run.Output().Snapshot().GetArray("output")->array.back();
  Q4T_CHECK(item.GetString("status") == "completed" &&
            item.GetString("output").find("resource://legacy-owned") !=
                std::string::npos);
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 0);
  Q4T_CHECK(FinishExecution(&run));
  return true;
}

Q4T_TEST(responses_mcp_session_resource_404_is_frozen_before_owner_restart) {
  StatefulMcp remote;
  ExecutionFixture fixture;
  const auto config = ExecutionConfig(remote.server);
  const auto request = ExecutionRequest(remote.server);
  std::string previous;
  {
    ObjectStore objects(fixture.options);
    ResponsesExecution run(objects);
    Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
    std::string wire;
    bool waiting = true;
    Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
    Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
    remote.expire_read = true;
    Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)));
    Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
    const auto& call = run.Output().Snapshot().GetArray("output")->array[1];
    Q4T_CHECK(call.GetString("type") == "mcp_call" &&
              call.GetString("status") == "completed");
    ResponsesMcpState state(objects, config);
    const auto owned = state.Inspect(call.GetString("id"), ExecutionAccess());
    Q4T_CHECK(ExecutionOk(owned) &&
              owned.value.Find("session")->GetBool("lost"));
    ResponsesMcpDefinition definition;
    ChatPlanError error;
    Q4T_CHECK(PlanResponsesMcpDefinition(
        request.GetArray("tools")->array.front(), &definition, &error));
    const auto catalog = state.Session(owned.value.GetString("catalog_id"),
                                       definition, ExecutionAccess());
    Q4T_CHECK(ExecutionOk(catalog) &&
              catalog.value.Find("session")->GetBool("lost"));
    previous = run.Id();
  }
  Q4T_CHECK(CloseMcpSessions(false));
  ObjectStore objects(fixture.options);
  auto next = request;
  Set(&next, "previous_response_id", String(previous));
  Set(&next, "input", String("Make a new independent query"));
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(run.Prepare(next, config, ExecutionAccess(), 44)));
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
  Q4T_CHECK(remote.initialized == 2 && remote.dispatched == 2 &&
            remote.read == 1);
  Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)));
  Q4T_CHECK(remote.read == 2);
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
  return true;
}

Q4T_TEST(mcp_session_absolute_lifetime_bounds_slow_publication_and_permission) {
  for (const bool slow_permission : {false, true}) {
    StatefulMcp remote;
    McpSession session;
    session.lifetime_ms = 120;
    McpTransportReceipt receipt;
    const auto delay = [&](const Json& state) {
      const auto expires = std::chrono::system_clock::time_point(
          std::chrono::milliseconds(state.GetInt("expires_ms")));
      std::this_thread::sleep_until(expires + 20ms);
    };
    if (!slow_permission) {
      session.checkpoint = [&](const Json& state) {
        delay(state);
        return ObjectReply{};
      };
    }
    const auto result =
        remote.Invoke(session, receipt, "tools/call", Access(), [&] {
          if (slow_permission) delay(session.state);
          return ObjectReply{};
        });
    Q4T_CHECK(!result.ok() && remote.initialized == 1 &&
              remote.dispatched == 0);
    Q4T_CHECK(receipt.call_permitted == slow_permission);
  }
  return true;
}

namespace {
Json MappedRequest(const Server& server, const std::string& kind,
                   std::string approval = "never") {
  auto request = ExecutionRequest(server, std::move(approval));
  auto& tool = Field(request, "tools").array[0];
  std::erase_if(tool.object,
                [](const auto& field) { return field.first == "server_url"; });
  Set(&tool, kind,
      String(kind == "connector_id" ? "connector_googledrive"
                                    : "tunnel_project"));
  return request;
}
ChatPlanConfig MappedConfig(const Server& server, const std::string& kind,
                            std::string generation = "one") {
  auto config = ExecutionConfig(server);
  // A private MCP route must not broaden ordinary file URL acquisition.
  config.file_inputs = std::make_shared<FileExpansionOptions>();
  auto policy = std::make_shared<McpDeploymentPolicy>();
  const auto raw = Object(
      {{"version", Number(1)},
       {"fetch", Object({{"public_networks", Boolean(false)},
                         {"allow_http", Boolean(true)},
                         {"allowed_networks", Array({String("127.0.0.1/32")})},
                         {"ports", Array({Number(server.port)})}})},
       {"targets",
        Array({Object(
            {{"kind", String(kind)},
             {"id", String(kind == "connector_id" ? "connector_googledrive"
                                                  : "tunnel_project")},
             {"url", String(server.Endpoint().url)},
             {"generation", String(std::move(generation))},
             {"scopes", Array({String(ExecutionAccess().scope)})}})})}});
  if (!ParseMcpDeploymentPolicy(OutputJson(raw), policy.get()))
    throw std::runtime_error("MCP mapped policy fixture");
  config.mcp_policy = std::move(policy);
  return config;
}
}  // namespace

Q4T_TEST(
    responses_mcp_mapped_http_executes_resource_and_counts_frozen_offline) {
  for (const std::string kind : {"connector_id", "tunnel_id"}) {
    StatefulMcp remote;
    ExecutionFixture fixture;
    ObjectStore objects(fixture.options);
    auto config = MappedConfig(remote.server, kind);
    const auto request = MappedRequest(remote.server, kind);
    ResponsesExecution run(objects);
    Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
    Json frozen;
    Q4T_CHECK(ExecutionOk(run.FreezeInputs(&frozen)));
    Q4T_CHECK(OutputJson(frozen).find(remote.server.Endpoint().url) ==
              std::string::npos);
    std::string wire;
    bool waiting = true;
    Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
    Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
    Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)));
    Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
    Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
              remote.read == 1);
    Q4T_CHECK(run.ResultJson().find(remote.server.Endpoint().url) ==
              std::string::npos);
    const auto requests = remote.server.Requests().size();
    config.mcp_policy.reset();
    config.count_input_only = true;
    ResponsesEnvelope envelope;
    ResponsesGenerationPlan counted;
    const auto count = Object({{"previous_response_id", String(run.Id())},
                               {"input", String("Continue")}});
    Q4T_CHECK(ExecutionOk(PrepareResponsesTokenCount(
        count, config, objects, ExecutionAccess(), &envelope, &counted)));
    Q4T_CHECK(remote.server.Requests().size() == requests);
    Q4T_CHECK(
        OutputJson(counted.input.messages).find("same session resource") !=
        std::string::npos);
  }
  return true;
}

Q4T_TEST(
    responses_mcp_mapped_approval_resumes_after_restart_without_relisting) {
  StatefulMcp remote;
  ExecutionFixture fixture;
  const auto config = MappedConfig(remote.server, "connector_id");
  const auto request = MappedRequest(remote.server, "connector_id", "always");
  Json approval;
  std::string response;
  {
    ObjectStore objects(fixture.options);
    ResponsesExecution run(objects);
    Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
    std::string wire;
    bool waiting = false;
    Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
    Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && waiting);
    approval = run.Output().Snapshot().GetArray("output")->array.back();
    Q4T_CHECK(ExecutionOk(run.Finish(false, 1, {}, 43, &wire)));
    response = run.Id();
  }
  Q4T_CHECK(CloseMcpSessions(false));
  ObjectStore objects(fixture.options);
  ResponsesExecution resumed(objects);
  Q4T_CHECK(ExecutionOk(
      resumed.Prepare(ApprovalResponse(request, approval, true, response),
                      config, ExecutionAccess(), 44)));
  std::string wire;
  Q4T_CHECK(ExecutionOk(resumed.Begin(&wire)));
  Q4T_CHECK(ExecutionOk(resumed.RunMcpApprovals({})));
  Q4T_CHECK(ExecutionOk(resumed.PrepareToolMedia(config)));
  Q4T_CHECK(ContinueExecution(&resumed, config) && FinishExecution(&resumed));
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1);
  return true;
}

Q4T_TEST(responses_mcp_mapped_remap_rejects_frozen_dispatch_and_old_approval) {
  StatefulMcp original, replacement;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = MappedConfig(original.server, "tunnel_id");
  const auto changed = MappedConfig(replacement.server, "tunnel_id", "two");
  const auto request = MappedRequest(original.server, "tunnel_id", "always");
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
  Json frozen;
  Q4T_CHECK(ExecutionOk(run.FreezeInputs(&frozen)));
  {
    ResponsesExecution queued(objects);
    const auto result = queued.RestoreInputs(frozen, changed, ExecutionAccess(),
                                             43, nullptr, &request);
    Q4T_CHECK(result.code == "mcp_target_changed");
  }
  Q4T_CHECK(replacement.initialized == 0);
  std::string wire;
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && waiting);
  const auto approval =
      run.Output().Snapshot().GetArray("output")->array.back();
  Q4T_CHECK(ExecutionOk(run.Finish(false, 1, {}, 43, &wire)));
  ResponsesExecution resumed(objects);
  Q4T_CHECK(ExecutionOk(
      resumed.Prepare(ApprovalResponse(request, approval, true, run.Id()),
                      changed, ExecutionAccess(), 44)));
  Q4T_CHECK(ExecutionOk(resumed.Begin(&wire)));
  Q4T_CHECK(resumed.RunMcpApprovals({}).code == "mcp_target_changed");
  Q4T_CHECK(original.dispatched == 0 && replacement.dispatched == 0);
  ResponsesMcpState state(objects, config);
  const auto owned = state.Inspect(approval.GetString("id"), ExecutionAccess());
  Q4T_CHECK(ExecutionOk(owned) && owned.value.GetString("state") == "approval");
  return true;
}

Q4T_TEST(responses_mcp_mapped_missing_or_revoked_route_never_contacts_backend) {
  StatefulMcp remote;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  auto config = MappedConfig(remote.server, "connector_id");
  const auto request = MappedRequest(remote.server, "connector_id");
  auto access = ExecutionAccess();
  access.scope = "client:other";
  ResponsesExecution revoked(objects);
  Q4T_CHECK(revoked.Prepare(request, config, access, 42).code ==
            "mcp_target_denied");
  config.mcp_policy.reset();
  ResponsesExecution missing(objects);
  Q4T_CHECK(missing.Prepare(request, config, ExecutionAccess(), 42).code ==
            "mcp_target_mapping_required");
  Q4T_CHECK(remote.server.Requests().empty());
  return true;
}

Q4T_TEST(responses_mcp_mapped_program_uses_original_route_for_native_dispatch) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = MappedConfig(server, "connector_id");
  auto request = MappedRequest(server, "connector_id");
  Set(&Field(request, "tools").array[0], "allowed_callers",
      Array({String("programmatic")}));
  Field(request, "tools")
      .array.push_back(Object({{"type", String("programmatic_tool_calling")}}));
  ResponsesExecution run(objects);
  Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)));
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) &&
            ProgramCode(&run,
                        "const r=await tools['mcp:project'].read_project({});"
                        "text(r.content[0].text);",
                        &wire));
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.RunPrograms(1, &waiting, {})) && !waiting);
  Q4T_CHECK(server.calls == 1 &&
            OutputItem(run, "program_output").GetString("result") ==
                "document result");
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
  return true;
}

namespace {
// CPU wire peer. The worker and broker under test are production code; this
// peer intentionally does not stand in for the self-owned HTTP/SSE agent.
class TunnelPeer {
 public:
  std::shared_ptr<McpTunnelBroker> broker = std::make_shared<McpTunnelBroker>();
  std::atomic<int> initialized{0}, called{0}, read{0}, terminated{0};
  std::atomic<bool> drop_call{false}, rpc_failure{false}, progress{false};
  std::atomic<bool> transport_failure{false}, invalid_final{false};
  TunnelPeer() {
    CloseMcpSessions();
    McpTunnelLimits limits;
    limits.agent_lease_ms = 20;
    if (!broker->Configure({{"tunnel_project",
                             "one",
                             {Access().scope, ExecutionAccess().scope},
                             {"client:agent"},
                             {"main"}}},
                           limits))
      throw std::runtime_error("native MCP tunnel fixture");
    Start("peer-one");
  }
  ~TunnelPeer() {
    CloseMcpSessions();
    thread_.request_stop();
    thread_.join();
    broker->Stop();
  }
  void Start(std::string instance) {
    if (thread_.joinable()) {
      thread_.request_stop();
      thread_.join();
    }
    thread_ = std::jthread([this, instance](std::stop_token stop) {
      while (!stop.stop_requested()) {
        BinaryAccess agent{"client:agent", Clock::now() + 1s,
                           [stop] { return stop.stop_requested(); }};
        auto polled =
            broker->Poll(agent, "tunnel_project", instance, {}, 25, 10);
        const auto* commands = polled.value.GetArray("commands");
        if (!polled.ok() || !commands) {
          std::this_thread::sleep_for(1ms);
          continue;
        }
        for (const auto& command : commands->array) {
          {
            std::lock_guard lock(mutex_);
            commands_.push_back(command);
          }
          const auto* rpc = command.Find("jsonrpc");
          auto response = Object({{"request_id", *command.Find("request_id")},
                                  {"resp_code", Number(200)},
                                  {"resp_headers", Object()}});
          auto result = Object();
          if (!rpc) {
            ++terminated;
            Set(&response, "resp_type", String("session_termination_response"));
            Set(&response, "resp_code", Number(204));
          } else if (!rpc->Has("id")) {
            Set(&response, "resp_type", String("notify_ack"));
            Set(&response, "resp_code", Number(202));
          } else {
            const auto method = rpc->GetString("method");
            if (method == "initialize") {
              const auto remote =
                  "private-native-" + std::to_string(++initialized);
              Set(&response, "resp_headers",
                  Object({{"Mcp-Session-Id", Array({String(remote)})}}));
              result = Object(
                  {{"protocolVersion", String("2025-06-18")},
                   {"capabilities",
                    Object({{"tools", Object()}, {"resources", Object()}})},
                   {"serverInfo", Object({{"name", String("cpu-peer")},
                                          {"version", String("1")}})}});
            } else if (method == "tools/list") {
              result = Object({{"tools", Array({Tool()})}});
            } else if (method == "tools/call") {
              ++called;
              if (drop_call) continue;
              result = Object(
                  {{"content",
                    Array({Object({{"type", String("resource_link")},
                                   {"uri", String("resource://native")},
                                   {"name", String("native document")}})})}});
            } else if (method == "resources/read") {
              ++read;
              result = Object(
                  {{"contents",
                    Array({Object(
                        {{"uri", String("resource://native")},
                         {"text", String("native tunnel document")}})})}});
            }
            auto message = Object({{"jsonrpc", String("2.0")},
                                   {"id", *rpc->Find("id")},
                                   {"result", result}});
            if (method == "tools/call" && rpc_failure) {
              std::erase_if(message.object, [](const auto& field) {
                return field.first == "result";
              });
              Set(&message, "error",
                  Object({{"code", Number(-32042)},
                          {"message", String("Authorization expired")},
                          {"data", Object({{"retryable", Boolean(false)},
                                           {"outcome_unknown", Boolean(true)},
                                           {"detail", String("原始错误")}})}}));
              Set(&response, "resp_code", Number(401));
            }
            Set(&response, "resp_json", message);
            if (method == "tools/call" && transport_failure)
              Set(&response, "q4t_outcome_unknown", Boolean(true));
            if (method == "tools/call" && invalid_final)
              Set(&Field(response, "resp_json"), "id", String("invalid-id"));
            if (progress) {
              auto notify = response;
              Set(&notify, "resp_type", String("jsonrpc_notify"));
              Set(&notify, "resp_json",
                  Object({{"jsonrpc", String("2.0")},
                          {"method", String("notifications/progress")},
                          {"params", Object({{"progressToken", String("p")},
                                             {"progress", Number(1)}})}}));
              (void)broker->Respond(agent, "tunnel_project", instance,
                                    command.GetString("shard_token"), notify);
            }
          }
          (void)broker->Respond(agent, "tunnel_project", instance,
                                command.GetString("shard_token"), response);
        }
      }
    });
  }
  McpEndpoint Endpoint() const {
    McpEndpoint endpoint;
    endpoint.tunnel_id = "tunnel_project";
    endpoint.tunnel_generation = "one";
    endpoint.authorization = "native-secret";
    return endpoint;
  }
  ObjectReply Invoke(
      McpSession* session, McpTransportReceipt* receipt,
      std::string_view method = "tools/list",
      std::function<ObjectReply()> permit = [] { return ObjectReply{}; },
      BinaryAccess access = Access()) {
    return InvokeMcp(access, Endpoint(), FileFetchOptions{}, method,
                     method == "tools/list" ? Object()
                     : method == "resources/read"
                         ? Object({{"uri", String("resource://native")}})
                         : Call(),
                     permit, receipt, {}, session, broker);
  }
  std::vector<Json> Commands() {
    std::lock_guard lock(mutex_);
    return commands_;
  }
  bool WaitTerminated(int count) {
    const auto end = Clock::now() + 1s;
    while (terminated < count && Clock::now() < end)
      std::this_thread::sleep_for(1ms);
    return terminated == count;
  }
  ChatPlanConfig Config() {
    ChatPlanConfig config;
    config.model = "cpu-native-mcp";
    config.structured = true;
    auto policy = std::make_shared<McpDeploymentPolicy>();
    const auto raw = Object(
        {{"version", Number(1)},
         {"targets",
          Array({Object({{"kind", String("tunnel_id")},
                         {"id", String("tunnel_project")},
                         {"transport", String("native")},
                         {"generation", String("one")},
                         {"scopes", Array({String(ExecutionAccess().scope)})},
                         {"agents", Array({String("client:agent")})}})})}});
    if (!ParseMcpDeploymentPolicy(OutputJson(raw), policy.get()))
      throw std::runtime_error("native MCP execution policy");
    config.mcp_policy = policy;
    config.mcp_tunnels = broker;
    return config;
  }
  Json Request(std::string approval = "never") {
    return Object(
        {{"input", String("Read the project document")},
         {"reasoning", Object({{"effort", String("none")}})},
         {"tools", Array({Object({{"type", String("mcp")},
                                  {"server_label", String("project")},
                                  {"tunnel_id", String("tunnel_project")},
                                  {"authorization", String("native-secret")},
                                  {"require_approval",
                                   String(std::move(approval))}})})}});
  }

 private:
  std::mutex mutex_;
  std::vector<Json> commands_;
  std::jthread thread_;
};
}  // namespace

Q4T_TEST(mcp_native_tunnel_isolated_list_call_resource_and_private_session) {
  TunnelPeer peer;
  McpSession session;
  McpTransportReceipt receipt;
  int checkpoints = 0, permits = 0;
  session.checkpoint = [&](const Json& state) {
    ++checkpoints;
    return state.GetString("agent_instance") == "peer-one"
               ? ObjectReply{}
               : Error(500, "fixture_state", "No agent affinity");
  };
  Q4T_CHECK(ExecutionOk(peer.Invoke(&session, &receipt)));
  const auto id = session.state.GetString("id");
  Q4T_CHECK(ExecutionOk(peer.Invoke(&session, &receipt, "tools/call", [&] {
    ++permits;
    return ObjectReply{};
  })));
  session.allow_new = false;
  Q4T_CHECK(ExecutionOk(peer.Invoke(&session, &receipt, "resources/read")));
  Q4T_CHECK(checkpoints == 3 && permits == 1 && peer.initialized == 1 &&
            peer.called == 1 && peer.read == 1 &&
            session.state.GetString("id") == id);
  Q4T_CHECK(receipt.result.GetArray("contents")->array[0].GetString("text") ==
            "native tunnel document");
  Q4T_CHECK(OutputJson(receipt.result).find("private-native-") ==
            std::string::npos);
  for (const auto& command : peer.Commands())
    Q4T_CHECK(
        command.Find("headers")->GetArray("authorization")->array[0].str ==
        "Bearer native-secret");
  Q4T_CHECK(CloseMcpSessions() && peer.WaitTerminated(1));
  Q4T_CHECK(peer.broker->Health().GetInt("pending") == 0);
  return true;
}

Q4T_TEST(mcp_native_tunnel_permission_and_checkpoint_fail_before_dispatch) {
  TunnelPeer peer;
  McpSession session;
  McpTransportReceipt receipt;
  session.checkpoint = [](const Json&) {
    return Error(503, "fixture_storage", "Cannot persist session");
  };
  Q4T_CHECK(peer.Invoke(&session, &receipt, "tools/call").code ==
            "fixture_storage");
  Q4T_CHECK(!receipt.call_permitted && peer.called == 0 &&
            peer.WaitTerminated(1));
  session = {};
  Q4T_CHECK(
      peer.Invoke(&session, &receipt, "tools/call",
                  [] { return Error(409, "fixture_approval", "Not approved"); })
          .code == "fixture_approval");
  Q4T_CHECK(!receipt.call_permitted && peer.called == 0);
  return true;
}

Q4T_TEST(mcp_native_tunnel_binding_changes_rejected_before_queueing) {
  TunnelPeer peer;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(peer.Invoke(&session, &receipt).ok());
  const auto count = peer.Commands().size();
  for (int kind = 0; kind < 4; ++kind) {
    auto endpoint = peer.Endpoint();
    auto access = Access();
    if (kind == 0) endpoint.tunnel_channel = "changed";
    if (kind == 1) endpoint.tunnel_generation = "changed";
    if (kind == 2) endpoint.authorization = "changed";
    if (kind == 3) access.scope = "client:other";
    Q4T_CHECK(InvokeMcp(
                  access, endpoint, FileFetchOptions{}, "tools/call", Call(),
                  [] { return ObjectReply{}; }, &receipt, {}, &session,
                  peer.broker)
                  .code == "mcp_session_binding_changed");
  }
  Q4T_CHECK(peer.Commands().size() == count && peer.called == 0);
  return true;
}

Q4T_TEST(mcp_native_tunnel_owner_loss_restores_original_without_initialize) {
  TunnelPeer peer;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(peer.Invoke(&session, &receipt).ok());
  Q4T_CHECK(BreakMcpOwner());
  Q4T_CHECK(ExecutionOk(peer.Invoke(&session, &receipt, "tools/call")));
  Q4T_CHECK(peer.initialized == 1 && peer.called == 1 && peer.terminated == 0);
  Q4T_CHECK(CloseMcpSessions(false));
  session.allow_new = false;
  Q4T_CHECK(peer.Invoke(&session, &receipt, "resources/read").ok());
  Q4T_CHECK(peer.initialized == 1 && peer.read == 1 && peer.terminated == 0);
  return true;
}

Q4T_TEST(mcp_native_tunnel_new_agent_cannot_take_original_resource_session) {
  TunnelPeer peer;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(peer.Invoke(&session, &receipt, "tools/call").ok());
  peer.Start("peer-two");
  session.allow_new = false;
  const auto result = peer.Invoke(&session, &receipt, "resources/read");
  Q4T_CHECK(result.code == "mcp_tunnel_session_lost" && peer.read == 0 &&
            session.state.GetBool("lost"));
  session.allow_new = true;
  Q4T_CHECK(ExecutionOk(peer.Invoke(&session, &receipt, "tools/call")));
  Q4T_CHECK(peer.initialized == 2 && peer.called == 2 &&
            session.state.GetString("agent_instance") == "peer-two");
  return true;
}

Q4T_TEST(mcp_native_tunnel_cancelled_handoff_is_unknown_and_never_replayed) {
  TunnelPeer peer;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(peer.Invoke(&session, &receipt).ok());
  peer.drop_call = true;
  auto access = Access();
  access.deadline = Clock::now() + 200ms;
  access.cancelled = [&] { return peer.called.load() > 0; };
  const auto start = Clock::now();
  const auto result = peer.Invoke(
      &session, &receipt, "tools/call", [] { return ObjectReply{}; }, access);
  Q4T_CHECK(result.http_status == 499 && Clock::now() - start < 1s &&
            receipt.call_permitted && receipt.outcome_unknown &&
            peer.called == 1);
  Q4T_CHECK(peer.broker->Health().GetInt("pending") == 0);
  return true;
}

Q4T_TEST(mcp_native_tunnel_offline_broker_is_bounded_before_permission) {
  auto broker = std::make_shared<McpTunnelBroker>();
  Q4T_CHECK(broker->Configure({{"tunnel_offline",
                                "one",
                                {Access().scope},
                                {"client:agent"},
                                {"main"}}}));
  McpEndpoint endpoint;
  endpoint.tunnel_id = "tunnel_offline";
  endpoint.tunnel_generation = "one";
  auto access = Access();
  access.deadline = Clock::now() + 100ms;
  McpTransportReceipt receipt;
  int permits = 0;
  const auto start = Clock::now();
  const auto result = InvokeMcp(
      access, endpoint, FileFetchOptions{}, "tools/call", Call(),
      [&] {
        ++permits;
        return ObjectReply{};
      },
      &receipt, {}, nullptr, broker);
  Q4T_CHECK(result.http_status == 408 && Clock::now() - start < 1s &&
            permits == 0 && !receipt.call_permitted);
  const auto polled = broker->Poll({"client:agent", Clock::now() + 1s, {}},
                                   "tunnel_offline", "late", {}, 1, 0);
  Q4T_CHECK(polled.http_status == 204);
  return true;
}

Q4T_TEST(mcp_native_tunnel_progress_and_http_error_keep_exact_rpc_outcome) {
  TunnelPeer peer;
  peer.progress = peer.rpc_failure = true;
  McpTransportReceipt receipt;
  Q4T_CHECK(ExecutionOk(peer.Invoke(nullptr, &receipt, "tools/call")));
  Q4T_CHECK(receipt.call_permitted && !receipt.outcome_unknown &&
            receipt.error.GetInt("code") == -32042 &&
            Field(receipt.error, "data").GetString("detail") == "原始错误");
  Q4T_CHECK(peer.called == 1 && peer.WaitTerminated(1));
  return true;
}

Q4T_TEST(mcp_native_tunnel_session_capacity_and_idle_cleanup_are_separate) {
  TunnelPeer peer;
  std::vector<McpSession> sessions(5);
  McpTransportReceipt receipt;
  for (size_t i = 0; i < 4; ++i)
    Q4T_CHECK(peer.Invoke(&sessions[i], &receipt).ok());
  Q4T_CHECK(peer.Invoke(&sessions[4], &receipt).code == "mcp_session_capacity");
  Q4T_CHECK(peer.initialized == 4);
  // Direct MCP's independent remote remains usable while native leases fill.
  Server direct;
  Q4T_CHECK(Invoke(direct, &receipt).ok());
  Q4T_CHECK(CloseMcpSessions() && peer.WaitTerminated(4));
  McpSession short_lived;
  short_lived.idle_ms = 30;
  Q4T_CHECK(peer.Invoke(&short_lived, &receipt).ok());
  std::this_thread::sleep_for(40ms);
  PollMcpSessions();
  Q4T_CHECK(peer.WaitTerminated(5));
  return true;
}

Q4T_TEST(mcp_native_tunnel_bridge_cannot_change_headers_or_dispatch_authority) {
  TunnelPeer peer;
  mcp_detail::TunnelSessions leases;
  std::unique_ptr<mcp_detail::TunnelExchange> exchange;
  Q4T_CHECK(leases
                .Begin(Access(), peer.Endpoint(), peer.broker, "tools/call",
                       Call(), nullptr, &exchange)
                .ok());
  auto forged = Object({{"type", String("tunnel_http")},
                        {"headers", Object()},
                        {"rpc", Object({{"jsonrpc", String("2.0")},
                                        {"id", Number(1)},
                                        {"method", String("tools/call")},
                                        {"params", Call()}})}});
  Q4T_CHECK(exchange->Forward(forged, false).code == "mcp_tunnel_authority");
  Q4T_CHECK(exchange->Forward(forged, true).code == "mcp_tunnel_authority");
  Q4T_CHECK(peer.Commands().empty() && peer.called == 0);
  return true;
}

Q4T_TEST(mcp_http_non200_rpc_error_is_known_and_404_marks_session_lost) {
  StatefulMcp remote;
  const auto original = remote.server.handler;
  remote.server.handler = [&](const Request& request, int fd,
                              std::stop_token stop) {
    if (request.rpc.GetString("method") != "tools/call")
      return original(request, fd, stop);
    ++remote.dispatched;
    auto response =
        Object({{"jsonrpc", String("2.0")},
                {"id", *request.rpc.Find("id")},
                {"error",
                 Object({{"code", Number(-32001)},
                         {"message", String("Session expired")},
                         {"data", Object({{"retryable", Boolean(false)}})}})}});
    Send(fd, Http(404, OutputJson(response)));
    return true;
  };
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(remote.Invoke(session, receipt).ok());
  const auto result = remote.Invoke(session, receipt, "tools/call");
  Q4T_CHECK(result.ok() && result.value.GetInt("upstream_status") == 404 &&
            !receipt.outcome_unknown &&
            receipt.error.GetInt("code") == -32001 &&
            session.state.GetBool("lost") && remote.dispatched == 1);
  session.allow_new = false;
  Q4T_CHECK(remote.Invoke(session, receipt, "resources/read").code ==
            "mcp_session_lost");
  return true;
}

Q4T_TEST(responses_mcp_native_tunnel_resource_and_frozen_offline_count) {
  TunnelPeer peer;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  auto config = peer.Config();
  ResponsesExecution run(objects);
  Q4T_CHECK(
      ExecutionOk(run.Prepare(peer.Request(), config, ExecutionAccess(), 42)));
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting);
  Q4T_CHECK(ExecutionOk(run.PrepareToolMedia(config)));
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
  Q4T_CHECK(peer.initialized == 1 && peer.called == 1 && peer.read == 1);
  const auto text = run.ResultJson();
  Q4T_CHECK(text.find("private-native-") == std::string::npos &&
            text.find("native-secret") == std::string::npos &&
            text.find("peer-one") == std::string::npos);
  const auto count = peer.Commands().size();
  config.mcp_policy.reset();
  config.mcp_tunnels.reset();
  config.count_input_only = true;
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan counted;
  Q4T_CHECK(ExecutionOk(PrepareResponsesTokenCount(
      Object({{"previous_response_id", String(run.Id())},
              {"input", String("Continue")}}),
      config, objects, ExecutionAccess(), &envelope, &counted)));
  Q4T_CHECK(peer.Commands().size() == count &&
            OutputJson(counted.input.messages).find("native tunnel document") !=
                std::string::npos);
  return true;
}

Q4T_TEST(responses_mcp_native_tunnel_approval_survives_owner_restart) {
  TunnelPeer peer;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = peer.Config();
  const auto request = peer.Request("always");
  ResponsesExecution first(objects);
  Q4T_CHECK(ExecutionOk(first.Prepare(request, config, ExecutionAccess(), 42)));
  std::string wire;
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(first.Begin(&wire)) && ModelCall(&first, false, &wire));
  Q4T_CHECK(ExecutionOk(first.RunMcp(1, &waiting, {})) && waiting &&
            peer.called == 0);
  const auto approval = OutputItem(first, "mcp_approval_request");
  Q4T_CHECK(ExecutionOk(first.Finish(false, 1, {}, 43, &wire)));
  Q4T_CHECK(CloseMcpSessions(false));
  ResponsesExecution next(objects);
  Q4T_CHECK(ExecutionOk(next.Prepare(
                ApprovalResponse(request, approval, true, first.Id()), config,
                ExecutionAccess(), 44)) &&
            ExecutionOk(next.Begin(&wire)));
  Q4T_CHECK(ExecutionOk(next.RunMcpApprovals({})) && peer.called == 1);
  Q4T_CHECK(ExecutionOk(next.PrepareToolMedia(config)) &&
            ContinueExecution(&next, config) && FinishExecution(&next));
  Q4T_CHECK(peer.initialized == 1 && peer.read == 1);
  return true;
}

Q4T_TEST(responses_mcp_native_tunnel_program_retains_original_authority) {
  TunnelPeer peer;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = peer.Config();
  auto request = peer.Request();
  Set(&Field(request, "tools").array[0], "allowed_callers",
      Array({String("programmatic")}));
  Field(request, "tools")
      .array.push_back(Object({{"type", String("programmatic_tool_calling")}}));
  ResponsesExecution run(objects);
  std::string wire;
  bool waiting = true;
  Q4T_CHECK(ExecutionOk(run.Prepare(request, config, ExecutionAccess(), 42)) &&
            ExecutionOk(run.Begin(&wire)) &&
            ProgramCode(
                &run,
                "const r=await "
                "tools['mcp:project'].read_project({});text(r.content[0].uri);",
                &wire));
  Q4T_CHECK(ExecutionOk(run.RunPrograms(1, &waiting, {})) && !waiting &&
            peer.called == 1);
  Q4T_CHECK(OutputItem(run, "program_output").GetString("result") ==
            "resource://native");
  Q4T_CHECK(ContinueExecution(&run, config) && FinishExecution(&run));
  return true;
}

Q4T_TEST(
    mcp_native_tunnel_known_result_wins_cancellation_during_worker_return) {
  TunnelPeer peer;
  McpSession session;
  McpTransportReceipt receipt;
  Q4T_CHECK(peer.Invoke(&session, &receipt).ok());
  auto access = Access();
  // Wait consumes the matched terminal response. Cancellation becomes true
  // precisely after handoff has completed and before the worker IPC returns.
  access.cancelled = [&] {
    return peer.called.load() > 0 &&
           peer.broker->Health().GetInt("pending") == 0 &&
           peer.broker->Health().GetInt("inflight") == 0;
  };
  Q4T_CHECK(ExecutionOk(peer.Invoke(
      &session, &receipt, "tools/call", [] { return ObjectReply{}; }, access)));
  Q4T_CHECK(access.cancelled() && receipt.call_permitted &&
            !receipt.outcome_unknown && peer.called == 1 &&
            receipt.result.GetArray("content"));
  return true;
}

Q4T_TEST(mcp_http_error_rejects_fractional_rpc_identity_and_code) {
  for (const bool fractional_id : {true, false}) {
    Server server;
    std::atomic<int> calls{0};
    server.handler = [&](const Request& request, int fd, std::stop_token) {
      if (request.rpc.GetString("method") != "tools/call") return false;
      ++calls;
      const auto id = OutputJson(*request.rpc.Find("id"));
      const auto body =
          "{\"jsonrpc\":\"2.0\",\"id\":" + id + (fractional_id ? ".5" : "") +
          ",\"error\":{\"code\":-32001" + (fractional_id ? "" : ".5") +
          ",\"message\":\"Invalid wire\"}}";
      Send(fd, Http(401, body));
      return true;
    };
    McpTransportReceipt receipt;
    const auto reply = Invoke(server, &receipt, "tools/call");
    Q4T_CHECK(reply.code == "mcp_http_error" && receipt.call_permitted &&
              receipt.outcome_unknown && calls == 1);
  }
  return true;
}

namespace {
using mcp_detail::TunnelAgentBackend;
const Json& At(const Json& value, const char* key) {
  const auto* found = value.Find(key);
  if (!found) throw std::runtime_error("missing tunnel fixture field");
  return *found;
}
Json AgentCommand(std::string method = "initialize", Json headers = Object(),
                  Json id = String("original-rpc")) {
  return Object(
      {{"request_id", String("original-wire")},
       {"command_type", String("jsonrpc")},
       {"channel", String("main")},
       {"headers", std::move(headers)},
       {"jsonrpc",
        Object({{"jsonrpc", String("2.0")},
                {"id", std::move(id)},
                {"method", String(method)},
                {"params", method == "tools/call" ? Call() : Object()}})}});
}
Json AgentSessionHeaders(const ObjectReply& result,
                         std::string protocol = "2025-06-18") {
  return Object({{"mcp-session-id",
                  At(At(result.value, "resp_headers"), "mcp-session-id")},
                 {"mcp-protocol-version", Array({String(protocol)})}});
}
ObjectReply AgentExchange(TunnelAgentBackend& agent, const Json& command) {
  return agent.Exchange(command, Clock::now() + 3s);
}
bool AgentOk(const ObjectReply& reply, const std::string& member = "result") {
  const auto* rpc = reply.value.Find("resp_json");
  return reply.ok() && reply.value.GetInt("resp_code") == 200 && rpc &&
         rpc->Has(member);
}
McpTunnelAgentConfig AgentConfig(const Server& control, const Server& target) {
  McpTunnelAgentConfig config;
  config.api_base = "http://127.0.0.1:" + std::to_string(control.port) + "/v1";
  config.api_key = "control-secret";
  config.tunnel_id = "tunnel_private";
  config.target = target.Endpoint();
  config.control_fetch = control.Options();
  config.target_fetch = target.Options();
  config.control_fetch.max_redirects = 0;
  config.target_fetch.max_redirects = 0;
  return config;
}
// Production broker/HTTP handler and production isolated agent. Only the two
// local TCP servers are CPU fixtures; no wire peer substitutes for the agent.
struct AgentBroker {
  McpTunnelBroker broker;
  Server server;
  std::atomic<bool> valid{true};
  std::string instance;
  std::mutex mutex;
  AgentBroker() {
    if (!broker.Configure({{"tunnel_private",
                            "gen-1",
                            {"client:caller"},
                            {"client:agent"},
                            {"main"}}}))
      throw std::runtime_error("agent broker fixture");
    server.handler = [this](const Request& r, int fd, std::stop_token) {
      const auto get = [&](const char* key) {
        const auto i = r.headers.find(key);
        return i == r.headers.end() ? std::string{} : i->second;
      };
      if (get("authorization") != "Bearer control-secret" ||
          get("x-tunnel-client-wire-protocol-version") != "2026-08-25")
        valid = false;
      const auto current = get("x-tunnel-client-instance-id");
      {
        std::lock_guard lock(mutex);
        if (!instance.empty() && instance != current) valid = false;
        instance = current;
      }
      const auto reply = HandleMcpTunnelRequest(
          broker, {"client:agent", Clock::now() + 3s, {}}, r.verb, r.target,
          r.body, current, get("x-tunnel-shard-token"));
      Send(fd, Http(reply.http_status,
                    reply.http_status == 204 ? "" : OutputJson(reply.value)));
      return true;
    };
  }
  ObjectReply Exchange(const Json& command, bool terminate = false) {
    BinaryAccess caller{"client:caller", Clock::now() + 5s, {}};
    const auto queued = broker.Submit(
        caller, "tunnel_private", "gen-1", "main", At(command, "headers"),
        terminate ? Json() : At(command, "jsonrpc"), terminate);
    if (!queued.ok()) return queued;
    return broker.Wait(caller, queued.value.GetString("request_id"));
  }
};
}  // namespace

Q4T_TEST(mcp_agent_config_strict_network_and_redacted_description) {
  Json raw;
  Q4T_CHECK(Parse(R"({"version":1,"api_base":"https://control.example/v1/",
    "api_key":"control-private-token","tunnel_id":"tunnel_private",
    "control_fetch":{"allow_http":false},
    "target":{"url":"https://mcp.example/mcp","authorization":"target-token"}})",
                  &raw));
  McpTunnelAgentConfig config;
  Q4T_CHECK(ParseMcpTunnelAgentConfig(OutputJson(raw), &config));
  Q4T_CHECK(config.api_base == "https://control.example/v1" &&
            config.control_fetch.max_redirects == 0 &&
            config.target_fetch.max_redirects == 0);
  const auto description = OutputJson(McpTunnelAgentDescription(config));
  Q4T_CHECK(description.find("token") == description.npos &&
            description.find("example") == description.npos &&
            description.find(config.sha256) != description.npos);
  for (const auto* key : {"version", "api_key", "channel", "restart_limit",
                          "target", "control_fetch", "unknown"}) {
    auto bad = raw;
    Set(&bad, key, Array());
    Q4T_CHECK(!ParseMcpTunnelAgentConfig(OutputJson(bad), &config));
  }
  for (const auto* url : {"http://127.0.0.1/v1", "https://x/v1?q=secret",
                          "https://user:secret@host/v1"}) {
    auto bad = raw;
    Set(&bad, "api_base", String(url));
    Q4T_CHECK(!ParseMcpTunnelAgentConfig(OutputJson(bad), &config));
  }
  Q4T_CHECK(
      !ParseMcpTunnelAgentConfig(R"({"version":1,"version":1})", &config));
  return true;
}

namespace {
Json SmallAgentConfig() {
  return Object(
      {{"version", Number(1)},
       {"api_base", String("https://control.example/v1")},
       {"api_key", String("control-token")},
       {"tunnel_id", String("tunnel_private")},
       {"target", Object({{"url", String("https://mcp.example")},
                          {"authorization", String("target-token")}})}});
}
size_t AgentConfigStringBytes(const Json& value) {
  if (value.IsString()) return value.str.size();
  size_t bytes = 0;
  for (const auto& [key, entry] : value.object)
    bytes += key.size() + AgentConfigStringBytes(entry);
  for (const auto& entry : value.array) bytes += AgentConfigStringBytes(entry);
  return bytes;
}
}  // namespace

Q4T_TEST(mcp_agent_config_redirect_authority_is_not_silently_discarded) {
  McpTunnelAgentConfig config;
  Json fractional;
  Q4T_CHECK(Parse("0.5", &fractional));
  for (const auto* field : {"control_fetch", "target_fetch"}) {
    auto raw = SmallAgentConfig();
    for (const auto& value : {Number(1), Number(-1), fractional, Boolean(false),
                              String("0"), Json()}) {
      Set(&raw, field, Object({{"max_redirects", value}}));
      Q4T_CHECK(!ParseMcpTunnelAgentConfig(OutputJson(raw), &config));
    }
    for (const auto& policy :
         {Object(), Object({{"max_redirects", Number(0)}})}) {
      Set(&raw, field, policy);
      Q4T_CHECK(ParseMcpTunnelAgentConfig(OutputJson(raw), &config));
      Q4T_CHECK(config.control_fetch.max_redirects == 0 &&
                config.target_fetch.max_redirects == 0);
    }
  }
  return true;
}

Q4T_TEST(mcp_agent_config_rejects_input_that_cannot_fit_worker_string_budget) {
  auto raw = SmallAgentConfig();
  McpTunnelAgentConfig config;
  const auto original = OutputJson(raw);
  Q4T_CHECK(ParseMcpTunnelAgentConfig(original, &config));
  const auto original_sha = config.sha256;
  Set(&raw, "api_key", String(std::string(8192, 'a')));
  auto target = *raw.Find("target");
  const auto remaining = 16384 - AgentConfigStringBytes(raw);
  Set(&target, "authorization",
      String(std::string(target.GetString("authorization").size() + remaining,
                         'b')));
  Set(&raw, "target", std::move(target));
  Q4T_CHECK(AgentConfigStringBytes(raw) == 16384);
  q4t::io::JsonParseLimits limits;
  limits.max_string_bytes = 16384;
  Json parsed;
  const auto bytes = OutputJson(raw);
  Q4T_CHECK(q4t::io::ParseJson(bytes, &parsed, true, limits));
  Q4T_CHECK(bytes.size() < kMcpTunnelAgentConfigBytes);
  Q4T_CHECK(!ParseMcpTunnelAgentConfig(bytes, &config));
  Q4T_CHECK(config.sha256 == original_sha);
  return true;
}

Q4T_TEST(mcp_agent_config_exact_worker_budget_round_trips_original_authority) {
  McpTunnelAgentConfig config;
  Q4T_CHECK(ParseMcpTunnelAgentConfig(OutputJson(SmallAgentConfig()), &config));
  config.api_key = std::string(8192, 'a');
  Json raw;
  Q4T_CHECK(Parse(SerializeMcpTunnelAgentConfig(config), &raw));
  config.target.authorization +=
      std::string(16384 - AgentConfigStringBytes(raw), 'b');
  const auto bytes = SerializeMcpTunnelAgentConfig(config);
  Q4T_CHECK(Parse(bytes, &raw) && AgentConfigStringBytes(raw) == 16384);
  McpTunnelAgentConfig checked;
  Q4T_CHECK(ParseMcpTunnelAgentConfig(bytes, &checked));
  Q4T_CHECK(SerializeMcpTunnelAgentConfig(checked) == bytes &&
            checked.sha256 == Digest(bytes) &&
            checked.api_key == config.api_key &&
            checked.target.authorization == config.target.authorization);
  checked.target.authorization.push_back('b');
  Q4T_CHECK(!ParseMcpTunnelAgentConfig(SerializeMcpTunnelAgentConfig(checked),
                                       &config));
  return true;
}

Q4T_TEST(mcp_agent_timeout_grammar_anchored_and_local_bound) {
  const auto received = Clock::now() - 2s;
  for (const auto& [value, expected] :
       std::vector<std::pair<std::string, std::chrono::nanoseconds>>{
           {"0s", 0ns},
           {"9ns", 9ns},
           {"12us", 12us},
           {"250ms", 250ms},
           {"3s", 3s},
           {"1m", 30s},
           {"1h", 30s},
           {"1.5s", 30s},
           {"-1s", 30s},
           {"1m2s", 30s},
           {"", 30s},
           {"18446744073709551615h", 30s}}) {
    const auto command = Object({{"response_timeout", String(value)}});
    Q4T_CHECK(mcp_detail::TunnelCommandDeadline(command, received, 30s) ==
              received + expected);
  }
  Q4T_CHECK(mcp_detail::TunnelCommandDeadline(
                Object({{"response_timeout", Number(0)}}), received, 30s) ==
            received + 30s);
  return true;
}

Q4T_TEST(mcp_agent_modern_session_call_resource_delete) {
  StatefulMcp remote;
  TunnelAgentBackend agent(remote.server.Endpoint(), remote.server.Options());
  const auto init = AgentExchange(agent, AgentCommand());
  Q4T_CHECK(AgentOk(init));
  const auto headers = AgentSessionHeaders(init);
  Q4T_CHECK(AgentOk(AgentExchange(agent, AgentCommand("tools/list", headers))));
  Q4T_CHECK(AgentOk(AgentExchange(agent, AgentCommand("tools/call", headers))));
  const auto resource =
      AgentExchange(agent, AgentCommand("resources/read", headers));
  Q4T_CHECK(AgentOk(resource) &&
            OutputJson(resource.value).find("same session resource") !=
                std::string::npos);
  auto terminate = AgentCommand("ignored", headers);
  Set(&terminate, "command_type", String("session_termination"));
  const auto closed = AgentExchange(agent, terminate);
  Q4T_CHECK(closed.ok() && closed.value.GetInt("resp_code") == 204 &&
            closed.value.GetString("resp_type") ==
                "session_termination_response");
  Q4T_CHECK(AgentExchange(agent, AgentCommand("tools/call", headers))
                .value.GetInt("resp_code") == 404);
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1 && remote.closed == 1);
  return true;
}

Q4T_TEST(mcp_agent_legacy_session_continuity_and_cleanup) {
  LegacyStatefulMcp remote;
  TunnelAgentBackend agent(remote.server.Endpoint(), remote.server.Options());
  const auto init = AgentExchange(agent, AgentCommand());
  Q4T_CHECK(AgentOk(init));
  const auto headers = AgentSessionHeaders(init, "2024-11-05");
  Q4T_CHECK(AgentOk(AgentExchange(agent, AgentCommand("tools/list", headers))));
  Q4T_CHECK(AgentOk(AgentExchange(agent, AgentCommand("tools/call", headers))));
  Q4T_CHECK(
      AgentOk(AgentExchange(agent, AgentCommand("resources/read", headers))));
  agent.Clear();
  Q4T_CHECK(AgentExchange(agent, AgentCommand("resources/read", headers))
                .value.GetInt("resp_code") == 404);
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1);
  return true;
}

Q4T_TEST(mcp_agent_legacy_cross_origin_denied) {
  Server remote, other;
  remote.handler = [&](const Request& r, int fd, std::stop_token) {
    if (r.verb == "POST")
      Send(fd, Http(405));
    else
      Send(fd,
           Http(200, "event: endpoint\ndata: " + other.Endpoint().url + "\n\n",
                "text/event-stream"));
    return true;
  };
  auto target = remote.Endpoint();
  target.authorization = "private-token";
  auto network = remote.Options();
  network.ports.push_back(other.port);
  TunnelAgentBackend agent(target, network);
  const auto result = AgentExchange(agent, AgentCommand());
  Q4T_CHECK(result.ok() && result.value.GetInt("resp_code") == 403 &&
            other.Requests().empty());
  return true;
}

Q4T_TEST(mcp_agent_headers_conflicts_before_private_io) {
  Server remote;
  auto target = remote.Endpoint();
  target.authorization = "operator-token";
  TunnelAgentBackend agent(target, remote.Options());
  for (const auto& headers :
       {Object({{"Authorization", Array({String("Bearer delegated")})}}),
        Object({{"Mcp-Session-Id", Array({String("unknown")})}}),
        Object({{"X-Custom", Array({String("one\r\nInjected: bad")})}}),
        Object({{"X-Custom", Array({String("one")})},
                {"x-custom", Array({String("two")})}})}) {
    const auto result =
        AgentExchange(agent, AgentCommand("tools/call", headers));
    Q4T_CHECK(result.ok() && result.value.GetInt("resp_code") >= 400);
  }
  Q4T_CHECK(remote.Requests().empty());
  auto headers =
      Object({{"Authorization", Array({String("Bearer operator-token")})},
              {"Cookie", Array({String("a=1"), String("b=2")})},
              {"X-Latin", Array({String("café")})},
              {"Connection", Array({String("x-omit")})},
              {"X-Omit", Array({String("hidden")})}});
  Q4T_CHECK(AgentOk(AgentExchange(agent, AgentCommand("tools/call", headers))));
  const auto rows = remote.Requests();
  Q4T_CHECK(rows.size() == 1 && !rows[0].headers.contains("x-omit") &&
            rows[0].headers.at("cookie") == "a=1; b=2" &&
            rows[0].headers.at("x-latin") == "caf\xe9" &&
            rows[0].headers.at("authorization") == "Bearer operator-token");
  return true;
}

Q4T_TEST(mcp_agent_exact_ids_and_non200_errors) {
  Server remote;
  remote.handler = [](const Request& r, int fd, std::stop_token) {
    auto answer = Object(
        {{"jsonrpc", String("2.0")},
         {"id", At(r.rpc, "id")},
         {"error", Object({{"code", Number(-32010)},
                           {"message", String("原始错误")},
                           {"data", Object({{"retry", Boolean(false)}})}})}});
    Send(fd, Http(409, OutputJson(answer)));
    return true;
  };
  TunnelAgentBackend agent(remote.Endpoint(), remote.Options());
  Json integer;
  Q4T_CHECK(Parse("9007199254740993", &integer));
  for (const auto& id : {integer, String("用户原始ID")}) {
    const auto reply =
        AgentExchange(agent, AgentCommand("tools/call", Object(), id));
    Q4T_CHECK(reply.ok() && reply.value.GetInt("resp_code") == 409 &&
              OutputJson(At(At(reply.value, "resp_json"), "id")) ==
                  OutputJson(id));
    const auto& error = At(At(reply.value, "resp_json"), "error");
    Q4T_CHECK(error.GetInt("code") == -32010 &&
              error.GetString("message") == "原始错误" &&
              !At(error, "data").GetBool("retry", true));
  }
  Q4T_CHECK(remote.Requests().size() == 2);
  return true;
}

Q4T_TEST(mcp_agent_failed_progress_and_ping_preserve_terminal) {
  Server remote;
  remote.handler = [](const Request& r, int fd, std::stop_token stop) {
    if (r.rpc.GetString("method") != "tools/call") return false;
    Send(fd,
         "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
         "Transfer-Encoding: chunked\r\n\r\n");
    for (int n = 0; n < 2; ++n)
      Chunk(fd,
            "data: "
            "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n");
    Chunk(
        fd,
        "data: {\"jsonrpc\":\"2.0\",\"method\":\"ping\",\"id\":\"ping\"}\n\n");
    Chunk(fd, "data: " + OutputJson(Rpc(r, Object({{"content", Array()}}))) +
                  "\n\n");
    Stall(stop);
    return true;
  };
  TunnelAgentBackend agent(remote.Endpoint(), remote.Options());
  int notifications = 0;
  const auto result = agent.Exchange(AgentCommand("tools/call"),
                                     Clock::now() + 2s, [&](const Json&) {
                                       ++notifications;
                                       return false;
                                     });
  Q4T_CHECK(AgentOk(result) && notifications == 1);
  const auto requests = remote.Requests();
  Q4T_CHECK(requests.size() == 2 && requests[1].rpc.GetString("id") == "ping" &&
            requests[1].rpc.Has("result"));
  return true;
}

Q4T_TEST(mcp_agent_no_tool_replay_after_404_or_disconnect) {
  StatefulMcp remote;
  TunnelAgentBackend agent(remote.server.Endpoint(), remote.server.Options());
  const auto init = AgentExchange(agent, AgentCommand());
  Q4T_CHECK(AgentOk(init));
  const auto headers = AgentSessionHeaders(init);
  remote.expire_call = true;
  const auto lost = AgentExchange(agent, AgentCommand("tools/call", headers));
  Q4T_CHECK(lost.ok() && lost.value.GetInt("resp_code") == 404 &&
            remote.dispatched == 1 && remote.initialized == 1);
  Q4T_CHECK(AgentExchange(agent, AgentCommand("resources/read", headers))
                    .value.GetInt("resp_code") == 404 &&
            remote.read == 0);
  Server broken;
  std::atomic<int> calls{0};
  broken.handler = [&](const Request&, int, std::stop_token) {
    ++calls;
    return true;
  };
  TunnelAgentBackend disconnected(broken.Endpoint(), broken.Options());
  const auto unknown = AgentExchange(disconnected, AgentCommand("tools/call"));
  Q4T_CHECK(unknown.ok() && unknown.value.GetBool("q4t_outcome_unknown") &&
            unknown.value.GetInt("resp_code") >= 400 && calls == 1 &&
            At(At(At(unknown.value, "resp_json"), "error"), "data")
                .GetBool("outcome_unknown"));
  return true;
}

Q4T_TEST(mcp_agent_invalid_initialize_cleanup) {
  Server remote;
  std::atomic<int> deleted{0};
  remote.handler = [&](const Request& r, int fd, std::stop_token) {
    if (r.verb == "DELETE") {
      if (r.headers.at("mcp-session-id") == "allocated-before-invalid")
        ++deleted;
      Send(fd, Http(204));
    } else {
      Send(fd, Http(200, OutputJson(Rpc(r, Object())), "application/json",
                    "Mcp-Session-Id: allocated-before-invalid\r\n"));
    }
    return true;
  };
  TunnelAgentBackend agent(remote.Endpoint(), remote.Options());
  const auto result = AgentExchange(agent, AgentCommand());
  Q4T_CHECK(result.ok() && result.value.GetInt("resp_code") == 502 &&
            deleted == 1);
  return true;
}

Q4T_TEST(mcp_agent_capacity_frees_on_termination) {
  StatefulMcp remote;
  TunnelAgentBackend agent(remote.server.Endpoint(), remote.server.Options());
  Json first;
  for (int n = 0; n < 16; ++n) {
    const auto init = AgentExchange(agent, AgentCommand());
    Q4T_CHECK(AgentOk(init));
    if (!n) first = AgentSessionHeaders(init);
  }
  Q4T_CHECK(AgentExchange(agent, AgentCommand()).value.GetInt("resp_code") ==
                503 &&
            remote.initialized == 16);
  auto terminate = AgentCommand("ignored", first);
  Set(&terminate, "command_type", String("session_termination"));
  Q4T_CHECK(AgentExchange(agent, terminate).value.GetInt("resp_code") == 204);
  Q4T_CHECK(AgentOk(AgentExchange(agent, AgentCommand())) &&
            remote.initialized == 17);
  agent.Clear();
  Q4T_CHECK(remote.closed == 17);
  return true;
}

Q4T_TEST(mcp_agent_fixed_target_deadline_and_payload_bounds) {
  Server remote, other;
  std::atomic<bool> oversized{false};
  remote.handler = [&](const Request& r, int fd, std::stop_token) {
    if (!oversized) return false;
    Send(fd, Http(200, OutputJson(Rpc(r, String(std::string(4U << 20, 'x'))))));
    return true;
  };
  TunnelAgentBackend agent(remote.Endpoint(), remote.Options());
  auto command = AgentCommand("tools/call");
  Set(&command, "url", String(other.Endpoint().url));
  Q4T_CHECK(AgentOk(AgentExchange(agent, command)) && other.Requests().empty());
  const auto expired = agent.Exchange(command, Clock::now() - 1ms);
  Q4T_CHECK(expired.value.GetInt("resp_code") == 408 && remote.calls == 1);
  oversized = true;
  const auto large = AgentExchange(agent, command);
  Q4T_CHECK(large.ok() && large.value.GetInt("resp_code") == 413 &&
            OutputJson(large.value).size() < 4096);
  return true;
}

namespace {
bool AgentBrokerRoundtrip(Server& remote, std::string protocol) {
  AgentBroker control;
  const auto config = AgentConfig(control.server, remote);
  std::atomic<int> result{-1};
  std::jthread supervisor([&](std::stop_token stop) {
    result = RunMcpTunnelAgent(config, [&] { return stop.stop_requested(); });
  });
  const auto init = control.Exchange(AgentCommand());
  Q4T_CHECK(AgentOk(init));
  const auto headers = AgentSessionHeaders(init, protocol);
  Q4T_CHECK(AgentOk(control.Exchange(AgentCommand("tools/list", headers))));
  Q4T_CHECK(AgentOk(control.Exchange(AgentCommand("tools/call", headers))));
  Q4T_CHECK(AgentOk(control.Exchange(AgentCommand("resources/read", headers))));
  Q4T_CHECK(control.Exchange(AgentCommand("ignored", headers), true)
                .value.GetInt("resp_code") == 204);
  supervisor.request_stop();
  supervisor.join();
  Q4T_CHECK(result == 0 && control.valid && !control.instance.empty());
  for (const auto& row : remote.Requests())
    Q4T_CHECK(!row.headers.contains("authorization") &&
              !row.headers.contains("x-tunnel-shard-token"));
  return true;
}
}  // namespace
Q4T_TEST(mcp_agent_broker_actual_http_roundtrip) {
  StatefulMcp remote;
  Q4T_CHECK(AgentBrokerRoundtrip(remote.server, "2025-06-18"));
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1 && remote.closed == 1);
  return true;
}
Q4T_TEST(mcp_agent_broker_actual_legacy_roundtrip) {
  LegacyStatefulMcp remote;
  Q4T_CHECK(AgentBrokerRoundtrip(remote.server, "2024-11-05"));
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 1 &&
            remote.read == 1);
  return true;
}
Q4T_TEST(mcp_agent_auth_failure_stops_no_restart) {
  Server control, remote;
  control.handler = [](const Request&, int fd, std::stop_token) {
    Send(fd, Http(401));
    return true;
  };
  const auto deadline = Clock::now() + 3s;
  Q4T_CHECK(RunMcpTunnelAgent(AgentConfig(control, remote),
                              [&] { return Clock::now() >= deadline; }) == 2);
  Q4T_CHECK(control.Requests().size() == 1 && remote.Requests().empty());
  return true;
}

Q4T_TEST(mcp_agent_auth_failure_cleanup_cannot_restart_owner) {
  Server control, remote;
  std::atomic<int> polls{0}, deletes{0};
  remote.handler = [&](const Request& r, int, std::stop_token stop) {
    if (r.verb != "DELETE") return false;
    ++deletes;
    Stall(stop);
    return true;
  };
  control.handler = [&](const Request& r, int fd, std::stop_token) {
    if (r.verb == "POST") {
      Send(fd, Http(401));
    } else {
      ++polls;
      auto command = AgentCommand();
      Set(&command, "shard_token", String("original-shard"));
      Send(fd, Http(200, OutputJson(Object({{"commands", Array({command})}}))));
    }
    return true;
  };
  const auto begin = Clock::now();
  Q4T_CHECK(RunMcpTunnelAgent(AgentConfig(control, remote),
                              [&] { return Clock::now() - begin >= 5s; }) == 2);
  Q4T_CHECK(polls == 1 && deletes == 1 && Clock::now() - begin < 3s);
  return true;
}

namespace {
// Wire faults live at the actual TCP upload boundary, after the backend call.
bool AgentDelivery(bool progress_failure, bool multiple) {
  Server control, remote;
  std::atomic<int> uploads{0}, notifications{0}, polls{0};
  std::atomic<bool> done{false}, valid{true};
  std::mutex mutex;
  std::string original;
  remote.handler = [&](const Request& r, int fd, std::stop_token) {
    ++remote.calls;
    if (progress_failure) {
      const auto notice =
          "data: "
          "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n";
      Send(fd,
           Http(200,
                std::string(notice) + notice + "data: " +
                    OutputJson(Rpc(r, Object({{"content", Array()}}))) + "\n\n",
                "text/event-stream"));
    } else {
      Send(fd, Http(200, OutputJson(Rpc(
                             r, String(std::string((1U << 20) + 32, 'a'))))));
    }
    return true;
  };
  control.handler = [&](const Request& r, int fd, std::stop_token) {
    if (r.verb == "GET") {
      if (++polls > 1) {
        Send(fd, Http(204));
        return true;
      }
      auto command = AgentCommand("tools/call");
      Set(&command, "shard_token", String("original-shard"));
      Set(&command, "response_timeout", String("5s"));
      auto commands = Array();
      if (multiple) {
        auto expired = command;
        Set(&expired, "response_timeout", String("0s"));
        commands.array.push_back(expired);
      }
      commands.array.push_back(command);
      if (multiple) {
        Set(&command, "request_id", String("second"));
        commands.array.push_back(command);
      }
      Send(fd, Http(200, OutputJson(Object({{"commands", commands}}))));
      return true;
    }
    if (r.headers.at("x-tunnel-shard-token") != "original-shard" ||
        r.headers.at("authorization") != "Bearer control-secret")
      valid = false;
    if (r.rpc.GetString("resp_type") == "jsonrpc_notify") {
      ++notifications;  // Deliberately lose the HTTP acknowledgement.
      return true;
    }
    const int count = ++uploads;
    {
      std::lock_guard lock(mutex);
      if (original.empty())
        original = r.body;
      else if (!multiple && original != r.body)
        valid = false;
    }
    if (!progress_failure && !multiple && count == 1) {
      Send(fd, Http(503, "", "application/json", "Retry-After: 0\r\n"));
    } else {
      Send(fd, Http(204));
      if (!multiple || count == 2) done = true;
    }
    return true;
  };
  const auto end = Clock::now() + 8s;
  const auto result = RunMcpTunnelAgent(AgentConfig(control, remote), [&] {
    return done || Clock::now() >= end;
  });
  Q4T_CHECK(result == 0 && done && valid);
  Q4T_CHECK(remote.calls == (multiple ? 2 : 1));
  Q4T_CHECK(uploads == (progress_failure ? 1 : 2));
  Q4T_CHECK(notifications == (progress_failure ? 1 : 0));
  return true;
}
}  // namespace
Q4T_TEST(mcp_agent_upload_retry_preserves_body_and_one_dispatch) {
  return AgentDelivery(false, false);
}
Q4T_TEST(mcp_agent_skips_expired_processes_multiple_poll) {
  return AgentDelivery(false, true);
}
Q4T_TEST(mcp_agent_failed_progress_upload_still_final) {
  return AgentDelivery(true, false);
}

Q4T_TEST(mcp_native_tunnel_synthetic_failure_remains_unknown) {
  for (int mode = 0; mode < 3; ++mode) {
    TunnelPeer peer;
    peer.rpc_failure = true;
    peer.transport_failure = mode == 1;
    peer.invalid_final = mode == 2;
    McpSession session;
    McpTransportReceipt receipt;
    Q4T_CHECK(peer.Invoke(&session, &receipt).ok());
    const auto result = peer.Invoke(&session, &receipt, "tools/call");
    Q4T_CHECK(receipt.call_permitted && peer.called == 1);
    if (!mode) {
      // Arbitrary upstream error data is not a transport authority flag.
      Q4T_CHECK(result.ok() && !receipt.outcome_unknown &&
                receipt.error.GetInt("code") == -32042);
    } else {
      Q4T_CHECK(result.code == "mcp_tunnel_outcome_unknown" &&
                receipt.outcome_unknown && receipt.error.IsNull());
    }
  }
  return true;
}

Q4T_TEST(responses_mcp_native_unknown_transport_is_durable) {
  TunnelPeer peer;
  peer.rpc_failure = true;
  peer.transport_failure = true;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = peer.Config();
  ResponsesExecution run(objects);
  Q4T_CHECK(
      ExecutionOk(run.Prepare(peer.Request(), config, ExecutionAccess(), 42)));
  std::string wire;
  Q4T_CHECK(ExecutionOk(run.Begin(&wire)) && ModelCall(&run, false, &wire));
  bool waiting = false;
  Q4T_CHECK(ExecutionOk(run.RunMcp(1, &waiting, {})) && !waiting &&
            peer.called == 1);
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
      peer.Request().GetArray("tools")->array.front(), &definition, &error));
  Q4T_CHECK(
      !state.Permit(call.GetString("id"), definition, ExecutionAccess()).ok() &&
      peer.called == 1);
  return true;
}

Q4T_TEST(realtime_mcp_native_import_preserves_actual_authority_and_secrets) {
  Server server;
  server.handler = [](const Request& request, int fd, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/list") return false;
    const auto* params = request.rpc.Find("params");
    const bool next = params && params->GetString("cursor") == "next";
    auto tool = Tool(next ? "hidden" : "read_project");
    Set(&tool, "outputSchema", Object({{"type", String("object")}}));
    auto result = Object({{"tools", Array({tool})}});
    if (!next) Set(&result, "nextCursor", String("next"));
    Send(fd, Http(200, OutputJson(Rpc(request, result))));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  auto tool = ExecutionRequest(server).GetArray("tools")->array[0];
  Set(&tool, "allowed_tools", Array({String("read_project")}));
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(tool, &definition, &error));
  RealtimeMcpCatalog imported;
  Q4T_CHECK(ExecutionOk(ImportRealtimeMcpCatalog(
      objects, config, definition, ExecutionAccess(), "mcpl_real", &imported)));
  Q4T_CHECK(imported.loaded && imported.item.GetString("id") == "mcpl_real" &&
            imported.item.GetArray("tools")->array.size() == 1);
  Q4T_CHECK(ValidateRealtimeShape(imported.item, RealtimeShape::kItem, &error));
  const auto projected = OutputJson(imported.item);
  for (const auto* hidden :
       {"mcp-request-secret", "mcp-header-secret", "opaque", "outputSchema"})
    Q4T_CHECK(projected.find(hidden) == projected.npos);
  ResponsesMcpState state(objects, config);
  auto opened = state.OpenCatalog(imported.authority, imported.definition,
                                  ExecutionAccess());
  Q4T_CHECK(ExecutionOk(opened));
  Q4T_CHECK(opened.value.GetArray("tools")->array[0].Has("outputSchema"));
  Q4T_CHECK(ExecutionOk(
      state.Session("mcpl_real", imported.definition, ExecutionAccess())));
  auto foreign = ExecutionAccess();
  foreign.scope = "client:foreign";
  Q4T_CHECK(!state.Authenticate(imported.authority, foreign).ok());
  size_t listed = 0;
  for (const auto& request : server.Requests()) {
    if (request.rpc.GetString("method") != "tools/list") continue;
    ++listed;
    Q4T_CHECK(request.headers.at("authorization") ==
              "Bearer mcp-request-secret");
    Q4T_CHECK(request.headers.at("x-private") == "mcp-header-secret");
  }
  Q4T_CHECK(listed == 2 && server.calls == 0);
  return true;
}

Q4T_TEST(realtime_mcp_catalog_seals_finite_session_across_owner_restart) {
  StatefulMcp remote;
  ExecutionFixture fixture;
  const auto config = ExecutionConfig(remote.server);
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(
      ExecutionRequest(remote.server).GetArray("tools")->array[0], &definition,
      &error));
  RealtimeMcpCatalog imported;
  Json original;
  {
    ObjectStore objects(fixture.options);
    Q4T_CHECK(ExecutionOk(ImportRealtimeMcpCatalog(
        objects, config, definition, ExecutionAccess(), "mcpl_restart",
        &imported)));
    Q4T_CHECK(imported.loaded && remote.initialized == 1);
    ResponsesMcpState state(objects, config);
    const auto sealed = state.Session("mcpl_restart", definition,
                                      ExecutionAccess());
    Q4T_CHECK(ExecutionOk(sealed));
    original = *sealed.value.Find("session");
  }
  const auto created = original.GetInt("created_ms");
  const auto expires = original.GetInt("expires_ms");
  Q4T_CHECK(created > 0 && expires > created &&
            expires - created <= 3600000 &&
            original.GetInt("idle_ms") > 0 &&
            original.GetInt("idle_ms") <= 300000);
  Q4T_CHECK(CloseMcpSessions(false) && remote.closed == 0);
  ObjectStore reopened(fixture.options);
  ResponsesMcpState state(reopened, config);
  const auto restored = state.Session("mcpl_restart", definition,
                                      ExecutionAccess());
  Q4T_CHECK(ExecutionOk(restored) &&
            OutputJson(*restored.value.Find("session")) == OutputJson(original));
  auto foreign = ExecutionAccess();
  foreign.scope = "client:foreign";
  Q4T_CHECK(!state.Session("mcpl_restart", definition, foreign).ok());
  McpSession session;
  session.state = *restored.value.Find("session");
  session.allow_new = false;
  const auto access = ExecutionAccess();
  McpTransportReceipt receipt;
  Q4T_CHECK(InvokeMcp({access.scope, access.deadline, access.cancelled},
                      definition.credentials, remote.server.Options(),
                      "tools/list", Object(), {}, &receipt, {}, &session).ok());
  Q4T_CHECK(remote.initialized == 1 && remote.dispatched == 0 &&
            session.state.GetInt("created_ms") == created &&
            session.state.GetInt("expires_ms") == expires);
  Q4T_CHECK(CloseMcpSessions() && remote.closed == 1);
  return true;
}

Q4T_TEST(realtime_mcp_native_import_records_remote_failure_without_callables) {
  Server server;
  server.handler = [](const Request& request, int fd, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/list") return false;
    auto rpc = Object(
        {{"jsonrpc", String("2.0")},
         {"id", *request.rpc.Find("id")},
         {"error", Object({{"code", Number(-32603)},
                           {"message", String("fixture remote failure")}})}});
    Send(fd, Http(200, OutputJson(rpc)));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(
      ExecutionRequest(server).GetArray("tools")->array[0], &definition,
      &error));
  RealtimeMcpCatalog imported;
  Q4T_CHECK(ExecutionOk(ImportRealtimeMcpCatalog(objects, config, definition,
                                                 ExecutionAccess(),
                                                 "mcpl_failed", &imported)));
  Q4T_CHECK(!imported.loaded &&
            imported.item.GetArray("tools")->array.empty() &&
            !imported.item.Has("error") && imported.authority.Has("error"));
  ResponsesMcpState state(objects, config);
  Q4T_CHECK(
      ExecutionOk(state.Authenticate(imported.authority, ExecutionAccess())));
  Q4T_CHECK(server.calls == 0);
  return true;
}

Q4T_TEST(realtime_mcp_native_import_rejects_remote_schema_without_success) {
  Server server;
  server.handler = [](const Request& request, int fd, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/list") return false;
    auto tool = Tool();
    Set(&tool, "inputSchema", Object({{"type", String("array")}}));
    Send(fd, Http(200, OutputJson(
                           Rpc(request, Object({{"tools", Array({tool})}})))));
    return true;
  };
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  const auto config = ExecutionConfig(server);
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(
      ExecutionRequest(server).GetArray("tools")->array[0], &definition,
      &error));
  RealtimeMcpCatalog imported;
  Q4T_CHECK(ExecutionOk(ImportRealtimeMcpCatalog(objects, config, definition,
                                                 ExecutionAccess(),
                                                 "mcpl_invalid", &imported)));
  Q4T_CHECK(!imported.loaded &&
            imported.item.GetArray("tools")->array.empty() &&
            !imported.authority.GetString("error").empty());
  Q4T_CHECK(server.calls == 0 && !server.Requests().empty());
  return true;
}

Q4T_TEST(realtime_mcp_native_import_cancel_identity_and_scope_preflight_no_io) {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects(fixture.options);
  auto config = ExecutionConfig(server);
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(
      ExecutionRequest(server).GetArray("tools")->array[0], &definition,
      &error));
  RealtimeMcpCatalog imported;
  imported.item = String("unchanged");
  auto access = ExecutionAccess();
  access.cancelled = [] { return true; };
  Q4T_CHECK(ImportRealtimeMcpCatalog(objects, config, definition, access,
                                     "mcpl_cancel", &imported)
                .http_status == 499);
  access = ExecutionAccess();
  access.deadline = Clock::now() - 1ms;
  Q4T_CHECK(ImportRealtimeMcpCatalog(objects, config, definition, access,
                                     "mcpl_expired", &imported)
                .http_status == 408);
  Q4T_CHECK(!ImportRealtimeMcpCatalog(objects, config, definition,
                                      ExecutionAccess(), "invalid", &imported)
                 .ok());
  auto declaration =
      Object({{"type", String("mcp")},
              {"server_label", String("drive")},
              {"connector_id", String("connector_googledrive")}});
  Q4T_CHECK(PlanResponsesMcpDefinition(declaration, &definition, &error));
  auto policy = std::make_shared<McpDeploymentPolicy>();
  policy->fetch = server.Options();
  policy->targets.push_back({"connector_id",
                             "connector_googledrive",
                             server.Endpoint().url,
                             "configured",
                             {"client:other"}});
  config.mcp_policy = policy;
  Q4T_CHECK(ImportRealtimeMcpCatalog(objects, config, definition,
                                     ExecutionAccess(), "mcpl_denied",
                                     &imported)
                .http_status == 403);
  Q4T_CHECK(imported.item.str == "unchanged" && server.Requests().empty() &&
            server.calls == 0);
  return true;
}

namespace {
Json RealtimeGeneratedCall() {
  return Object({{"type", String("mcp_call")},
                 {"id", String("mcp_realtime")},
                 {"server_label", String("project")},
                 {"name", String("read_project")},
                 {"arguments", String(" { \"path\" : \"docs/MODEL.md\" } ")}});
}
Json RealtimeDecision(const Json& approval, bool approve) {
  return Object({{"type", String("mcp_approval_response")},
                 {"id", String("item_decision")},
                 {"approval_request_id", *approval.Find("id")},
                 {"approve", Boolean(approve)}});
}
Json RealtimeAwaitingCall(const Json& approval) {
  auto call = RealtimeGeneratedCall();
  Set(&call, "approval_request_id", *approval.Find("id"));
  return call;
}
struct RealtimeNativeFixture {
  Server server;
  ExecutionFixture fixture;
  ObjectStore objects{fixture.options};
  ChatPlanConfig config = ExecutionConfig(server);
  RealtimeMcpCatalog catalog;
  bool Import(std::string approval = "never") {
    ResponsesMcpDefinition definition;
    ChatPlanError error;
    return PlanResponsesMcpDefinition(
               ExecutionRequest(server, std::move(approval))
                   .GetArray("tools")
                   ->array[0],
               &definition, &error) &&
           ExecutionOk(ImportRealtimeMcpCatalog(objects, config, definition,
                                                ExecutionAccess(),
                                                "mcpl_realtime", &catalog)) &&
           catalog.loaded;
  }
  ObjectReply Propose() {
    return ProposeRealtimeMcpCall(objects, config, catalog,
                                  RealtimeGeneratedCall(), "resp_realtime",
                                  ExecutionAccess());
  }
  ObjectReply Execute(const Json& call, RealtimeMcpExecution* result,
                      StateAccess access = ExecutionAccess()) {
    return ExecuteRealtimeMcpCall(objects, config, catalog.definition, call,
                                  "resp_realtime", access, result);
  }
};
}  // namespace

Q4T_TEST(realtime_mcp_native_execution_dispatches_actual_call_at_most_once) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed) &&
            proposed.value.GetString("type") == "mcp_call" &&
            f.server.calls == 0);
  Q4T_CHECK(!f.Propose().ok());
  RealtimeMcpExecution result;
  Q4T_CHECK(ExecutionOk(f.Execute(proposed.value, &result)));
  Q4T_CHECK(result.call_permitted && !result.outcome_unknown &&
            result.item.GetString("status") == "completed" &&
            result.item.GetString("arguments") ==
                RealtimeGeneratedCall().GetString("arguments") &&
            result.item.GetString("output").find("document result") !=
                std::string::npos &&
            f.server.calls == 1);
  const auto saved = OutputJson(result.item);
  Q4T_CHECK(!f.Execute(proposed.value, &result).ok() &&
            OutputJson(result.item) == saved && f.server.calls == 1);
  ResponsesMcpState state(f.objects, f.config);
  Q4T_CHECK(ExecutionOk(state.Authenticate(result.item, ExecutionAccess())));
  return true;
}

Q4T_TEST(realtime_mcp_native_approval_is_explicit_single_use_and_exact_call) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import("always"));
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed) &&
            proposed.value.GetString("type") == "mcp_approval_request");
  auto expected = RealtimeAwaitingCall(proposed.value);
  auto premature = expected;
  Set(&premature, "status", String("in_progress"));
  RealtimeMcpExecution result;
  Q4T_CHECK(!f.Execute(premature, &result).ok() && f.server.calls == 0);
  const auto decision = RealtimeDecision(proposed.value, true);
  auto forged = expected;
  Set(&forged, "arguments", String("{}"));
  Q4T_CHECK(!DecideRealtimeMcpCall(f.objects, f.config, f.catalog.definition,
                                   forged, decision, "resp_realtime",
                                   ExecutionAccess())
                 .ok());
  const auto decided =
      DecideRealtimeMcpCall(f.objects, f.config, f.catalog.definition, expected,
                            decision, "resp_realtime", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(decided) && f.server.calls == 0);
  Q4T_CHECK(!DecideRealtimeMcpCall(f.objects, f.config, f.catalog.definition,
                                   expected, decision, "resp_realtime",
                                   ExecutionAccess())
                 .ok());
  Q4T_CHECK(ExecutionOk(f.Execute(decided.value, &result)) &&
            result.item.GetString("approval_request_id") ==
                proposed.value.GetString("id") &&
            f.server.calls == 1);
  return true;
}

Q4T_TEST(realtime_mcp_native_denial_is_durable_and_never_dispatches) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import("always"));
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  auto decision = RealtimeDecision(proposed.value, false);
  Set(&decision, "reason", String("client declined"));
  const auto denied =
      DecideRealtimeMcpCall(f.objects, f.config, f.catalog.definition,
                            RealtimeAwaitingCall(proposed.value), decision,
                            "resp_realtime", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(denied) &&
            denied.value.GetString("status") == "failed");
  RealtimeMcpExecution result;
  Q4T_CHECK(!f.Execute(denied.value, &result).ok() && f.server.calls == 0);
  ResponsesMcpState state(f.objects, f.config);
  Q4T_CHECK(ExecutionOk(state.Authenticate(denied.value, ExecutionAccess())));
  RealtimeMcpCallProjection projected;
  ChatPlanError error;
  Q4T_CHECK(ProjectRealtimeMcpCall(denied.value, &projected, &error) &&
            projected.phase == RealtimeMcpCallProjection::Phase::kFailed);
  return true;
}

Q4T_TEST(realtime_mcp_native_forged_call_scope_and_target_do_no_remote_io) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  const auto requests = f.server.Requests().size();
  RealtimeMcpExecution result;
  result.item = String("unchanged");
  for (const auto field : {"name", "server_label", "arguments", "id"}) {
    auto forged = proposed.value;
    Set(&forged, field, String("different"));
    Q4T_CHECK(!f.Execute(forged, &result).ok());
  }
  auto foreign = ExecutionAccess();
  foreign.scope = "client:foreign";
  Q4T_CHECK(!f.Execute(proposed.value, &result, foreign).ok());
  auto changed = f.catalog.definition;
  Set(&changed.declaration, "server_url", String("http://127.0.0.1:1/mcp"));
  Q4T_CHECK(!ExecuteRealtimeMcpCall(f.objects, f.config, changed,
                                    proposed.value, "resp_realtime",
                                    ExecutionAccess(), &result)
                 .ok());
  Q4T_CHECK(result.item.str == "unchanged" && f.server.calls == 0 &&
            f.server.Requests().size() == requests);
  return true;
}

Q4T_TEST(realtime_mcp_native_execution_keeps_original_response_ownership) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  RealtimeMcpExecution result;
  const auto requests = f.server.Requests().size();
  const auto rejected = ExecuteRealtimeMcpCall(
      f.objects, f.config, f.catalog.definition, proposed.value, "resp_other",
      ExecutionAccess(), &result);
  Q4T_CHECK(rejected.code == "realtime_mcp_owner_conflict" &&
            f.server.Requests().size() == requests && f.server.calls == 0);
  Q4T_CHECK(ExecutionOk(f.Execute(proposed.value, &result)) &&
            f.server.calls == 1);
  return true;
}

Q4T_TEST(
    realtime_mcp_native_missing_credentials_do_not_initialize_anonymously) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  auto anonymous = f.catalog.definition;
  anonymous.credentials.authorization.clear();
  anonymous.credentials.headers.clear();
  const auto requests = f.server.Requests().size();
  RealtimeMcpExecution result;
  const auto rejected =
      ExecuteRealtimeMcpCall(f.objects, f.config, anonymous, proposed.value,
                             "resp_realtime", ExecutionAccess(), &result);
  Q4T_CHECK(rejected.code == "mcp_credentials_required" &&
            f.server.Requests().size() == requests);
  f.catalog.definition.credentials.authorization = "replacement-fixture-key";
  Q4T_CHECK(ExecutionOk(f.Execute(proposed.value, &result)) &&
            f.server.calls == 1);
  size_t sent = 0;
  for (const auto& request : f.server.Requests()) {
    if (request.rpc.GetString("method") != "tools/call") continue;
    ++sent;
    Q4T_CHECK(request.headers.at("authorization") ==
              "Bearer replacement-fixture-key");
  }
  Q4T_CHECK(sent == 1);
  return true;
}

Q4T_TEST(realtime_mcp_native_tightened_policy_blocks_before_remote_io) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  const auto requests = f.server.Requests().size();
  RealtimeMcpExecution result;
  auto changed = f.catalog.definition;
  Set(&changed.declaration, "require_approval", String("always"));
  Q4T_CHECK(ExecuteRealtimeMcpCall(f.objects, f.config, changed, proposed.value,
                                   "resp_realtime", ExecutionAccess(), &result)
                .code == "mcp_approval_required");
  changed = f.catalog.definition;
  Set(&changed.declaration, "allowed_tools", Array());
  Q4T_CHECK(ExecuteRealtimeMcpCall(f.objects, f.config, changed, proposed.value,
                                   "resp_realtime", ExecutionAccess(), &result)
                .code == "mcp_tool_denied");
  Q4T_CHECK(f.server.calls == 0 && f.server.Requests().size() == requests);
  return true;
}

Q4T_TEST(realtime_mcp_native_cancel_and_expired_preflight_do_not_consume_call) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  auto access = ExecutionAccess();
  access.cancelled = [] { return true; };
  RealtimeMcpExecution result;
  const auto requests = f.server.Requests().size();
  Q4T_CHECK(f.Execute(proposed.value, &result, access).http_status == 499);
  access = ExecutionAccess();
  access.deadline = Clock::now();
  Q4T_CHECK(f.Execute(proposed.value, &result, access).http_status == 408);
  Q4T_CHECK(f.server.calls == 0 && f.server.Requests().size() == requests);
  ResponsesMcpState state(f.objects, f.config);
  const auto pending = state.Inspect("mcp_realtime", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(pending) &&
            pending.value.GetString("state") == "ready");
  // Explicit owner action after the rejected attempts, never an automatic
  // retry.
  Q4T_CHECK(ExecutionOk(f.Execute(proposed.value, &result)) &&
            f.server.calls == 1);
  return true;
}

Q4T_TEST(realtime_mcp_native_protocol_failure_is_durable_not_success) {
  RealtimeNativeFixture f;
  f.server.handler = [&](const Request& request, int fd, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/call") return false;
    ++f.server.calls;
    Send(fd,
         Http(200, OutputJson(Object(
                       {{"jsonrpc", String("2.0")},
                        {"id", *request.rpc.Find("id")},
                        {"error",
                         Object({{"code", Number(-32603)},
                                 {"message", String("remote failure")}})}}))));
    return true;
  };
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  RealtimeMcpExecution result;
  Q4T_CHECK(ExecutionOk(f.Execute(proposed.value, &result)) &&
            result.item.GetString("status") == "failed" &&
            !result.outcome_unknown);
  Q4T_CHECK(result.item.Find("error")->GetString("type") ==
            "mcp_protocol_error");
  Q4T_CHECK(!f.Execute(proposed.value, &result).ok() && f.server.calls == 1);
  return true;
}

Q4T_TEST(realtime_mcp_native_tool_error_preserves_actual_structured_result) {
  RealtimeNativeFixture f;
  const auto actual = Object(
      {{"content", Array({Object({{"type", String("text")},
                                  {"text", String("cannot read file")}})})},
       {"structuredContent", Object({{"path", String("docs/MODEL.md")},
                                     {"allowed", Boolean(false)}})},
       {"isError", Boolean(true)}});
  f.server.handler = [&](const Request& request, int fd, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/call") return false;
    ++f.server.calls;
    Send(fd, Http(200, OutputJson(Rpc(request, actual))));
    return true;
  };
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  RealtimeMcpExecution result;
  Q4T_CHECK(ExecutionOk(f.Execute(proposed.value, &result)) &&
            result.item.GetString("status") == "failed" &&
            result.item.GetString("output") == OutputJson(actual));
  RealtimeMcpCallProjection projected;
  ChatPlanError error;
  Q4T_CHECK(ProjectRealtimeMcpCall(result.item, &projected, &error) &&
            projected.item.GetString("output") == OutputJson(actual) &&
            projected.item.Find("error")->GetString("type") ==
                "tool_execution_error" &&
            f.server.calls == 1);
  return true;
}

Q4T_TEST(
    realtime_mcp_native_cancel_after_permission_is_unknown_without_replay) {
  RealtimeNativeFixture f;
  std::atomic<bool> cancelled{false};
  f.server.handler = [&](const Request& request, int, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/call") return false;
    ++f.server.calls;
    cancelled = true;
    return true;  // Remote work occurred, but no result is delivered.
  };
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  auto access = ExecutionAccess();
  access.cancelled = [&] { return cancelled.load(); };
  RealtimeMcpExecution result;
  Q4T_CHECK(ExecutionOk(f.Execute(proposed.value, &result, access)) &&
            result.call_permitted && result.outcome_unknown &&
            result.item.GetString("status") == "failed" && f.server.calls == 1);
  ResponsesMcpState state(f.objects, f.config);
  const auto durable = state.Inspect("mcp_realtime", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(durable) &&
            durable.value.GetString("state") == "unknown");
  Q4T_CHECK(!f.Execute(proposed.value, &result).ok() && f.server.calls == 1);
  return true;
}

Q4T_TEST(
    realtime_mcp_native_proposal_rejects_unowned_catalog_and_bad_arguments) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto requests = f.server.Requests().size();
  for (const auto arguments : {"{", "[]", "{\"path\":1,\"path\":2}"}) {
    auto call = RealtimeGeneratedCall();
    Set(&call, "arguments", String(arguments));
    Q4T_CHECK(!ProposeRealtimeMcpCall(f.objects, f.config, f.catalog, call,
                                      "resp_realtime", ExecutionAccess())
                   .ok());
  }
  auto forged = f.catalog;
  Set(&forged.authority, "server_label", String("other"));
  Q4T_CHECK(!ProposeRealtimeMcpCall(f.objects, f.config, forged,
                                    RealtimeGeneratedCall(), "resp_realtime",
                                    ExecutionAccess())
                 .ok());
  forged = f.catalog;
  forged.loaded = false;
  Q4T_CHECK(!ProposeRealtimeMcpCall(f.objects, f.config, forged,
                                    RealtimeGeneratedCall(), "resp_realtime",
                                    ExecutionAccess())
                 .ok());
  auto call = RealtimeGeneratedCall();
  Set(&call, "approval_request_id", String("mcpr_forged"));
  Q4T_CHECK(!ProposeRealtimeMcpCall(f.objects, f.config, f.catalog, call,
                                    "resp_realtime", ExecutionAccess())
                 .ok());
  Q4T_CHECK(f.server.calls == 0 && f.server.Requests().size() == requests);
  Q4T_CHECK(ExecutionOk(f.Propose()));
  return true;
}

Q4T_TEST(
    realtime_mcp_native_denial_survives_missing_optional_deployment_route) {
  RealtimeNativeFixture f;
  auto policy = std::make_shared<McpDeploymentPolicy>();
  policy->fetch = f.server.Options();
  policy->targets.push_back({"connector_id",
                             "connector_dropbox",
                             f.server.Endpoint().url,
                             "generation1",
                             {ExecutionAccess().scope}});
  f.config.mcp_policy = policy;
  auto tool = ExecutionRequest(f.server, "always").GetArray("tools")->array[0];
  std::erase_if(tool.object,
                [](const auto& field) { return field.first == "server_url"; });
  Set(&tool, "connector_id", String("connector_dropbox"));
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(tool, &definition, &error));
  Q4T_CHECK(ExecutionOk(ImportRealtimeMcpCatalog(f.objects, f.config,
                                                 definition, ExecutionAccess(),
                                                 "mcpl_realtime", &f.catalog)));
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  f.config.mcp_policy.reset();
  const auto expected = RealtimeAwaitingCall(proposed.value);
  const auto requests = f.server.Requests().size();
  Q4T_CHECK(!DecideRealtimeMcpCall(f.objects, f.config, f.catalog.definition,
                                   expected,
                                   RealtimeDecision(proposed.value, true),
                                   "resp_realtime", ExecutionAccess())
                 .ok());
  const auto denied =
      DecideRealtimeMcpCall(f.objects, f.config, f.catalog.definition, expected,
                            RealtimeDecision(proposed.value, false),
                            "resp_realtime", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(denied) &&
            denied.value.GetString("status") == "failed" &&
            f.server.calls == 0 && f.server.Requests().size() == requests);
  return true;
}

Q4T_TEST(realtime_mcp_native_abandon_unsent_call_requires_original_owner) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  const auto requests = f.server.Requests().size();
  Q4T_CHECK(AbandonRealtimeMcpCall(f.objects, f.config, f.catalog.definition,
                                   RealtimeGeneratedCall(), "resp_other",
                                   ExecutionAccess())
                .code == "realtime_mcp_owner_conflict");
  const auto ended = AbandonRealtimeMcpCall(
      f.objects, f.config, f.catalog.definition, RealtimeGeneratedCall(),
      "resp_realtime", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(ended) && ended.value.GetString("status") == "failed" &&
            ended.value.Find("error")->Find("content")->GetString("code") ==
                "mcp_execution_interrupted");
  RealtimeMcpExecution result;
  Q4T_CHECK(!f.Execute(proposed.value, &result).ok());
  Q4T_CHECK(f.server.calls == 0 && f.server.Requests().size() == requests);
  return true;
}

Q4T_TEST(realtime_mcp_native_abandon_unpublished_approval_denies_without_io) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import("always") && ExecutionOk(f.Propose()));
  const auto requests = f.server.Requests().size();
  const auto ended = AbandonRealtimeMcpCall(
      f.objects, f.config, f.catalog.definition, RealtimeGeneratedCall(),
      "resp_realtime", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(ended) && ended.value.GetString("status") == "failed");
  ResponsesMcpState state(f.objects, f.config);
  const auto durable = state.Inspect("mcp_realtime", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(durable) &&
            durable.value.GetString("state") == "denied");
  Q4T_CHECK(f.server.calls == 0 && f.server.Requests().size() == requests);
  return true;
}

Q4T_TEST(realtime_mcp_native_abandon_preserves_known_completion_exactly) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  RealtimeMcpExecution result;
  Q4T_CHECK(ExecutionOk(f.Execute(proposed.value, &result)));
  const auto requests = f.server.Requests().size();
  for (int i = 0; i < 2; ++i) {
    const auto ended = AbandonRealtimeMcpCall(
        f.objects, f.config, f.catalog.definition, RealtimeGeneratedCall(),
        "resp_realtime", ExecutionAccess());
    Q4T_CHECK(ExecutionOk(ended) &&
              OutputJson(ended.value) == OutputJson(result.item));
  }
  Q4T_CHECK(f.server.calls == 1 && f.server.Requests().size() == requests);
  return true;
}

Q4T_TEST(realtime_mcp_native_old_cleanup_cannot_cancel_resumed_approval_owner) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import("always"));
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  const auto decided =
      DecideRealtimeMcpCall(f.objects, f.config, f.catalog.definition,
                            RealtimeAwaitingCall(proposed.value),
                            RealtimeDecision(proposed.value, true),
                            "resp_resumed", ExecutionAccess());
  Q4T_CHECK(ExecutionOk(decided));
  Q4T_CHECK(AbandonRealtimeMcpCall(f.objects, f.config, f.catalog.definition,
                                   RealtimeGeneratedCall(), "resp_realtime",
                                   ExecutionAccess())
                .code == "realtime_mcp_owner_conflict");
  RealtimeMcpExecution result;
  Q4T_CHECK(ExecutionOk(ExecuteRealtimeMcpCall(
                f.objects, f.config, f.catalog.definition, decided.value,
                "resp_resumed", ExecutionAccess(), &result)) &&
            f.server.calls == 1);
  return true;
}

Q4T_TEST(realtime_mcp_native_session_save_conflict_preserves_durable_outcome) {
  RealtimeNativeFixture f;
  std::atomic<bool> newer_session_saved{false};
  f.server.handler = [&](const Request& request, int fd, std::stop_token) {
    if (request.rpc.GetString("method") != "tools/call") return false;
    ++f.server.calls;
    ResponsesMcpState state(f.objects, f.config);
    const auto current =
        state.Session("mcpl_realtime", f.catalog.definition, ExecutionAccess());
    if (ExecutionOk(current)) {
      auto changed = *current.value.Find("session");
      Set(&changed, "lost", Boolean(true));
      const auto saved = state.BindSession(
          "mcpl_realtime", f.catalog.definition,
          current.value.GetInt("revision"), changed, ExecutionAccess());
      newer_session_saved =
          ExecutionOk(saved) && saved.value.GetInt("revision") ==
                                    current.value.GetInt("revision") + 1;
    }
    Send(fd,
         Http(404));  // Session loss after dispatch; actual outcome unknown.
    return true;
  };
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  RealtimeMcpExecution result;
  const auto completed = f.Execute(proposed.value, &result);
  Q4T_CHECK(ExecutionOk(completed) && newer_session_saved &&
            !result.session_saved && !result.session_error.empty() &&
            result.item.GetString("status") == "failed" &&
            result.outcome_unknown &&
            OutputJson(completed.value) == OutputJson(result.item));
  ResponsesMcpState state(f.objects, f.config);
  const auto durable = state.Authenticate(result.item, ExecutionAccess());
  Q4T_CHECK(ExecutionOk(durable) &&
            OutputJson(durable.value) == OutputJson(result.item));
  Q4T_CHECK(!f.Execute(proposed.value, &result).ok() && f.server.calls == 1);
  return true;
}

namespace {
bool RealtimeActualCalls(bool approval_required,
                         bool cancel_unpublished = false) {
  RealtimeNativeFixture f;
  if (!f.Import(approval_required ? "always" : "never")) return false;
  RealtimeConversation history("sess_actual_calls");
  std::vector<Json> events;
  size_t next_id = 0;
  std::atomic<bool> proposal_ready{false};
  Json original_approval;
  RealtimeMcpCallCallbacks callbacks;
  callbacks.new_id = [&](std::string_view prefix) {
    return std::string(prefix) + std::to_string(++next_id);
  };
  callbacks.emit = [&](const Json& event) {
    events.push_back(event);
    return ObjectReply{};
  };
  callbacks.run = [&](const RealtimeMcpCallJob& job, Clock::time_point deadline,
                      const std::function<bool()>& cancelled,
                      RealtimeMcpExecution* result) {
    auto access = ExecutionAccess();
    access.deadline = deadline;
    access.cancelled = cancelled;
    switch (job.action) {
      case RealtimeMcpAction::kPropose: {
        auto reply =
            ProposeRealtimeMcpCall(f.objects, f.config, job.catalog,
                                   job.generated, job.response_id, access);
        if (cancel_unpublished) {
          original_approval = reply.value;
          proposal_ready = true;
          while (!cancelled() && Clock::now() < deadline)
            std::this_thread::sleep_for(1ms);
        }
        return reply;
      }
      case RealtimeMcpAction::kDecide:
        return DecideRealtimeMcpCall(f.objects, f.config,
                                     job.catalog.definition, job.generated,
                                     job.decision, job.response_id, access);
      case RealtimeMcpAction::kExecute:
        return ExecuteRealtimeMcpCall(f.objects, f.config,
                                      job.catalog.definition, job.native_call,
                                      job.response_id, access, result);
      case RealtimeMcpAction::kAbandon:
        return AbandonRealtimeMcpCall(f.objects, f.config,
                                      job.catalog.definition, job.generated,
                                      job.response_id, access);
    }
    return Error(500, "fixture_invalid", "Unknown fixture action");
  };
  RealtimeMcpCalls calls(history, std::move(callbacks));
  RealtimeItemTicket ticket;
  const auto generated = RealtimeGeneratedCall();
  if (!history.BeginOutput(generated, "unused", true, 8192, &ticket) ||
      !ExecutionOk(calls.Begin(ticket, {generated, 0, 0}, f.catalog,
                               "resp_realtime", true)))
    return false;
  bool decided = false;
  bool cancelled = false;
  const auto deadline = Clock::now() + 3s;
  while (calls.Pending() || calls.Workers()) {
    if (cancel_unpublished && proposal_ready && !cancelled) {
      if (!calls.CancelResponse("resp_realtime")) return false;
      cancelled = true;
    }
    if (Clock::now() >= deadline || !ExecutionOk(calls.Tick())) return false;
    if (approval_required && !cancel_unpublished && !decided) {
      Json proposal;
      for (const auto& event : events) {
        const auto* item = event.Find("item");
        if (event.GetString("type") == "conversation.item.done" && item &&
            item->GetString("type") == "mcp_approval_request")
          proposal = *item;
      }
      if (!proposal.IsNull()) {
        if (f.server.calls != 0 ||
            !ExecutionOk(calls.Decide(RealtimeDecision(proposal, true))))
          return false;
        decided = true;
      }
    }
    std::this_thread::sleep_for(1ms);
  }
  ChatPlanError error;
  for (const auto& event : events)
    if (!ValidateRealtimeShape(event, RealtimeShape::kServerEvent, &error))
      return false;
  Json actual;
  if (cancel_unpublished) {
    if (!cancelled || !history.Get(ticket, &actual) ||
        actual.GetString("approval_request_id") !=
            original_approval.GetString("id") ||
        !actual.Find("error") ||
        OutputJson(*actual.Find("error")).find("Tool approval denied") ==
            std::string::npos ||
        f.server.calls != 0)
      return false;
    return std::ranges::none_of(events, [](const auto& event) {
      const auto* item = event.Find("item");
      return item && item->GetString("type") == "mcp_approval_request";
    });
  }
  if (!history.Get(ticket, &actual) ||
      actual.GetString("output").find("document result") == std::string::npos ||
      !actual.Find("error") || !actual.Find("error")->IsNull())
    return false;
  RealtimeConversationSnapshot snapshot;
  return history.Snapshot(nullptr, &snapshot) && f.server.calls == 1 &&
         decided == approval_required && calls.CleanupFailures() == 0;
}
}  // namespace
Q4T_TEST(realtime_mcp_calls_actual_native_http_and_history_completion) {
  Q4T_CHECK(RealtimeActualCalls(false));
  return true;
}
Q4T_TEST(realtime_mcp_calls_actual_native_approval_http_and_sdk_events) {
  Q4T_CHECK(RealtimeActualCalls(true));
  return true;
}
Q4T_TEST(realtime_mcp_calls_actual_native_unpublished_approval_cancel) {
  Q4T_CHECK(RealtimeActualCalls(true, true));
  return true;
}

Q4T_TEST(realtime_mcp_history_authenticates_sdk_result_and_rejects_forgery) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import());
  const auto proposed = f.Propose();
  RealtimeMcpExecution result;
  Q4T_CHECK(ExecutionOk(proposed) &&
            ExecutionOk(f.Execute(proposed.value, &result)));
  RealtimeMcpCallProjection view;
  ChatPlanError error;
  Q4T_CHECK(ProjectRealtimeMcpCall(result.item, &view, &error));
  RealtimeConversationSnapshot snapshot;
  snapshot.items = Array({view.item});
  Json native;
  Q4T_CHECK(ExecutionOk(ResolveRealtimeMcpHistory(
      &f.objects, f.config, snapshot, ExecutionAccess(), &native)));
  Q4T_CHECK(OutputJson(native.array[0]) == OutputJson(result.item));
  Set(&snapshot.items.array[0], "output", String("forged output"));
  const auto old = OutputJson(native);
  Q4T_CHECK(!ResolveRealtimeMcpHistory(&f.objects, f.config, snapshot,
                                       ExecutionAccess(), &native)
                 .ok());
  Q4T_CHECK(OutputJson(native) == old && f.server.calls == 1);
  return true;
}
Q4T_TEST(realtime_mcp_history_denial_errors_and_scope_are_authenticated) {
  RealtimeNativeFixture f;
  Q4T_CHECK(f.Import("always"));
  const auto proposed = f.Propose();
  Q4T_CHECK(ExecutionOk(proposed));
  const auto denied =
      DecideRealtimeMcpCall(f.objects, f.config, f.catalog.definition,
                            RealtimeAwaitingCall(proposed.value),
                            RealtimeDecision(proposed.value, false),
                            "resp_realtime", ExecutionAccess());
  RealtimeMcpCallProjection view;
  ChatPlanError error;
  Q4T_CHECK(ExecutionOk(denied) &&
            ProjectRealtimeMcpCall(denied.value, &view, &error));
  RealtimeConversationSnapshot snapshot;
  snapshot.items = Array(
      {view.item, proposed.value, RealtimeDecision(proposed.value, false)});
  Json native;
  Q4T_CHECK(ExecutionOk(ResolveRealtimeMcpHistory(
      &f.objects, f.config, snapshot, ExecutionAccess(), &native)));
  Q4T_CHECK(OutputJson(native.array[0]) == OutputJson(denied.value));
  auto other = ExecutionAccess();
  other.scope = "client:other";
  Q4T_CHECK(
      !ResolveRealtimeMcpHistory(&f.objects, f.config, snapshot, other, &native)
           .ok());
  Set(&Field(snapshot.items.array[0], "error"), "message",
      String("invented failure"));
  Q4T_CHECK(!ResolveRealtimeMcpHistory(&f.objects, f.config, snapshot,
                                       ExecutionAccess(), &native)
                 .ok());
  Q4T_CHECK(f.server.calls == 0);
  return true;
}

namespace {
bool RealtimeActualSession(bool approval, bool response_only = false) {
  RealtimeNativeFixture f;
  f.config.max_len = 8192;
  WebSocketOutput output;
  RequestRegistry requests;
  std::vector<Json> events;
  std::atomic<size_t> generations{0};
  RealtimeConversationCallbacks callbacks;
  const auto prepare = [&](const Json& settings, const Json& response,
                           const Json& items, const RealtimeMcpContext* mcp,
                           bool preview, RealtimeChatPlan* plan) {
    Json native;
    auto reply = PreviewRealtimeMcpHistory(items, &native);
    if (!ExecutionOk(reply)) return reply;
    ChatPlanError error;
    const auto status =
        PlanRealtimeChat(settings, response, items, f.config, plan, &error,
                         nullptr, false, mcp, preview, &native);
    return status ? ObjectReply{}
                  : Error(error.http_status, error.code, status.message());
  };
  callbacks.validate_prompt = [&](const RealtimePromptSelection& selected) {
    RealtimeChatPlan plan;
    return prepare(selected.settings, Object(), Array(), selected.mcp.get(),
                   true, &plan);
  };
  callbacks.prepare = [&](const RealtimeConversationJob& job) {
    RealtimeChatPlan plan;
    return prepare(job.settings, job.response, job.snapshot.items,
                   job.mcp.get(), job.mcp_definitions_only, &plan);
  };
  callbacks.acquire = [&](const std::string& id) {
    return requests.Register(id, "", Clock::now() + 5s);
  };
  callbacks.release = [&](const auto& control) { requests.Remove(control); };
  callbacks.mcp_import.emplace();
  callbacks.mcp_import->import = [&](const auto& definition, std::string id,
                                     auto deadline, const auto& cancelled,
                                     RealtimeMcpCatalog* catalog) {
    auto access = ExecutionAccess();
    access.deadline = deadline;
    access.cancelled = cancelled;
    return ImportRealtimeMcpCatalog(f.objects, f.config, definition, access,
                                    std::move(id), catalog);
  };
  callbacks.mcp_call.emplace();
  callbacks.mcp_call->run = [&](const RealtimeMcpCallJob& job, auto deadline,
                                const auto& cancelled,
                                RealtimeMcpExecution* result) {
    auto access = ExecutionAccess();
    access.deadline = deadline;
    access.cancelled = cancelled;
    switch (job.action) {
      case RealtimeMcpAction::kPropose:
        return ProposeRealtimeMcpCall(f.objects, f.config, job.catalog,
                                      job.generated, job.response_id, access);
      case RealtimeMcpAction::kDecide:
        return DecideRealtimeMcpCall(f.objects, f.config,
                                     job.catalog.definition, job.generated,
                                     job.decision, job.response_id, access);
      case RealtimeMcpAction::kExecute:
        return ExecuteRealtimeMcpCall(f.objects, f.config,
                                      job.catalog.definition, job.native_call,
                                      job.response_id, access, result);
      case RealtimeMcpAction::kAbandon:
        return AbandonRealtimeMcpCall(f.objects, f.config,
                                      job.catalog.definition, job.generated,
                                      job.response_id, access);
    }
    return Error(500, "fixture_action", "Invalid fixture action");
  };
  callbacks.execute = [&](const RealtimeConversationJob& job,
                          const auto& control, const RealtimeDeltaSink& emit,
                          const RealtimeSpeechSink&) {
    RealtimeGenerationResult result;
    Json native;
    result.reply = ResolveRealtimeMcpHistory(&f.objects, f.config, job.snapshot,
                                             ExecutionAccess(), &native);
    if (!ExecutionOk(result.reply)) return result;
    RealtimeChatPlan plan;
    ChatPlanError error;
    auto status = PlanRealtimeChat(
        job.settings, job.response, job.snapshot.items, f.config, &plan, &error,
        nullptr, false, job.mcp.get(), false, &native);
    if (!status) {
      result.reply = Error(error.http_status, error.code, status.message());
      return result;
    }
    const auto generation = generations++;
    ChatResponseParser parser;
    status = parser.Init(plan.generation.generation_request,
                         "<|im_start|>assistant\n<think>\n\n</think>\n\n",
                         control->id(), true);
    std::vector<SchemaReceipt> receipts;
    std::string text = "document result received";
    if (!generation) {
      if (!job.mcp || job.mcp->bindings.empty()) {
        result.reply = Error(500, "fixture_catalog", "No actual catalog");
        return result;
      }
      if (plan.generation.chat.constraints.schema_receipts) {
        const auto& functions =
            plan.generation.generation_request.GetArray("tools")->array;
        for (const auto& tool : functions) {
          const auto& function = *tool.Find("function");
          if (function.GetString("name") != job.mcp->bindings[0].model_name)
            continue;
          const auto value = Object({{"path", String("docs/MODEL.md")}});
          OutputSchemaGraph schema;
          status = schema.Init(*function.Find("parameters"), false, nullptr,
                               "schema", OutputSchemaPurpose::kToolResult);
          if (status) status = schema.Check(value);
          SchemaReceipt receipt;
          if (status)
            status = MakeSchemaReceipt(function.GetString("name"),
                                       *function.Find("parameters"), value,
                                       &receipt);
          if (status) receipts.push_back(receipt);
        }
        parser.SetSchemaReceipts(&receipts);
      }
      const std::string arguments =
          plan.generation.chat.constraints.active
              ? "<parameter=arguments>{\"path\":\"docs/MODEL.md\"}</parameter>"
              : "<parameter=path>docs/MODEL.md</parameter>";
      text = "<tool_call><function=" + job.mcp->bindings[0].model_name + ">" +
             arguments + "</function></tool_call>";
    } else if (OutputJson(native).find("document result") ==
               std::string::npos) {
      result.reply = Error(500, "fixture_result", "Actual tool result missing");
      return result;
    }
    std::vector<ResponseDelta> deltas;
    if (status) status = parser.Append(text, &deltas);
    if (status) status = parser.Finish(false, &deltas);
    if (!status) {
      result.reply = Error(500, "fixture_parser", status.message());
      return result;
    }
    for (const auto& delta : deltas) {
      result.reply = emit(delta);
      if (!result.reply.ok()) return result;
    }
    q4t::io::ParseJson(parser.MessageJson(), &result.message);
    return result;
  };
  auto settings = DefaultRealtimeChatSettings(f.config, "");
  const auto tools =
      *ExecutionRequest(f.server, approval ? "always" : "never").Find("tools");
  if (!response_only) Set(&settings, "tools", tools);
  RealtimeConversationSession session("sess_native_mcp", "conv_native_mcp",
                                      settings, output, callbacks);
  if (!session.Start()) return false;
  const auto read = [&]() {
    while (!output.Front().empty()) {
      const auto frame = output.Front();
      WebSocketDecoder decoder(false, kWebSocketMessageLimit);
      size_t used = 0;
      if (decoder.Consume(
              {reinterpret_cast<const uint8_t*>(frame.data()), frame.size()},
              &used) != WebSocketRead::kEvent ||
          used != frame.size())
        return false;
      Json event;
      ChatPlanError error;
      if (!q4t::io::ParseJson(std::string(decoder.Event().payload), &event) ||
          !ValidateRealtimeWireShape(event, RealtimeShape::kServerEvent,
                                     &error))
        return false;
      if (event.GetString("type") == "error") return false;
      const auto serialized = OutputJson(event);
      if (serialized.find("mcp-request-secret") != std::string::npos ||
          serialized.find("mcp-header-secret") != std::string::npos)
        return false;
      events.push_back(std::move(event));
      output.Acknowledge();
    }
    return true;
  };
  const auto send = [&](const Json& event) {
    return session.Message(WebSocketOpcode::kText, OutputJson(event)) && read();
  };
  const auto count = [&](std::string_view type) {
    return std::ranges::count_if(
        events, [&](const auto& e) { return e.GetString("type") == type; });
  };
  const auto wait = [&](const std::function<bool()>& done) {
    const auto deadline = Clock::now() + 5s;
    while (Clock::now() < deadline) {
      if (!session.Tick() || !read()) return false;
      if (done()) return true;
      std::this_thread::sleep_for(1ms);
    }
    return false;
  };
  auto create = Object({{"type", String("response.create")}});
  if (response_only) {
    Set(&create, "response",
        Object({{"tools", tools},
                {"tool_choice", Object({{"type", String("mcp")},
                                        {"server_label", String("project")},
                                        {"name", String("read_project")}})}}));
  } else if (!wait([&] { return count("mcp_list_tools.completed") == 1; })) {
    return false;
  }
  if (!send(create)) return false;
  if (approval) {
    Json proposal;
    if (!wait([&] {
          for (const auto& e : events)
            if (e.GetString("type") == "conversation.item.done") {
              const auto* item = e.Find("item");
              if (item && item->GetString("type") == "mcp_approval_request")
                proposal = *item;
            }
          return !proposal.IsNull();
        }) ||
        f.server.calls != 0 || count("response.done") != 1)
      return false;
    if (!send(Object({{"type", String("conversation.item.create")},
                      {"item", RealtimeDecision(proposal, true)}})))
      return false;
  }
  if (!wait([&] {
        return !session.ActiveResponses() && !session.PendingMcp();
      }) ||
      f.server.calls != 1)
    return false;
  if (!send(Object({{"type", String("response.create")},
                    {"response", Object({{"tool_choice", String("none")}})}})))
    return false;
  if (!wait([&] { return count("response.done") == 2; })) return false;
  for (const auto& event : events)
    if (event.GetString("type") == "response.done" &&
        event.Find("response")->GetString("status") != "completed")
      return false;
  session.Cancel();
  session.Join();
  return generations == 2 && f.server.calls == 1;
}
}  // namespace
Q4T_TEST(realtime_session_actual_native_mcp_http_tool_roundtrip) {
  Q4T_CHECK(RealtimeActualSession(false));
  return true;
}
Q4T_TEST(realtime_session_actual_native_mcp_approval_http_roundtrip) {
  Q4T_CHECK(RealtimeActualSession(true));
  return true;
}
Q4T_TEST(realtime_session_actual_native_mcp_response_override_http_roundtrip) {
  Q4T_CHECK(RealtimeActualSession(false, true));
  return true;
}
