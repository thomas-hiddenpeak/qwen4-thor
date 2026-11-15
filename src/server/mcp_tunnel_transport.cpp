#include "mcp_tunnel_transport_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_response.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
int64_t Now() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}
bool Text(std::string_view value, size_t limit) {
  return !value.empty() && value.size() <= limit &&
         std::all_of(value.begin(), value.end(),
                     [](unsigned char c) { return c >= 0x21 && c <= 0x7e; });
}
bool Protocol(std::string_view value) {
  return value == "2025-06-18" || value == "2025-03-26" ||
         value == "2024-11-05";
}
ObjectReply Invalid() {
  return Error(503, "mcp_tunnel_authority", "Invalid native MCP authority");
}
struct Lease {
  std::weak_ptr<McpTunnelBroker> broker;
  McpEndpoint endpoint;
  std::string id, scope, instance, remote_session, protocol;
  int64_t expires = 0, idle = 0;
};
Json Headers(const Lease& lease, bool initialize = false) {
  auto headers = Object();
  for (const auto& [key, value] : lease.endpoint.headers)
    Set(&headers, http::Lower(key), String(value));
  if (!lease.endpoint.authorization.empty())
    Set(&headers, "authorization",
        String("Bearer " + lease.endpoint.authorization));
  Set(&headers, "accept", String("application/json, text/event-stream"));
  Set(&headers, "content-type", String("application/json"));
  if (!initialize && !lease.remote_session.empty())
    Set(&headers, "mcp-session-id", String(lease.remote_session));
  if (!initialize && !lease.protocol.empty())
    Set(&headers, "mcp-protocol-version", String(lease.protocol));
  return headers;
}
Json WireHeaders(const Json& headers) {
  auto result = Object();
  for (const auto& [name, value] : headers.object)
    Set(&result, name, Array({value}));
  return result;
}
void Release(const Lease& lease) {
  if (lease.instance.empty() || lease.remote_session.empty()) return;
  const auto broker = lease.broker.lock();
  if (!broker) return;
  try {
    // Submit holds no external I/O; its deadline bounds abandoned results as
    // well as queued work. The agent must never replay this or any tool call.
    const BinaryAccess access{
        lease.scope, Clock::now() + std::chrono::seconds(1), {}};
    (void)broker->Submit(
        access, lease.endpoint.tunnel_id, lease.endpoint.tunnel_generation,
        lease.endpoint.tunnel_channel, WireHeaders(Headers(lease)), Json(),
        true, lease.instance, true);
  } catch (...) {
    // Cleanup failure cannot replace a known result or stop core service.
  }
}
bool SameHeaders(const Json& actual, const Json& expected) {
  if (!actual.IsObject() || actual.object.size() != expected.object.size())
    return false;
  auto normalized = Object();
  for (const auto& [key, value] : actual.object) {
    const auto name = http::Lower(key);
    if (!value.IsString() || normalized.Has(name)) return false;
    Set(&normalized, name, value);
  }
  for (const auto& [key, value] : expected.object) {
    const auto* found = normalized.Find(key);
    if (!found || found->str != value.str) return false;
  }
  return true;
}
bool PrivateState(const Json& state) {
  const auto created = state.GetInt("created_ms");
  const auto expires = state.GetInt("expires_ms");
  const auto idle = state.GetInt("idle_ms");
  return Text(state.GetString("agent_instance"), 128) &&
         (state.GetString("session_id").empty() ||
          Text(state.GetString("session_id"), 1024)) &&
         Protocol(state.GetString("protocol")) && !state.GetBool("legacy") &&
         created > 0 && expires > created && expires - created <= 3600000 &&
         idle > 0 && idle <= 300000 &&
         state.GetInt("idle_expires_ms") >= created &&
         state.GetInt("idle_expires_ms") <= expires + 300000;
}
}  // namespace

