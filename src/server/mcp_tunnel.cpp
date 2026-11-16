#include "q4t/server/mcp_tunnel.h"

#include <algorithm>
#include <condition_variable>
#include <ctime>
#include <map>
#include <mutex>
#include <set>

#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
constexpr uint64_t kRequestBytes = 1U << 20;
constexpr uint64_t kResponseBytes = 4U << 20;
constexpr uint64_t kPollBytes = 8U << 20;
constexpr uint64_t kProgressBytes = 1U << 20;
// Reserve terminal failure space and bounded identity/owner metadata per task.
constexpr uint64_t kEnvelopeBytes = 4096;
std::string CreatedAt() {
  const auto now = std::time(nullptr);
  std::tm utc{};
  char text[32]{};
  if (!gmtime_r(&now, &utc) ||
      !std::strftime(text, sizeof(text), "%Y-%m-%dT%H:%M:%SZ", &utc))
    return {};
  return text;
}
bool Text(std::string_view value, size_t maximum = 256) {
  return !value.empty() && value.size() <= maximum &&
         std::all_of(value.begin(), value.end(),
                     [](unsigned char c) { return c >= 0x21 && c <= 0x7e; });
}
bool Contains(const std::vector<std::string>& values, std::string_view value) {
  return std::find(values.begin(), values.end(), value) != values.end();
}
bool Names(const std::vector<std::string>& values, size_t limit) {
  if (values.empty() || values.size() > limit) return false;
  std::set<std::string> seen;
  for (const auto& value : values)
    if (!Text(value, 512) || value == "*" || !seen.insert(value).second)
      return false;
  return true;
}
ObjectReply Denied() {
  return Error(403, "mcp_tunnel_denied", "Tunnel authority does not match");
}
ObjectReply Expired(bool cancelled = false) {
  return Error(
      cancelled ? 499 : 408,
      cancelled ? "request_cancelled" : "request_deadline_exceeded",
      cancelled ? "Tunnel operation cancelled" : "Tunnel operation expired");
}
ObjectReply Active(const BinaryAccess& access) {
  if (access.scope.empty()) return Denied();
  if (access.cancelled && access.cancelled()) return Expired(true);
  if (Clock::now() >= access.deadline) return Expired();
  return {};
}
bool SecretEqual(std::string_view left, std::string_view right) {
  if (left.size() != right.size()) return false;
  unsigned difference = 0;
  for (size_t i = 0; i < left.size(); ++i)
    difference |= static_cast<unsigned char>(left[i]) ^
                  static_cast<unsigned char>(right[i]);
  return difference == 0;
}
// JSON represents headers as Unicode strings. HTTP field values permit only
// HTAB, printable ASCII and Latin-1; reject controls and larger codepoints.
bool HeaderValue(std::string_view value) {
  for (size_t i = 0; i < value.size(); ++i) {
    const auto c = static_cast<unsigned char>(value[i]);
    if (c == '\t' || (c >= 0x20 && c <= 0x7e)) continue;
    if ((c != 0xc2 && c != 0xc3) || ++i == value.size()) return false;
    const auto tail = static_cast<unsigned char>(value[i]);
    if (tail < 0x80 || tail > 0xbf) return false;
  }
  return true;
}
bool Headers(const Json& headers, bool response, Json* filtered) {
  *filtered = Object();
  if (!headers.IsObject() || headers.object.size() > 64 ||
      OutputJson(headers).size() > 16384)
    return false;
  std::set<std::string> connection;
  for (const auto& [key, values] : headers.object) {
    if (http::Lower(key) != "connection") continue;
    if (!values.IsArray()) return false;
    for (const auto& value : values.array) {
      std::vector<std::string> tokens;
      if (!value.IsString() || !http::Tokens(value.str, &tokens)) return false;
      for (auto& token : tokens) connection.insert(http::Lower(token));
    }
  }
  for (const auto& [key, values] : headers.object) {
    if (key.empty() || key.size() > 128 ||
        !std::all_of(key.begin(), key.end(), http::IsToken))
      return false;
    auto name = http::Lower(key);
    if (connection.contains(name) || name == "connection" ||
        name == "transfer-encoding" || name == "content-length" ||
        name == "host" || name == "upgrade" || name == "trailer" ||
        name == "proxy-authorization" || name == "proxy-authenticate" ||
        name == "keep-alive" || name == "te" || name == "proxy-connection")
      continue;
    if (response && name == "x-request-id") name = "x-tunnel-mcp-request-id";
    if (response && name != "content-type" && name != "mcp-session-id" &&
        name != "mcp-protocol-version" && name != "last-event-id" &&
        name != "access-control-expose-headers" && name != "www-authenticate" &&
        name != "x-tunnel-mcp-request-id")
      continue;
    if (!values.IsArray() || values.array.size() > 16) return false;
    auto kept = Array();
    for (const auto& value : values.array) {
      if (!value.IsString() || !HeaderValue(value.str)) return false;
      if (!value.str.empty()) kept.array.push_back(value);
    }
    if (!kept.array.empty()) {
      if (filtered->Has(name)) return false;
      Set(filtered, name, std::move(kept));
    }
  }
  return true;
}
bool RpcRequest(const Json& rpc) {
  if (!rpc.IsObject() || rpc.GetString("jsonrpc") != "2.0" ||
      !Text(rpc.GetString("method")))
    return false;
  const auto* id = rpc.Find("id");
  return !id || id->IsString() || (id->IsNumber() && IsInputInteger(*id));
}
Json GatewayError(const Json& id) {
  return Object(
      {{"jsonrpc", String("2.0")},
       {"id", id},
       {"error", Object({{"code", Number(-32603)},
                         {"message", String("Invalid tunnel response")}})}});
}
}  // namespace

