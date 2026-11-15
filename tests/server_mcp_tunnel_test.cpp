#include "q4t/server/mcp_tunnel.h"

#include <atomic>
#include <barrier>
#include <future>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
const Json& At(const Json& value, std::string_view name) {
  for (const auto& [key, field] : value.object)
    if (key == name) return field;
  throw std::runtime_error("Missing tunnel fixture field");
}
Json& At(Json& value, std::string_view name) {
  for (auto& [key, field] : value.object)
    if (key == name) return field;
  throw std::runtime_error("Missing tunnel fixture field");
}
BinaryAccess Caller(std::string scope = "client:owner") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
BinaryAccess Agent() { return Caller("client:agent"); }
McpTunnelRoute Route() {
  return {"tunnel_private",
          "deployment-1",
          {"client:owner", "client:other"},
          {"client:agent", "client:backup"},
          {"main", "resources"}};
}
Json Rpc() {
  return Object({{"jsonrpc", String("2.0")},
                 {"id", String("rpc_original")},
                 {"method", String("tools/call")},
                 {"params", Object({{"name", String("read_project")},
                                    {"arguments", Object()}})}});
}
ObjectReply Submit(McpTunnelBroker& broker,
                   const BinaryAccess& caller = Caller(),
                   std::string channel = "main", Json rpc = Rpc()) {
  return broker.Submit(
      caller, "tunnel_private", "deployment-1", channel,
      Object({{"Authorization", Array({String("Bearer private")})}}), rpc);
}
ObjectReply Poll(McpTunnelBroker& broker, std::string instance = "instance-one",
                 int wait_ms = 0) {
  return broker.Poll(Agent(), "tunnel_private", instance, {}, 25, wait_ms);
}
Json Command(const ObjectReply& polled) {
  const auto* commands = polled.value.Find("commands");
  if (!polled.ok() || !commands || !commands->IsArray() ||
      commands->array.empty())
    throw std::runtime_error("tunnel command fixture unavailable");
  return commands->array.front();
}
Json Response(const Json& command, Json rpc = Json()) {
  if (rpc.IsNull())
    rpc = Object({{"jsonrpc", String("2.0")},
                  {"id", At(command, "jsonrpc").Find("id")
                             ? At(At(command, "jsonrpc"), "id")
                             : Json()},
                  {"result", Object({{"content", Array()}})}});
  return Object({{"request_id", At(command, "request_id")},
                 {"channel", At(command, "channel")},
                 {"resp_code", Number(200)},
                 {"resp_type", String("jsonrpc_response")},
                 {"resp_json", std::move(rpc)}});
}
ObjectReply Respond(McpTunnelBroker& broker, const Json& command,
                    const Json& response,
                    std::string instance = "instance-one") {
  return broker.Respond(Agent(), "tunnel_private", instance,
                        command.GetString("shard_token"), response);
}
}  // namespace

Q4T_TEST(mcp_tunnel_concurrent_handoff_is_once_and_fifo_with_channels) {
  McpTunnelBroker broker;
  Q4T_CHECK(broker.Configure({Route()}));
  const auto first = Submit(broker), second = Submit(broker),
             third = Submit(broker, Caller(), "resources");
  Q4T_CHECK(first.ok() && second.ok() && third.ok());
  std::barrier start(3);
  auto run = [&] {
    start.arrive_and_wait();
    return broker.Poll(Agent(), "tunnel_private", "instance-one", {"main"}, 25,
                       0);
  };
  auto left = std::async(std::launch::async, run);
  auto right = std::async(std::launch::async, run);
  start.arrive_and_wait();
  auto a = left.get(), b = right.get();
  Q4T_CHECK(a.ok() && b.ok());
  Q4T_CHECK((a.http_status == 200) != (b.http_status == 200));
  const auto& commands =
      At(a.http_status == 200 ? a.value : b.value, "commands");
  Q4T_CHECK(commands.array.size() == 2);
  Q4T_CHECK(commands.array[0].GetString("request_id") ==
            first.value.GetString("request_id"));
  Q4T_CHECK(commands.array[1].GetString("request_id") ==
            second.value.GetString("request_id"));
  const auto extra = Command(Poll(broker));
  Q4T_CHECK(extra.GetString("channel") == "resources" &&
            extra.GetString("request_id") ==
                third.value.GetString("request_id"));
  Q4T_CHECK(Poll(broker).http_status == 204);
  Q4T_CHECK(broker.Health().GetInt("inflight") == 3);
  Q4T_CHECK(commands.array[0].GetString("created_at").ends_with('Z'));
  Q4T_CHECK(commands.array[0].GetString("response_timeout").ends_with("ms"));
  return true;
}