struct TunnelSessions::Impl {
  std::map<std::string, std::shared_ptr<Lease>> leases;
};
struct TunnelExchange::Impl {
  TunnelSessions* owner;
  std::shared_ptr<Lease> lease;
  BinaryAccess access;  // Lives only for this synchronous invocation.
  std::string method;
  Json params, initial;
  bool allow_new = false, retained = false;
  bool initialize = false, notified = false, called = false;
  unsigned lists = 0;
};
TunnelSessions::TunnelSessions() : impl_(std::make_unique<Impl>()) {}
TunnelSessions::~TunnelSessions() = default;
void TunnelSessions::Collect() {
  const auto now = Now();
  for (auto i = impl_->leases.begin(); i != impl_->leases.end();) {
    if (now >= i->second->expires || now >= i->second->idle) {
      Release(*i->second);
      i = impl_->leases.erase(i);
    } else {
      ++i;
    }
  }
}
void TunnelSessions::Clear(bool release_remote) {
  if (release_remote)
    for (const auto& [id, lease] : impl_->leases) Release(*lease);
  impl_->leases.clear();
}
ObjectReply TunnelSessions::Begin(
    const BinaryAccess& access, const McpEndpoint& endpoint,
    const std::shared_ptr<McpTunnelBroker>& broker, std::string_view method,
    const Json& params, const McpSession* session,
    std::unique_ptr<TunnelExchange>* exchange) {
  if (!broker || !exchange) return Invalid();
  exchange->reset();
  Collect();
  const auto initial = session ? session->state : Json();
  const bool initialized = initial.Has("protocol");
  if (initialized && !PrivateState(initial)) return Invalid();
  const auto id =
      session ? initial.GetString("id") : detail::NewRequestId("mcpt_");
  if (id.empty()) return Invalid();
  auto& leases = impl_->leases;
  auto found = leases.find(id);
  if (found == leases.end()) {
    const auto count = std::count_if(
        leases.begin(), leases.end(),
        [&](const auto& item) { return item.second->scope == access.scope; });
    if (leases.size() >= 16 || count >= 4)
      return Error(503, "mcp_session_capacity",
                   "Native MCP session capacity occupied");
    auto lease = std::make_shared<Lease>();
    lease->id = id;
    lease->broker = broker;
    lease->endpoint = endpoint;
    lease->scope = access.scope;
    lease->expires = Now() + 3600000;
    lease->idle = Now() + 300000;
    if (initialized) {
      lease->instance = initial.GetString("agent_instance");
      lease->remote_session = initial.GetString("session_id");
      lease->protocol = initial.GetString("protocol");
      lease->expires = initial.GetInt("expires_ms");
      lease->idle = initial.GetInt("idle_expires_ms");
    }
    found = leases.emplace(id, std::move(lease)).first;
  }
  auto lease = found->second;
  if (lease->scope != access.scope || lease->broker.lock() != broker ||
      lease->endpoint.tunnel_id != endpoint.tunnel_id ||
      lease->endpoint.tunnel_generation != endpoint.tunnel_generation ||
      lease->endpoint.tunnel_channel != endpoint.tunnel_channel ||
      lease->endpoint.headers != endpoint.headers ||
      lease->endpoint.authorization != endpoint.authorization ||
      (initialized &&
       (lease->instance != initial.GetString("agent_instance") ||
        lease->remote_session != initial.GetString("session_id") ||
        lease->protocol != initial.GetString("protocol"))))
    return Invalid();
  auto state = std::make_unique<TunnelExchange::Impl>();
  state->owner = this;
  state->lease = std::move(lease);
  state->access = access;
  state->method = method;
  state->params = params;
  state->initial = initial;
  state->allow_new = !session || session->allow_new;
  state->retained = initialized;
  *exchange =
      std::unique_ptr<TunnelExchange>(new TunnelExchange(std::move(state)));
  return {};
}
TunnelExchange::TunnelExchange(std::unique_ptr<Impl> impl)
    : impl_(std::move(impl)) {}