struct McpTunnelBroker::Impl {
  struct Route {
    McpTunnelRoute config;
    std::string agent_scope, instance;
    Clock::time_point lease{};
  };
  struct Request {
    std::string id, token, tunnel, generation, channel, caller, agent, instance;
    std::string expected_instance;
    Json command, rpc_id, notifications = Array();
    Clock::time_point deadline;
    uint64_t sequence = 0, accounted = 0, command_bytes = 0, progress_bytes = 0;
    bool polled = false, done = false, claimed = false;
    bool has_rpc_id = false, termination = false, detached_cleanup = false;
    ObjectReply result;
    std::string final_identity;
  };
  mutable std::mutex mutex;
  std::condition_variable changed;
  McpTunnelLimits limits;
  std::map<std::string, Route> routes;
  std::map<std::string, std::shared_ptr<Request>> requests;
  uint64_t retained = 0, sequence = 0;
  bool configured = false, stopped = false;

  void Release(Request& request) {
    retained -= request.accounted;
    request.accounted = 0;
  }
  void Fail(Request& request, ObjectReply failure) {
    request.command = Json();
    request.notifications = Array();
    request.done = true;
    failure.value = Object({{"dispatched", Boolean(request.polled)}});
    request.result = std::move(failure);
    const auto retained_before = request.accounted;
    request.accounted = kEnvelopeBytes + 2 * OutputJson(request.rpc_id).size();
    retained -= retained_before - request.accounted;
  }
  void Collect() {
    const auto now = Clock::now();
    for (auto i = requests.begin(); i != requests.end();) {
      auto& request = *i->second;
      if (request.deadline > now &&
          !(request.detached_cleanup && request.done)) {
        ++i;
        continue;
      }
      if (!request.done) Fail(request, Expired());
      // A waiting caller retains its exact known result; the broker releases
      // ownership. No completed response is overwritten by late collection.
      Release(request);
      i = requests.erase(i);
    }
  }
};