Q4T_TEST(mcp_tunnel_original_identity_token_and_scope_control_completion) {
  McpTunnelBroker broker;
  Q4T_CHECK(broker.Configure({Route()}));
  const auto submitted = Submit(broker);
  Q4T_CHECK(submitted.ok());
  auto command = Command(Poll(broker));
  auto response = Response(command);
  Q4T_CHECK(broker
                .Respond(Caller("client:other"), "tunnel_private",
                         "instance-one", command.GetString("shard_token"),
                         response)
                .http_status == 403);
  Q4T_CHECK(broker
                .Respond(Agent(), "tunnel_other", "instance-one",
                         command.GetString("shard_token"), response)
                .http_status == 403);
  Q4T_CHECK(
      broker
          .Respond(Agent(), "tunnel_private", "instance-one", "wrong", response)
          .http_status == 403);
  Q4T_CHECK(Respond(broker, command, response, "instance-two").http_status ==
            403);
  Q4T_CHECK(
      broker
          .Wait(Caller("client:other"), submitted.value.GetString("request_id"))
          .http_status == 403);
  Q4T_CHECK(Respond(broker, command, response).ok());
  Q4T_CHECK(Respond(broker, command, response).ok());
  Set(&At(response, "resp_json"), "result",
      Object({{"changed", Boolean(true)}}));
  Q4T_CHECK(Respond(broker, command, response).http_status == 409);
  const auto result =
      broker.Wait(Caller(), submitted.value.GetString("request_id"));
  Q4T_CHECK(result.ok() && result.value.GetBool("dispatched") &&
            result.value.GetString("agent_instance") == "instance-one");
  Q4T_CHECK(!At(At(result.value, "resp_json"), "result").Has("changed"));
  Q4T_CHECK(broker.Health().GetInt("retained_bytes") == 0);
  Q4T_CHECK(Respond(broker, command, Response(command)).http_status == 404);
  return true;
}

Q4T_TEST(mcp_tunnel_preserves_native_error_and_filters_response_headers) {
  McpTunnelBroker broker;
  Q4T_CHECK(broker.Configure({Route()}));
  Q4T_CHECK(Submit(broker).ok());
  auto command = Command(Poll(broker));
  auto rpc = Object(
      {{"jsonrpc", String("2.0")},
       {"id", String("rpc_original")},
       {"error",
        Object({{"code", Number(-32004)},
                {"message", String("版本错误")},
                {"data", Object({{"versions", Array({String("v1")})}})}})}});
  auto response = Response(command, rpc);
  Set(&response, "resp_code", Number(401));
  Set(&response, "resp_timing", String("optional future metadata"));
  Set(&response, "resp_headers",
      Object(
          {{"Content-Type", Array({String("application/json")})},
           {"WWW-Authenticate", Array({String("Bearer realm=\"private\"\t")})},
           {"X-Request-Id", Array({String("upstream-original")})},
           {"Mcp-Session-Id", Array({String("do-not-forward")})},
           {"Connection", Array({String("Mcp-Session-Id")})},
           {"Set-Cookie", Array({String("secret\r\nignored")})}}));
  Q4T_CHECK(Respond(broker, command, response).ok());
  const auto result = broker.Wait(Caller(), command.GetString("request_id"));
  Q4T_CHECK(result.ok() && result.value.GetInt("resp_code") == 401);
  Q4T_CHECK(OutputJson(At(result.value, "resp_json")) == OutputJson(rpc));
  const auto& headers = At(result.value, "resp_headers");
  Q4T_CHECK(headers.Has("www-authenticate") && headers.Has("content-type") &&
            headers.Has("x-tunnel-mcp-request-id") &&
            !headers.Has("mcp-session-id"));
  Q4T_CHECK(!headers.Has("connection") && !headers.Has("set-cookie"));
  return true;
}