TunnelExchange::~TunnelExchange() {
  if (impl_->retained) return;
  Release(*impl_->lease);
  impl_->owner->impl_->leases.erase(impl_->lease->id);
}
void TunnelExchange::Retain() { impl_->retained = true; }
ObjectReply TunnelExchange::Checkpoint(const Json& state) {
  auto& lease = *impl_->lease;
  if (!PrivateState(state) ||
      state.GetString("agent_instance") != lease.instance ||
      state.GetString("session_id") != lease.remote_session ||
      state.GetString("protocol") != lease.protocol ||
      !state.GetString("id").starts_with("mcps_") ||
      !ValidId(state.GetString("id")))
    return Invalid();
  if (state.GetString("id") != lease.id) {
    auto& leases = impl_->owner->impl_->leases;
    if (leases.contains(state.GetString("id"))) return Invalid();
    leases.emplace(state.GetString("id"), impl_->lease);
    leases.erase(lease.id);
    lease.id = state.GetString("id");
  }
  lease.expires = state.GetInt("expires_ms");
  lease.idle = state.GetInt("idle_expires_ms");
  return {};
}
ObjectReply TunnelExchange::Forward(const Json& wire, bool permitted) {
  auto& s = *impl_;
  auto& lease = *s.lease;
  const auto* rpc = wire.Find("rpc");
  const auto* headers = wire.Find("headers");
  if (!rpc || !rpc->IsObject() || rpc->GetString("jsonrpc") != "2.0" ||
      !headers || rpc->Has("result") || rpc->Has("error"))
    return Invalid();
  const auto method = rpc->GetString("method");
  const auto* params = rpc->Find("params");
  const bool initialize = method == "initialize";
  if (initialize) {
    const auto now = Now();
    const bool renewable = !s.initial.Has("protocol") ||
                           s.initial.GetBool("lost") ||
                           now < s.initial.GetInt("created_ms") ||
                           now >= s.initial.GetInt("expires_ms") ||
                           now >= s.initial.GetInt("idle_expires_ms");
    if (s.initialize || !s.allow_new || !renewable || !rpc->Has("id") ||
        !params || !params->IsObject() ||
        params->GetString("protocolVersion") != "2025-06-18")
      return Invalid();
  } else if (method == "notifications/initialized") {
    if (!s.initialize || s.notified || rpc->Has("id")) return Invalid();
    s.notified = true;
  } else if (method == "tools/list") {
    if (s.method != method || ++s.lists > 32 || !rpc->Has("id") || !params ||
        !params->IsObject() ||
        (!params->object.empty() &&
         (params->object.size() != 1 || !params->Find("cursor") ||
          !params->Find("cursor")->IsString() ||
          params->GetString("cursor").empty() ||
          params->GetString("cursor").size() > 8192)))
      return Invalid();
  } else {
    if (s.method != method || s.called || !permitted || !params ||
        OutputJson(*params) != OutputJson(s.params) || !rpc->Has("id") ||
        (method != "tools/call" && method != "resources/read"))
      return Invalid();
    s.called = true;
  }
  const auto expected = Headers(lease, initialize);
  if (!SameHeaders(*headers, expected) ||
      wire.GetString("expected_instance") != (initialize ? "" : lease.instance))
    return Invalid();
  if (initialize) {
    Release(lease);
    lease.instance.clear();
    lease.remote_session.clear();
    lease.protocol.clear();
    s.initialize = true;
    s.retained = false;
  } else if (lease.instance.empty() || lease.protocol.empty()) {
    return Invalid();
  }
  const auto broker = lease.broker.lock();
  if (!broker)
    return Error(503, "mcp_tunnel_stopped", "Native MCP broker unavailable");
  auto queued = broker->Submit(
      s.access, lease.endpoint.tunnel_id, lease.endpoint.tunnel_generation,
      lease.endpoint.tunnel_channel, WireHeaders(expected), *rpc, false,
      lease.instance);
  if (!queued.ok()) return queued;
  auto reply = broker->Wait(s.access, queued.value.GetString("request_id"));
  if (!reply.ok()) return reply;
  const auto instance = reply.value.GetString("agent_instance");
  if (!Text(instance, 128) ||
      (!lease.instance.empty() && instance != lease.instance))
    return Invalid();
  lease.instance = instance;
  if (reply.value.GetBool("q4t_outcome_unknown")) {
    auto failed = Error(502, "mcp_tunnel_outcome_unknown",
                        "Private MCP outcome unavailable");
    Set(&failed.value, "upstream_status",
        Number(reply.value.GetInt("resp_code")));
    return failed;
  }
  if (initialize) {
    const auto* response = reply.value.Find("resp_json");
    const auto* result = response ? response->Find("result") : nullptr;
    const auto* received_headers = reply.value.Find("resp_headers");
    if (received_headers) {
      const auto* remote = received_headers->Find("mcp-session-id");
      if (remote) {
        if (!remote->IsArray() || remote->array.size() != 1 ||
            !remote->array[0].IsString() || !Text(remote->array[0].str, 1024))
          return Invalid();
        lease.remote_session = remote->array[0].str;
      }
    }
    if (result && Protocol(result->GetString("protocolVersion")))
      lease.protocol = result->GetString("protocolVersion");
  }
  return reply;
}
}  // namespace q4t::server::mcp_detail