McpTunnelBroker::McpTunnelBroker() : impl_(std::make_unique<Impl>()) {}
McpTunnelBroker::~McpTunnelBroker() { Stop(); }
Status McpTunnelBroker::Configure(std::vector<McpTunnelRoute> routes,
                                  McpTunnelLimits limits) {
  if (routes.size() > 128 || !limits.requests || limits.requests > 128 ||
      !limits.per_client || limits.per_client > limits.requests ||
      limits.retained_bytes < 4096 || limits.retained_bytes > (32U << 20) ||
      limits.request_ms < 1 || limits.request_ms > 120000 ||
      limits.agent_lease_ms < 1 || limits.agent_lease_ms > 30000)
    return Status::Fail("Invalid MCP tunnel limits");
  std::map<std::string, Impl::Route> next;
  for (auto& route : routes) {
    if (!ValidId(route.id) || !Text(route.generation, 128) ||
        !Names(route.callers, 128) || !Names(route.agents, 128) ||
        !Names(route.channels, 16) || next.contains(route.id))
      return Status::Fail("Invalid MCP tunnel route");
    const auto id = route.id;
    next.emplace(id, Impl::Route{std::move(route), {}, {}, {}});
  }
  std::lock_guard lock(impl_->mutex);
  if (impl_->configured || impl_->stopped)
    return Status::Fail("MCP tunnel configuration is immutable");
  impl_->routes = std::move(next);
  impl_->limits = limits;
  impl_->configured = true;
  return {};
}

ObjectReply McpTunnelBroker::Submit(
    const BinaryAccess& caller, std::string_view tunnel,
    std::string_view generation, std::string_view channel, const Json& headers,
    const Json& rpc, bool terminate_session, std::string_view expected_instance,
    bool detached_cleanup) {
  auto active = Active(caller);
  if (!active.ok()) return active;
  Json clean;
  if (!ValidId(tunnel) || !Text(generation, 128) || !Text(channel, 512) ||
      (!expected_instance.empty() && !Text(expected_instance, 128)) ||
      (!terminate_session && !RpcRequest(rpc)) ||
      (terminate_session && !rpc.IsNull()) ||
      (detached_cleanup && !terminate_session) ||
      !Headers(headers, false, &clean))
    return Error(400, "invalid_tunnel_request", "Invalid MCP tunnel request");
  const auto bytes = OutputJson(rpc).size() + OutputJson(clean).size();
  if (bytes > kRequestBytes)
    return Error(413, "mcp_tunnel_capacity", "Tunnel request exceeds capacity");
  std::string id, token;
  try {
    id = detail::NewRequestId("req_");
    token = detail::NewRequestId("mt_") + detail::NewRequestId("");
  } catch (const std::runtime_error&) {
    return Error(503, "mcp_entropy_unavailable", "Tunnel identity unavailable");
  }
  auto request = std::make_shared<Impl::Request>();
  request->id = id;
  request->token = token;
  request->tunnel = tunnel;
  request->generation = generation;
  request->channel = channel;
  request->caller = caller.scope;
  request->termination = terminate_session;
  request->detached_cleanup = detached_cleanup;
  request->expected_instance = expected_instance;
  if (const auto* rpc_id = rpc.Find("id")) {
    request->rpc_id = *rpc_id;
    request->has_rpc_id = true;
  }
  request->command =
      Object({{"request_id", String(id)},
              {"shard_token", String(token)},
              {"command_type",
               String(terminate_session ? "session_termination" : "jsonrpc")},
              {"channel", String(std::string(channel))},
              {"headers", std::move(clean)},
              {"created_at", String(CreatedAt())}});
  if (!terminate_session) Set(&request->command, "jsonrpc", rpc);
  request->command_bytes = OutputJson(request->command).size();
  request->accounted = kEnvelopeBytes + 2 * OutputJson(request->rpc_id).size() +
                       request->command_bytes;
  std::lock_guard lock(impl_->mutex);
  if (impl_->stopped)
    return Error(503, "mcp_tunnel_stopped", "Tunnel broker stopped");
  impl_->Collect();
  const auto route = impl_->routes.find(std::string(tunnel));
  if (route == impl_->routes.end() ||
      route->second.config.generation != generation ||
      !Contains(route->second.config.callers, caller.scope) ||
      !Contains(route->second.config.channels, channel))
    return Denied();
  const auto clients = std::count_if(
      impl_->requests.begin(), impl_->requests.end(),
      [&](const auto& entry) { return entry.second->caller == caller.scope; });
  if (impl_->requests.size() >= impl_->limits.requests ||
      static_cast<size_t>(clients) >= impl_->limits.per_client ||
      request->accounted > impl_->limits.retained_bytes - impl_->retained)
    return Error(503, "mcp_tunnel_capacity", "Tunnel queue capacity occupied");
  request->deadline = std::min(
      caller.deadline,
      Clock::now() + std::chrono::milliseconds(impl_->limits.request_ms));
  request->sequence = ++impl_->sequence;
  const auto [inserted, fresh] = impl_->requests.emplace(id, request);
  if (!fresh)
    return Error(503, "mcp_entropy_unavailable", "Tunnel identity unavailable");
  impl_->retained += inserted->second->accounted;
  impl_->changed.notify_all();
  ObjectReply result;
  result.value = Object({{"request_id", String(id)}});
  return result;
}