Q4T_TEST(mcp_tunnel_progress_does_not_finish_or_extend_deadline) {
  McpTunnelBroker broker;
  McpTunnelLimits limits;
  limits.request_ms = 500;
  Q4T_CHECK(broker.Configure({Route()}, limits));
  Q4T_CHECK(Submit(broker).ok());
  auto command = Command(Poll(broker));
  auto response = Response(
      command, Object({{"jsonrpc", String("2.0")},
                       {"method", String("notifications/progress")},
                       {"params", Object({{"progress", Number(1)}})}}));
  Set(&response, "resp_type", String("jsonrpc_notify"));
  Q4T_CHECK(Respond(broker, command, response).ok());
  auto invalid = response;
  Set(&invalid, "resp_code", Number(999));
  Q4T_CHECK(Respond(broker, command, invalid).http_status == 400);
  Q4T_CHECK(broker.Health().GetInt("inflight") == 1);
  for (int i = 0; i < 63; ++i)
    Q4T_CHECK(Respond(broker, command, response).ok());
  Q4T_CHECK(Respond(broker, command, response).http_status == 429);
  auto result = broker.Wait(Caller(), command.GetString("request_id"));
  Q4T_CHECK(result.code == "request_deadline_exceeded" &&
            result.value.GetBool("dispatched"));
  Q4T_CHECK(broker.Health().GetInt("retained_bytes") == 0 &&
            Poll(broker).http_status == 204);
  return true;
}

Q4T_TEST(
    mcp_tunnel_invalid_terminal_completes_without_overwriting_known_result) {
  for (int mode = 0; mode < 5; ++mode) {
    McpTunnelBroker broker;
    Q4T_CHECK(broker.Configure({Route()}));
    Q4T_CHECK(Submit(broker).ok());
    auto command = Command(Poll(broker));
    auto response = Response(command);
    if (mode == 0) Set(&response, "resp_code", Number(600));
    if (mode == 1)
      Set(&At(response, "resp_json"), "id", String("not-original"));
    if (mode == 2)
      Set(&response, "resp_headers",
          Object({{"Mcp-Session-Id", Array({String("wide-😀")})}}));
    if (mode == 3) Set(&response, "resp_type", String("notify_ack"));
    if (mode == 4) Set(&response, "shard_token", At(command, "shard_token"));
    Q4T_CHECK(Respond(broker, command, response).http_status == 400);
    Q4T_CHECK(Respond(broker, command, Response(command)).http_status == 409);
    const auto result = broker.Wait(Caller(), command.GetString("request_id"));
    Q4T_CHECK(result.ok() && result.value.GetInt("resp_code") == 502 &&
              At(At(result.value, "resp_json"), "error").GetInt("code") ==
                  -32603);
  }
  return true;
}

Q4T_TEST(mcp_tunnel_notifications_termination_and_status_normalization) {
  for (const auto& [wire, normalized] :
       {std::pair{101, 502}, {299, 200}, {499, 502}, {429, 429}}) {
    McpTunnelBroker broker;
    Q4T_CHECK(broker.Configure({Route()}));
    Q4T_CHECK(Submit(broker).ok());
    auto command = Command(Poll(broker));
    auto response = Response(command);
    Set(&response, "resp_code", Number(wire));
    Q4T_CHECK(Respond(broker, command, response).ok());
    Q4T_CHECK(broker.Wait(Caller(), command.GetString("request_id"))
                  .value.GetInt("resp_code") == normalized);
  }
  for (bool termination : {false, true}) {
    McpTunnelBroker broker;
    Q4T_CHECK(broker.Configure({Route()}));
    auto rpc = Object({{"jsonrpc", String("2.0")},
                       {"method", String("notifications/initialized")}});
    const auto submitted =
        broker.Submit(Caller(), "tunnel_private", "deployment-1", "main",
                      Object(), termination ? Json() : rpc, termination);
    Q4T_CHECK(submitted.ok());
    auto command = Command(Poll(broker));
    auto response = Object(
        {{"request_id", At(command, "request_id")},
         {"resp_code", Number(termination ? 204 : 202)},
         {"resp_type", String(termination ? "session_termination_response"
                                          : "notify_ack")}});
    Q4T_CHECK(Respond(broker, command, response).ok());
    Q4T_CHECK(broker.Wait(Caller(), command.GetString("request_id")).ok());
  }
  return true;
}

