#include "q4t/server/mcp_tunnel.h"

#include <sys/socket.h>
#include <unistd.h>

#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/http_request.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
struct Fixture {
  McpTunnelBroker broker;
  BinaryAccess caller{
      "client:owner", std::chrono::steady_clock::now() + 5s, {}};
  BinaryAccess agent{"client:agent", caller.deadline, {}};
  Fixture() {
    if (!broker.Configure({{"tunnel_private",
                            "gen-1",
                            {caller.scope},
                            {agent.scope},
                            {"main", "resources"}}}))
      throw std::runtime_error("tunnel API fixture configuration");
  }
  ObjectReply Api(std::string_view method, std::string_view target,
                  std::string body = "", std::string token = "") {
    return HandleMcpTunnelRequest(broker, agent, method, target, body,
                                  "agent-one", token);
  }
  ObjectReply Submit() {
    Json rpc;
    if (!Parse(
            R"({"jsonrpc":"2.0","method":"tools/list","id":9007199254740993})",
            &rpc))
      throw std::runtime_error("tunnel API fixture request");
    return broker.Submit(caller, "tunnel_private", "gen-1", "main", Object(),
                         rpc);
  }
};
bool Read(const std::string& wire, HttpRequestHeaders* headers,
          std::string* body, int* failure = nullptr) {
  int fd[2];
  if (socketpair(AF_UNIX, SOCK_STREAM, 0, fd)) return false;
  std::thread writer([&] {
    size_t offset = 0;
    while (offset < wire.size()) {
      const auto n =
          send(fd[0], wire.data() + offset, wire.size() - offset, MSG_NOSIGNAL);
      if (n <= 0) break;
      offset += static_cast<size_t>(n);
    }
    shutdown(fd[0], SHUT_WR);
  });
  std::string method, path;
  const auto ok = ReadHttpRequest(fd[1], &method, &path, body, 1000, nullptr,
                                  failure, headers);
  shutdown(fd[1], SHUT_RDWR);
  writer.join();
  close(fd[0]);
  close(fd[1]);
  return ok;
}
}  // namespace

Q4T_TEST(mcp_tunnel_api_exact_routes_classification_and_method_contract) {
  Fixture f;
  Q4T_CHECK(f.Api("GET", "/v1/tunnels/tunnel_private").ok());
  Q4T_CHECK(f.Api("GET", "/v1/tunnels/tunnel_unknown").http_status == 403);
  Q4T_CHECK(
      f.Api("POST", "/v1/tunnels/tunnel_private").value.GetString("allow") ==
      "GET");
  Q4T_CHECK(f.Api("GET", "/v1/tunnels/tunnel_private/response")
                .value.GetString("allow") == "POST");
  Q4T_CHECK(ClassifyHttpConnection("GET", "/v1/tunnels/tunnel_private/poll") ==
            HttpConnectionClass::kTunnelPoll);
  Q4T_CHECK(
      ClassifyHttpConnection("POST", "/v1/tunnels/tunnel_private/response") ==
      HttpConnectionClass::kTunnelResponse);
  Q4T_CHECK(ClassifyHttpConnection("GET", "/v1/tunnels/tunnel_private") ==
            HttpConnectionClass::kObservation);
  for (const auto* path :
       {"/v1/tunnels", "/v1/tunnels//poll", "/v1/tunnels/a/other",
        "/v1/tunnels/a/poll/more", "/v1/tunnels/a#fragment"}) {
    Q4T_CHECK(f.Api("GET", path).http_status == 404);
    Q4T_CHECK(!McpTunnelMethodAllowed("GET", path));
  }
  return true;
}