ObjectReply McpTunnelBroker::Poll(const BinaryAccess& agent,
                                  std::string_view tunnel,
                                  std::string_view instance,
                                  const std::vector<std::string>& channels,
                                  size_t limit, int wait_ms) {
  auto active = Active(agent);
  if (!active.ok()) return active;
  if (!Text(instance, 128) || limit < 1 || limit > 25 || wait_ms < 0 ||
      wait_ms > 15000 || (!channels.empty() && !Names(channels, 16)))
    return Error(400, "invalid_tunnel_poll", "Invalid tunnel poll parameters");
  const auto end = std::min(agent.deadline,
                            Clock::now() + std::chrono::milliseconds(wait_ms));
  for (;;) {
    // Cancellation can involve caller I/O; never invoke it while locked.
    active = Active(agent);
    if (!active.ok()) return active;
    std::unique_lock lock(impl_->mutex);
    if (impl_->stopped)
      return Error(503, "mcp_tunnel_stopped", "Tunnel broker stopped");
    impl_->Collect();
    const auto route = impl_->routes.find(std::string(tunnel));
    if (route == impl_->routes.end() ||
        !Contains(route->second.config.agents, agent.scope))
      return Denied();
    auto& owner = route->second;
    for (const auto& channel : channels)
      if (!Contains(owner.config.channels, channel)) return Denied();
    const auto now = Clock::now();
    if (owner.lease > now &&
        (owner.instance != instance || owner.agent_scope != agent.scope))
      return Error(409, "mcp_tunnel_agent_busy", "Tunnel agent lease occupied");
    owner.agent_scope = agent.scope;
    owner.instance = instance;
    owner.lease = now + std::chrono::milliseconds(impl_->limits.agent_lease_ms);
    std::vector<std::shared_ptr<Impl::Request>> pending;
    for (const auto& [id, request] : impl_->requests) {
      if (request->tunnel == tunnel && !request->polled && !request->done &&
          (channels.empty() || Contains(channels, request->channel)))
        pending.push_back(request);
    }
    std::sort(pending.begin(), pending.end(), [](const auto& a, const auto& b) {
      return a->sequence < b->sequence;
    });
    auto commands = Array();
    uint64_t bytes = 16;  // JSON envelope and trailing separator allowance.
    for (auto& request : pending) {
      if (!request->expected_instance.empty() &&
          request->expected_instance != instance) {
        impl_->Fail(*request, Error(409, "mcp_tunnel_session_lost",
                                    "Original tunnel agent unavailable"));
        impl_->changed.notify_all();
        continue;
      }
      const auto remaining =
          std::chrono::duration_cast<std::chrono::milliseconds>(
              request->deadline - Clock::now())
              .count();
      if (remaining <= 0) continue;
      auto command = request->command;
      Set(&command, "response_timeout",
          String(std::to_string(remaining) + "ms"));
      const auto size = OutputJson(command).size() + 1;
      if (commands.array.size() == limit || bytes + size > kPollBytes) break;
      commands.array.push_back(std::move(command));
      bytes += size;
      request->polled = true;
      request->agent = agent.scope;
      request->instance = instance;
      // Hand-off is irreversible, including when the HTTP poll receipt is lost.
      request->command = Json();
      impl_->retained -= request->command_bytes;
      request->accounted -= request->command_bytes;
      request->command_bytes = 0;
    }
    if (!commands.array.empty()) {
      ObjectReply result;
      result.value = Object({{"commands", std::move(commands)}});
      return result;
    }
    if (Clock::now() >= end) {
      ObjectReply empty;
      empty.http_status = 204;
      return empty;
    }
    impl_->changed.wait_until(lock, std::min(end, Clock::now() + 20ms));
  }
}