Q4T_TEST(mcp_tunnel_cancel_stop_wake_and_release_without_redelivery) {
  for (bool handoff : {false, true}) {
    McpTunnelBroker broker;
    Q4T_CHECK(broker.Configure({Route()}));
    std::atomic<bool> cancelled{false}, entered{false};
    auto caller = Caller();
    caller.cancelled = [&] {
      broker
          .Health();  // A callback must not deadlock by re-entering the broker.
      entered = true;
      return cancelled.load();
    };
    auto submitted = Submit(broker, caller);
    Q4T_CHECK(submitted.ok());
    if (handoff) Q4T_CHECK(Poll(broker).http_status == 200);
    entered = false;
    auto waiter = std::async(std::launch::async, [&] {
      return broker.Wait(caller, submitted.value.GetString("request_id"));
    });
    const auto deadline = Clock::now() + 1s;
    while (!entered && Clock::now() < deadline) std::this_thread::yield();
    Q4T_CHECK(entered);
    if (handoff)
      broker.Stop();
    else
      cancelled = true;
    Q4T_CHECK(waiter.wait_for(1s) == std::future_status::ready);
    const auto result = waiter.get();
    Q4T_CHECK(result.code ==
              (handoff ? "mcp_tunnel_stopped" : "request_cancelled"));
    Q4T_CHECK(result.value.GetBool("dispatched") == handoff);
    Q4T_CHECK(broker.Health().GetInt("retained_bytes") == 0);
    if (!handoff) Q4T_CHECK(Poll(broker).http_status == 204);
  }
  return true;
}

Q4T_TEST(mcp_tunnel_queue_and_response_capacity_release_for_other_clients) {
  McpTunnelBroker broker;
  McpTunnelLimits limits;
  limits.requests = 2;
  limits.per_client = 1;
  limits.retained_bytes = 12000;
  Q4T_CHECK(broker.Configure({Route()}, limits));
  auto first = Submit(broker);
  Q4T_CHECK(first.ok() && Submit(broker).code == "mcp_tunnel_capacity");
  Q4T_CHECK(Submit(broker, Caller("client:other")).ok());
  auto polled = Poll(broker);
  auto command = Command(polled);
  auto response = Response(command);
  Set(&At(response, "resp_json"), "result",
      Object({{"large", String(std::string(5000, 'x'))}}));
  Q4T_CHECK(Respond(broker, command, response).code == "mcp_tunnel_capacity");
  Q4T_CHECK(broker.Wait(Caller(), first.value.GetString("request_id")).code ==
            "mcp_tunnel_capacity");
  Q4T_CHECK(Submit(broker).ok());
  broker.Stop();
  Q4T_CHECK(broker.Health().GetInt("retained_bytes") == 0);
  Q4T_CHECK(Submit(broker).code == "mcp_tunnel_stopped");
  return true;
}

Q4T_TEST(mcp_tunnel_agent_lease_failover_keeps_original_session_and_no_replay) {
  McpTunnelBroker broker;
  McpTunnelLimits limits;
  limits.agent_lease_ms = 200;
  Q4T_CHECK(broker.Configure({Route()}, limits));
  Q4T_CHECK(Submit(broker).ok());
  const auto original = Command(Poll(broker));
  Q4T_CHECK(Poll(broker, "instance-two").code == "mcp_tunnel_agent_busy");
  auto pinned = broker.Submit(Caller(), "tunnel_private", "deployment-1",
                              "main", Object(), Rpc(), false, "instance-one");
  Q4T_CHECK(pinned.ok());
  std::this_thread::sleep_for(250ms);
  Q4T_CHECK(Poll(broker, "instance-two").http_status == 204);
  const auto failed =
      broker.Wait(Caller(), pinned.value.GetString("request_id"));
  Q4T_CHECK(failed.code == "mcp_tunnel_session_lost" &&
            !failed.value.GetBool("dispatched"));
  Q4T_CHECK(Respond(broker, original, Response(original)).ok());
  Q4T_CHECK(broker.Wait(Caller(), original.GetString("request_id")).ok());
  Q4T_CHECK(Submit(broker).ok());
  const auto next = Command(Poll(broker, "instance-two"));
  Q4T_CHECK(next.GetString("request_id") != original.GetString("request_id"));
  Q4T_CHECK(Respond(broker, next, Response(next), "instance-two").ok());
  Q4T_CHECK(broker.Wait(Caller(), next.GetString("request_id")).ok());
  return true;
}