Q4T_TEST(mcp_tunnel_api_roundtrip_preserves_exact_rpc_id_and_unknown_metadata) {
  Fixture f;
  const auto submitted = f.Submit();
  Q4T_CHECK(submitted.ok());
  const auto polled =
      f.Api("GET",
            "/v1/tunnels/tunnel_private/"
            "poll?limit=1&timeout_ms=0&channel=main&channel=resources");
  Q4T_CHECK(polled.ok());
  const auto* commands = polled.value.GetArray("commands");
  Q4T_CHECK(commands && commands->array.size() == 1);
  const auto command = commands->array[0];
  const auto token = command.GetString("shard_token");
  Json response;
  Q4T_CHECK(Parse(R"({"resp_code":200,"resp_json":{"jsonrpc":"2.0",
    "id":9007199254740993,"result":{"tools":[]}},"future_optional":{"v":1}})",
                  &response));
  Set(&response, "request_id", String(command.GetString("request_id")));
  Q4T_CHECK(f.Api("POST", "/v1/tunnels/tunnel_private/response",
                  OutputJson(response), "wrong")
                .http_status == 403);
  Q4T_CHECK(f.Api("POST", "/v1/tunnels/tunnel_private/response",
                  OutputJson(response), token)
                .ok());
  const auto result =
      f.broker.Wait(f.caller, submitted.value.GetString("request_id"));
  Q4T_CHECK(result.ok() && OutputJson(result.value).find("9007199254740993") !=
                               std::string::npos);
  Q4T_CHECK(f.Api("GET", "/v1/tunnels/tunnel_private/poll?timeout_ms=0")
                .http_status == 204);
  return true;
}

Q4T_TEST(
    mcp_tunnel_api_rejects_ambiguous_queries_and_bad_json_without_dispatch) {
  Fixture f;
  for (const auto* query :
       {"limit=0", "limit=26", "limit=1&limit=2", "limit=x", "timeout_ms=-1",
        "timeout_ms=15001", "timeout_ms=1&timeout_ms=2",
        "channel=main&channel=main", "unknown=1", "channel=%FF",
        "channel=main&", "timeout_ms=%31%2E%30"})
    Q4T_CHECK(
        f.Api("GET", std::string("/v1/tunnels/tunnel_private/poll?") + query)
            .http_status == 400);
  Q4T_CHECK(f.Api("GET", "/v1/tunnels/tunnel_private?limit=1").http_status ==
            400);
  Q4T_CHECK(f.Api("GET", "/v1/tunnels/tunnel_private/poll?timeout_ms=0", "{}")
                .http_status == 400);
  Q4T_CHECK(f.Submit().ok());
  auto poll = f.Api("GET", "/v1/tunnels/tunnel_private/poll?timeout_ms=0");
  Q4T_CHECK(poll.ok());
  const auto command = poll.value.GetArray("commands")->array[0];
  for (const auto* raw :
       {"[]", "{} trailing", R"({"request_id":"one","request_id":"two"})",
        R"({"bad":"\ud800"})"})
    Q4T_CHECK(f.Api("POST", "/v1/tunnels/tunnel_private/response", raw,
                    command.GetString("shard_token"))
                  .http_status == 400);
  Q4T_CHECK(f.broker.Health().GetInt("inflight") == 1);
  return true;
}

Q4T_TEST(mcp_tunnel_http_protected_headers_and_payload_limit) {
  const auto prefix =
      "POST /v1/tunnels/tunnel_private/response HTTP/1.1\r\nHost: "
      "localhost\r\n";
  const auto auth =
      "X-Tunnel-Client-Instance-Id: agent-one\r\nX-Tunnel-Shard-Token: "
      "original_token\r\n";
  HttpRequestHeaders headers;
  std::string body;
  const std::string payload(8192, 'x');
  Q4T_CHECK(Read(
      std::string(prefix) + auth + "Content-Length: 8192\r\n\r\n" + payload,
      &headers, &body));
  Q4T_CHECK(body == payload && headers.tunnel_instance == "agent-one" &&
            headers.tunnel_shard_token == "original_token");
  for (const auto* bad : {"X-Tunnel-Shard-Token: duplicate\r\n",
                          "X-Tunnel-Client-Instance-Id: other\r\n",
                          "Connection: X-Tunnel-Shard-Token\r\n",
                          "Connection: X-Tunnel-Client-Instance-Id\r\n"}) {
    headers = {};
    Q4T_CHECK(
        !Read(std::string(prefix) + auth + bad + "Content-Length: 0\r\n\r\n",
              &headers, &body));
  }
  headers = {};
  int failure = 400;
  Q4T_CHECK(
      !Read(std::string(prefix) + auth + "Content-Length: 4194305\r\n\r\n",
            &headers, &body, &failure));
  Q4T_CHECK(failure == 413);
  headers = {};
  Q4T_CHECK(!Read(
      std::string(prefix) +
          "Transfer-Encoding: chunked\r\nTrailer: "
          "X-Tunnel-Shard-Token\r\n\r\n0\r\nX-Tunnel-Shard-Token: late\r\n\r\n",
      &headers, &body));
  return true;
}