namespace {
int FinalStatus(int status) {
  // Recognized final statuses from the protocol's HTTP runtime. Private 2xx
  // still mean success; private failures cannot masquerade as standard ones.
  constexpr int known[] = {
      200, 201, 202, 203, 204, 205, 206, 207, 208, 226, 300, 301, 302, 303, 304,
      305, 307, 308, 400, 401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411,
      412, 413, 414, 415, 416, 417, 418, 421, 422, 423, 424, 425, 426, 428, 429,
      431, 451, 500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511};
  if (std::find(std::begin(known), std::end(known), status) != std::end(known))
    return status;
  return status >= 200 && status < 300 ? 200 : 502;
}
bool RpcResponse(const Json& rpc, const Json& id) {
  const auto* actual_id = rpc.Find("id");
  if (!rpc.IsObject() || rpc.GetString("jsonrpc") != "2.0" || !actual_id ||
      OutputJson(*actual_id) != OutputJson(id) || rpc.Has("method") ||
      rpc.Has("result") == rpc.Has("error"))
    return false;
  const auto* error = rpc.Find("error");
  if (!error) return true;
  const auto* code = error->Find("code");
  const auto* message = error->Find("message");
  return error->IsObject() && code && IsInputInteger(*code) && message &&
         message->IsString();
}
ObjectReply Ack() {
  ObjectReply result;
  result.value = Object({{"status", String("ok")}});
  return result;
}
}  // namespace