Q4T_TEST(mcp_tunnel_configuration_authority_and_health_are_bounded_redacted) {
  McpTunnelBroker broker;
  auto invalid = Route();
  invalid.agents = {"*"};
  Q4T_CHECK(!broker.Configure({invalid}));
  Q4T_CHECK(broker.Configure({Route()}));
  Q4T_CHECK(!broker.Configure({Route()}));
  Q4T_CHECK(Submit(broker, Caller("client:unknown")).http_status == 403);
  Q4T_CHECK(broker
                .Submit(Caller(), "tunnel_private", "other-generation", "main",
                        Object(), Rpc())
                .http_status == 403);
  Q4T_CHECK(broker.Poll(Caller(), "tunnel_private", "instance-one", {}, 1, 0)
                .http_status == 403);
  Q4T_CHECK(
      broker.Poll(Agent(), "tunnel_private", "instance-one", {"invalid"}, 1, 0)
          .http_status == 403);
  Q4T_CHECK(broker.Describe(Agent(), "tunnel_private").ok());
  Q4T_CHECK(
      broker.Describe(Caller("client:unknown"), "tunnel_private").http_status ==
      403);
  Q4T_CHECK(Submit(broker).ok());
  const auto health = OutputJson(broker.Health());
  for (const auto* hidden : {"private", "deployment", "client:", "instance",
                             "read_project", "Bearer"})
    Q4T_CHECK(health.find(hidden) == health.npos);
  auto large = Rpc();
  Set(&large, "params",
      Object({{"large", String(std::string(1U << 20, 'x'))}}));
  Q4T_CHECK(Submit(broker, Caller(), "main", large).http_status == 413);
  return true;
}

Q4T_TEST(mcp_tunnel_final_keeps_progress_and_unicode_header_contract) {
  McpTunnelBroker broker;
  Q4T_CHECK(broker.Configure({Route()}));
  Q4T_CHECK(Submit(broker).ok());
  auto command = Command(Poll(broker));
  const auto notification =
      Object({{"jsonrpc", String("2.0")},
              {"method", String("notifications/progress")},
              {"params", Object({{"message", String("处理中")}})}});
  auto progress = Response(command, notification);
  Set(&progress, "resp_type", String("jsonrpc_notify"));
  Q4T_CHECK(Respond(broker, command, progress).ok());
  auto response = Response(command);
  Set(&response, "resp_headers",
      Object({{"WWW-Authenticate", Array({String("café\t")})}}));
  Q4T_CHECK(Respond(broker, command, response).ok());
  const auto result = broker.Wait(Caller(), command.GetString("request_id"));
  Q4T_CHECK(result.ok());
  const auto& notifications = At(result.value, "notifications");
  Q4T_CHECK(notifications.array.size() == 1 &&
            OutputJson(At(notifications.array[0], "resp_json")) ==
                OutputJson(notification));
  Q4T_CHECK(
      At(At(result.value, "resp_headers"), "www-authenticate").array[0].str ==
      "café\t");
  return true;
}

Q4T_TEST(mcp_tunnel_expired_queue_releases_capacity_and_never_hands_off) {
  McpTunnelBroker broker;
  McpTunnelLimits limits;
  limits.request_ms = 10;
  limits.requests = limits.per_client = 1;
  Q4T_CHECK(broker.Configure({Route()}, limits));
  const auto pending = Submit(broker);
  Q4T_CHECK(pending.ok());
  auto waiter = broker.Wait(Caller(), pending.value.GetString("request_id"));
  Q4T_CHECK(waiter.code == "request_deadline_exceeded" &&
            !waiter.value.GetBool("dispatched"));
  Q4T_CHECK(Poll(broker).http_status == 204);
  Q4T_CHECK(Submit(broker).ok());
  std::this_thread::sleep_for(20ms);
  Q4T_CHECK(broker.Health().GetInt("retained_bytes") == 0);
  Q4T_CHECK(Poll(broker).http_status == 204);
  auto expired = Caller();
  expired.deadline = Clock::now();
  Q4T_CHECK(Submit(broker, expired).code == "request_deadline_exceeded");
  Q4T_CHECK(broker.Poll(expired, "tunnel_private", "instance-one").code ==
            "request_deadline_exceeded");
  return true;
}

Q4T_TEST(mcp_tunnel_long_poll_wakes_on_enqueue_and_stops_without_request) {
  for (bool stop : {false, true}) {
    McpTunnelBroker broker;
    Q4T_CHECK(broker.Configure({Route()}));
    std::atomic<bool> entered{false};
    auto agent = Agent();
    agent.cancelled = [&] {
      entered = true;
      return false;
    };
    auto polled = std::async(std::launch::async, [&] {
      return broker.Poll(agent, "tunnel_private", "instance-one", {}, 1, 5000);
    });
    const auto deadline = Clock::now() + 1s;
    while (!entered && Clock::now() < deadline) std::this_thread::yield();
    Q4T_CHECK(entered);
    if (stop)
      broker.Stop();
    else
      Q4T_CHECK(Submit(broker).ok());
    Q4T_CHECK(polled.wait_for(1s) == std::future_status::ready);
    auto result = polled.get();
    Q4T_CHECK(stop ? result.code == "mcp_tunnel_stopped"
                   : result.http_status == 200);
  }
  return true;
}