ObjectReply McpTunnelBroker::Respond(const BinaryAccess& agent,
                                     std::string_view tunnel,
                                     std::string_view instance,
                                     std::string_view shard_token,
                                     const Json& response) {
  auto active = Active(agent);
  if (!active.ok()) return active;
  const auto id = response.GetString("request_id");
  if (!response.IsObject() || !Text(id, 128) || !Text(instance, 128) ||
      !Text(shard_token, 128))
    return Error(400, "invalid_tunnel_client_response",
                 "Invalid tunnel response");
  std::lock_guard lock(impl_->mutex);
  if (impl_->stopped)
    return Error(503, "mcp_tunnel_stopped", "Tunnel broker stopped");
  impl_->Collect();
  const auto found = impl_->requests.find(id);
  if (found == impl_->requests.end())
    return Error(404, "mcp_tunnel_request_missing",
                 "Tunnel request unavailable");
  auto& request = *found->second;
  const auto route = impl_->routes.find(std::string(tunnel));
  if (route == impl_->routes.end() ||
      !Contains(route->second.config.agents, agent.scope) ||
      request.tunnel != tunnel || !request.polled ||
      request.agent != agent.scope || request.instance != instance ||
      !SecretEqual(request.token, shard_token))
    return Denied();
  // Validate after exact authentication, so malformed terminal data cannot
  // complete another caller's pending operation.
  const auto type = response.GetString("resp_type", "jsonrpc_response");
  const auto channel = response.GetString("channel", "main");
  const bool progress = type == "jsonrpc_notify";
  const auto* status = response.Find("resp_code");
  const auto* rpc = response.Find("resp_json");
  if (!rpc) rpc = response.Find("rpc_resp");
  Json headers;
  const auto* wire_headers = response.Find("resp_headers");
  const auto* unknown = response.Find("q4t_outcome_unknown");
  const auto size = OutputJson(response).size();
  bool valid =
      size <= kResponseBytes && status && IsInputInteger(*status) &&
      status->number >= 100 && status->number <= 599 &&
      channel == request.channel && !response.Has("shard_token") &&
      (!response.Has("channel") || response.Find("channel")->IsString()) &&
      (!response.Has("resp_type") || response.Find("resp_type")->IsString()) &&
      !(response.Has("resp_json") && response.Has("rpc_resp")) &&
      (!unknown || unknown->IsBool()) &&
      Headers(wire_headers ? *wire_headers : Object(), true, &headers);
  if (progress) {
    valid = valid && !request.termination && request.has_rpc_id && rpc &&
            RpcRequest(*rpc) && !rpc->Has("id") && !rpc->Has("result") &&
            !rpc->Has("error");
  } else if (type == "jsonrpc_response") {
    valid = valid && !request.termination && request.has_rpc_id && rpc &&
            RpcResponse(*rpc, request.rpc_id);
  } else if (type == "notify_ack") {
    valid = valid && !request.termination && !request.has_rpc_id && !rpc;
  } else if (type == "session_termination_response") {
    valid = valid && request.termination && !rpc;
  } else {
    valid = false;
  }
  if (unknown && unknown->IsBool() && unknown->boolean)
    valid = valid && type == "jsonrpc_response" && rpc && rpc->Has("error") &&
            status->number >= 400;
  if (channel != request.channel) return Denied();
  auto result =
      Object({{"resp_type", String(type)},
              {"resp_code", Number(valid ? FinalStatus(status->number) : 502)},
              {"resp_headers", std::move(headers)}});
  if (rpc && valid) Set(&result, "resp_json", *rpc);
  if (valid && response.GetBool("q4t_outcome_unknown"))
    Set(&result, "q4t_outcome_unknown", Boolean(true));
  const auto identity = valid ? Digest(OutputJson(result)) : "";
  if (request.done) {
    if (valid && !progress && identity == request.final_identity) return Ack();
    return Error(409, "mcp_tunnel_response_conflict",
                 "Tunnel result already final");
  }
  auto invalid =
      Error(400, "invalid_tunnel_client_response", "Invalid tunnel response");
  if (!valid) {
    if (progress) return invalid;
    result =
        Object({{"resp_type",
                 String(request.termination  ? "session_termination_response"
                        : request.has_rpc_id ? "jsonrpc_response"
                                             : "notify_ack")},
                {"resp_code", Number(502)},
                {"resp_headers", Object()}});
    if (request.has_rpc_id) {
      Set(&result, "resp_json", GatewayError(request.rpc_id));
      Set(&result, "q4t_outcome_unknown", Boolean(true));
    }
  }
  const auto result_bytes = OutputJson(result).size();
  if (progress) {
    if (request.notifications.array.size() >= 64 ||
        request.progress_bytes + result_bytes > kProgressBytes ||
        result_bytes > impl_->limits.retained_bytes - impl_->retained)
      return Error(429, "mcp_tunnel_progress_capacity",
                   "Tunnel progress capacity occupied");
    request.notifications.array.push_back(std::move(result));
    request.progress_bytes += result_bytes;
    request.accounted += result_bytes;
    impl_->retained += result_bytes;
    return Ack();
  }
  if (valid && result_bytes > impl_->limits.retained_bytes - impl_->retained) {
    impl_->Fail(request, Error(503, "mcp_tunnel_capacity",
                               "Tunnel response capacity occupied"));
    impl_->changed.notify_all();
    return Error(503, "mcp_tunnel_capacity",
                 "Tunnel response capacity occupied");
  }
  // Invalid responses fit the per-request reserved failure space. Legitimate
  // payloads get a distinct charge, including preserved progress events.
  if (valid) {
    request.accounted += result_bytes;
    impl_->retained += result_bytes;
  }
  request.done = true;
  request.final_identity = identity;
  Set(&result, "dispatched", Boolean(true));
  request.result.value = std::move(result);
  impl_->changed.notify_all();
  if (request.detached_cleanup) {
    impl_->Release(request);
    impl_->requests.erase(id);
  }
  return valid ? Ack() : invalid;
}

ObjectReply McpTunnelBroker::Wait(const BinaryAccess& caller,
                                  std::string_view request_id) {
  std::unique_lock lock(impl_->mutex);
  const auto found = impl_->requests.find(std::string(request_id));
  if (found == impl_->requests.end())
    return Error(404, "mcp_tunnel_request_missing",
                 "Tunnel request unavailable");
  auto request = found->second;
  if (caller.scope.empty() || request->caller != caller.scope) return Denied();
  if (request->detached_cleanup)
    return Error(409, "mcp_tunnel_cleanup", "Cleanup has no response owner");
  if (request->claimed)
    return Error(409, "mcp_tunnel_waiter_busy", "Tunnel result already owned");
  request->claimed = true;
  for (;;) {
    if (request->done) break;
    lock.unlock();
    auto active = Active(caller);
    lock.lock();
    if (request->done) break;
    if (!active.ok() || impl_->stopped || Clock::now() >= request->deadline) {
      impl_->Fail(*request, !active.ok()     ? active
                            : impl_->stopped ? Error(503, "mcp_tunnel_stopped",
                                                     "Tunnel broker stopped")
                                             : Expired());
      break;
    }
    impl_->changed.wait_until(lock,
                              std::min(request->deadline, Clock::now() + 20ms));
  }
  auto result = std::move(request->result);
  if (request->polled)
    Set(&result.value, "agent_instance", String(request->instance));
  if (result.ok())
    Set(&result.value, "notifications", std::move(request->notifications));
  impl_->Release(*request);
  impl_->requests.erase(request->id);
  impl_->changed.notify_all();
  return result;
}

ObjectReply McpTunnelBroker::Describe(const BinaryAccess& access,
                                      std::string_view tunnel) {
  auto active = Active(access);
  if (!active.ok()) return active;
  std::lock_guard lock(impl_->mutex);
  const auto route = impl_->routes.find(std::string(tunnel));
  if (route == impl_->routes.end() ||
      (!Contains(route->second.config.agents, access.scope) &&
       !Contains(route->second.config.callers, access.scope)))
    return Denied();
  ObjectReply result;
  result.value =
      Object({{"id", String(route->first)},
              {"object", String("tunnel")},
              {"name", String(route->first)},
              {"status", String(impl_->stopped ? "stopped" : "active")},
              {"proc_affinity", Boolean(true)}});
  return result;
}
Json McpTunnelBroker::Health() const {
  std::lock_guard lock(impl_->mutex);
  impl_->Collect();
  int64_t online = 0, pending = 0, inflight = 0, completed = 0;
  for (const auto& [id, route] : impl_->routes)
    if (!impl_->stopped && route.lease > Clock::now()) ++online;
  for (const auto& [id, request] : impl_->requests) {
    if (request->done)
      ++completed;
    else if (request->polled)
      ++inflight;
    else
      ++pending;
  }
  return Object({{"configured", Boolean(impl_->configured)},
                 {"stopped", Boolean(impl_->stopped)},
                 {"routes", Number(impl_->routes.size())},
                 {"agents_online", Number(online)},
                 {"pending", Number(pending)},
                 {"inflight", Number(inflight)},
                 {"completed", Number(completed)},
                 {"retained_bytes", Number(impl_->retained)}});
}
void McpTunnelBroker::Stop() {
  std::lock_guard lock(impl_->mutex);
  impl_->stopped = true;
  for (auto& [id, request] : impl_->requests) {
    if (!request->done)
      impl_->Fail(*request,
                  Error(503, "mcp_tunnel_stopped", "Tunnel broker stopped"));
    impl_->Release(*request);
  }
  impl_->requests.clear();
  impl_->changed.notify_all();
}
}  // namespace q4t::server