Q4T_TEST(mcp_tunnel_single_completion_owner_and_invalid_pre_handoff_reply) {
  McpTunnelBroker broker;
  Q4T_CHECK(broker.Configure({Route()}));
  const auto submitted = Submit(broker);
  Q4T_CHECK(submitted.ok());
  auto fake = Object({{"request_id", At(submitted.value, "request_id")},
                      {"resp_code", Number(502)}});
  Q4T_CHECK(
      broker
          .Respond(Agent(), "tunnel_private", "instance-one", "unissued", fake)
          .http_status == 403);
  auto command = Command(Poll(broker));
  std::atomic<bool> entered{false};
  auto caller = Caller();
  caller.cancelled = [&] {
    entered = true;
    return false;
  };
  auto waiter = std::async(std::launch::async, [&] {
    return broker.Wait(caller, command.GetString("request_id"));
  });
  const auto deadline = Clock::now() + 1s;
  while (!entered && Clock::now() < deadline) std::this_thread::yield();
  Q4T_CHECK(entered);
  Q4T_CHECK(broker.Wait(Caller(), command.GetString("request_id")).code ==
            "mcp_tunnel_waiter_busy");
  Q4T_CHECK(Respond(broker, command, Response(command)).ok());
  Q4T_CHECK(waiter.wait_for(1s) == std::future_status::ready &&
            waiter.get().ok());
  return true;
}

Q4T_TEST(mcp_tunnel_poll_envelope_and_large_rpc_error_stay_within_budgets) {
  McpTunnelBroker broker;
  Q4T_CHECK(broker.Configure({Route()}));
  auto rpc = Rpc();
  Set(&rpc, "params", Object({{"large", String(std::string(950000, 'x'))}}));
  for (int i = 0; i < 10; ++i)
    Q4T_CHECK(Submit(broker, Caller(), "main", rpc).ok());
  const auto batch = Poll(broker);
  Q4T_CHECK(batch.ok() && OutputJson(batch.value).size() <= (8U << 20));
  Q4T_CHECK(At(batch.value, "commands").array.size() == 8);
  Q4T_CHECK(broker.Health().GetInt("pending") == 2);
  McpTunnelBroker small;
  McpTunnelLimits limits;
  limits.retained_bytes = 32768;
  Q4T_CHECK(small.Configure({Route()}, limits));
  rpc = Rpc();
  Set(&rpc, "id", String(std::string(8000, 'i')));
  Q4T_CHECK(Submit(small, Caller(), "main", rpc).ok());
  const auto command = Command(Poll(small));
  auto bad = Response(command);
  Set(&bad, "resp_code", Number(600));
  Q4T_CHECK(Respond(small, command, bad).http_status == 400);
  Q4T_CHECK(small.Health().GetInt("retained_bytes") <= 32768);
  auto result = small.Wait(Caller(), command.GetString("request_id"));
  Q4T_CHECK(result.ok() &&
            At(result.value, "resp_json").GetString("id").size() == 8000);
  Q4T_CHECK(small.Health().GetInt("retained_bytes") == 0);
  return true;
}

Q4T_TEST(mcp_tunnel_known_result_survives_collection_and_stop_for_its_waiter) {
  for (bool stop : {false, true}) {
    McpTunnelBroker broker;
    Q4T_CHECK(broker.Configure({Route()}));
    const auto submitted = Submit(broker);
    Q4T_CHECK(submitted.ok());
    auto command = Command(Poll(broker));
    std::atomic<bool> entered{false}, release{false};
    auto caller = Caller();
    caller.cancelled = [&] {
      entered = true;
      const auto end = Clock::now() + 1s;
      while (!release && Clock::now() < end) std::this_thread::yield();
      return true;
    };
    auto waiter = std::async(std::launch::async, [&] {
      return broker.Wait(caller, command.GetString("request_id"));
    });
    const auto end = Clock::now() + 1s;
    while (!entered && Clock::now() < end) std::this_thread::yield();
    Q4T_CHECK(entered);
    Q4T_CHECK(Respond(broker, command, Response(command)).ok());
    if (stop)
      broker.Stop();
    else
      broker.Health();
    release = true;
    Q4T_CHECK(waiter.wait_for(1s) == std::future_status::ready);
    Q4T_CHECK(waiter.get().ok());
    Q4T_CHECK(broker.Health().GetInt("retained_bytes") == 0);
  }
  return true;
}
