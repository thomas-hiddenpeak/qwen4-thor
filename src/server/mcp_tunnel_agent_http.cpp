#include "mcp_tunnel_agent_internal.h"

#include <algorithm>
#include <set>

#include "mcp_sse_internal.h"
#include "mcp_transport_internal.h"
#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/lifetime.h"
#include "remote_http_internal.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using Deadline = Clock::time_point;
using file_detail::RemoteHeaders;
using file_detail::RemoteHttp;
using file_detail::Url;
constexpr uint64_t kReplyBytes = 4U << 20;
bool Text(std::string_view value, size_t limit) {
  return !value.empty() && value.size() <= limit &&
         std::all_of(value.begin(), value.end(),
                     [](unsigned char c) { return c >= 0x21 && c <= 0x7e; });
}
ObjectReply Protocol() {
  return Error(502, "mcp_protocol_error", "Invalid private MCP response");
}
bool Hop(std::string_view key) {
  return key == "host" || key == "content-length" ||
         key == "transfer-encoding" || key == "connection" ||
         key == "trailer" || key == "upgrade" || key == "expect" ||
         key == "te" || key == "keep-alive" || key.starts_with("proxy-") ||
         key == "accept-encoding" || key == "user-agent";
}
bool Latin1(std::string_view text, std::string* bytes) {
  bytes->clear();
  for (size_t i = 0; i < text.size(); ++i) {
    auto c = static_cast<unsigned char>(text[i]);
    if (c == '\t' || (c >= 0x20 && c <= 0x7e)) {
      bytes->push_back(c);
      continue;
    }
    if ((c != 0xc2 && c != 0xc3) || ++i == text.size()) return false;
    const auto tail = static_cast<unsigned char>(text[i]);
    if (tail < 0x80 || tail > 0xbf) return false;
    bytes->push_back(static_cast<char>(((c & 0x1f) << 6) | (tail & 0x3f)));
  }
  return true;
}
std::string Unicode(std::string_view bytes) {
  std::string value;
  for (unsigned char c : bytes) {
    if (c < 0x80) {
      value.push_back(c);
    } else {
      value.push_back(static_cast<char>(0xc0 | (c >> 6)));
      value.push_back(static_cast<char>(0x80 | (c & 0x3f)));
    }
  }
  return value;
}
bool RequestHeaders(const Json& wire, const McpEndpoint& fixed,
                    RemoteHeaders* headers, std::string* authority) {
  headers->clear();
  if (!wire.IsObject() || wire.object.size() > 64 ||
      OutputJson(wire).size() > 16384)
    return false;
  std::set<std::string> omitted;
  for (const auto& [key, list] : wire.object) {
    if (http::Lower(key) != "connection") continue;
    if (!list.IsArray()) return false;
    for (const auto& value : list.array) {
      std::vector<std::string> tokens;
      if (!value.IsString() || !http::Tokens(value.str, &tokens)) return false;
      for (const auto& token : tokens) omitted.insert(http::Lower(token));
    }
  }
  std::set<std::string> seen;
  for (const auto& [key, list] : wire.object) {
    const auto name = http::Lower(key);
    if (name.empty() || !std::all_of(name.begin(), name.end(), http::IsToken) ||
        !seen.insert(name).second || !list.IsArray() || list.array.size() > 16)
      return false;
    if (Hop(name) || omitted.contains(name)) continue;
    if ((name == "authorization" || name == "content-type" ||
         name.starts_with("mcp-") || name == "last-event-id") &&
        list.array.size() != 1)
      return false;
    std::string joined;
    for (const auto& value : list.array) {
      std::string bytes;
      if (!value.IsString() || !Latin1(value.str, &bytes)) return false;
      if (!joined.empty()) joined += name == "cookie" ? "; " : ", ";
      joined += bytes;
    }
    if (!joined.empty()) headers->emplace(name, std::move(joined));
  }
  // Operator credentials and delegated user credentials may not silently
  // replace each other. A conflict fails before contacting the private host.
  auto merge = [&](std::string key, const std::string& value) {
    key = http::Lower(key);
    const auto found = headers->find(key);
    if (found != headers->end() && found->second != value) return false;
    (*headers)[std::move(key)] = value;
    return true;
  };
  for (const auto& [key, value] : fixed.headers)
    if (!merge(key, value)) return false;
  if (!fixed.authorization.empty() &&
      !merge("authorization", "Bearer " + fixed.authorization))
    return false;
  (*headers)["accept"] = "application/json, text/event-stream";
  (*headers)["content-type"] = "application/json";
  auto identity = Object();
  for (const auto& [key, value] : *headers)
    if (key != "mcp-session-id" && key != "mcp-protocol-version" &&
        key != "last-event-id")
      Set(&identity, key, String(Unicode(value)));
  return InputValueIdentity(identity, authority).ok();
}
Json ResponseHeaders(const RemoteHttp& response) {
  auto headers = Object();
  for (const auto* key :
       {"content-type", "mcp-session-id", "mcp-protocol-version",
        "last-event-id", "access-control-expose-headers", "www-authenticate",
        "x-request-id"})
    if (response.HasHeader(key))
      Set(&headers, key, Array({String(Unicode(response.Header(key)))}));
  return headers;
}
Json Response(const Json& command, int status, Json headers,
              const Json* rpc = nullptr, bool progress = false) {
  const bool termination =
      command.GetString("command_type") == "session_termination";
  const auto* original = command.Find("jsonrpc");
  const bool notification = original && !original->Has("id");
  auto result = Object(
      {{"request_id", *command.Find("request_id")},
       {"channel", String(command.GetString("channel", "main"))},
       {"resp_code", Number(status)},
       {"resp_headers", std::move(headers)},
       {"resp_type", String(progress       ? "jsonrpc_notify"
                            : termination  ? "session_termination_response"
                            : notification ? "notify_ack"
                                           : "jsonrpc_response")}});
  if (rpc) Set(&result, "resp_json", *rpc);
  if (OutputJson(result).size() > kReplyBytes) {
    Set(&result, "resp_code", Number(413));
    Set(&result, "resp_headers", Object());
    if (original && original->Find("id"))
      Set(&result, "resp_json",
          Object({{"jsonrpc", String("2.0")},
                  {"id", *original->Find("id")},
                  {"error",
                   Object({{"code", Number(-32603)},
                           {"message", String("MCP response limit")}})}}));
  }
  return result;
}
ObjectReply TerminalError(const Json& command, const ObjectReply& error,
                          bool attempted) {
  Json rpc;
  const auto* original = command.Find("jsonrpc");
  if (original && original->Find("id"))
    rpc = Object(
        {{"jsonrpc", String("2.0")},
         {"id", *original->Find("id")},
         {"error", Object({{"code", Number(-32603)},
                           {"message", String("Private MCP transport failed")},
                           {"data", Object({{"code", String(error.code)},
                                            {"outcome_unknown",
                                             Boolean(attempted)}})}})}});
  ObjectReply result;
  result.value = Response(command, error.http_status, Object(),
                          rpc.IsNull() ? nullptr : &rpc);
  // This error was synthesized by the transport, not returned by the tool.
  // Preserve uncertainty outside arbitrary upstream JSON-RPC error data.
  if (attempted && !rpc.IsNull())
    Set(&result.value, "q4t_outcome_unknown", Boolean(true));
  return result;
}
bool Rpc(const Json& message, const Json& expected) {
  const auto* id = message.Find("id");
  if (!message.IsObject() || message.GetString("jsonrpc") != "2.0" || !id ||
      OutputJson(*id) != OutputJson(expected) || message.Has("method") ||
      message.Has("result") == message.Has("error"))
    return false;
  const auto* error = message.Find("error");
  return !error ||
         (error->IsObject() && error->Find("code") &&
          IsInputInteger(*error->Find("code")) && error->Find("message") &&
          error->Find("message")->IsString());
}
bool SameOrigin(const Url& a, const Url& b) {
  return a.scheme == b.scheme && a.host == b.host && a.port == b.port;
}
}  // namespace

struct TunnelAgentBackend::Impl {
  struct Session {
    std::string authority, token;
    Url post;
    RemoteHeaders headers;
    Deadline created, touched;
    std::unique_ptr<RemoteHttp> stream;
    std::unique_ptr<Events> events;
  };
  McpEndpoint fixed;
  FileFetchOptions network;
  Url target;
  ObjectReply checked;
  std::map<std::string, std::unique_ptr<Session>> sessions;

  void Close(Session& session) {
    if (session.stream) return;
    try {
      std::unique_ptr<RemoteHttp> response;
      (void)RemoteHttp::Open(
          session.post, network, "DELETE", session.headers, "", 0,
          Clock::now() + std::chrono::milliseconds(100), &response);
    } catch (...) {
    }
  }
  ObjectReply Send(std::string_view method, const Url& target,
                   const RemoteHeaders& headers, const Json* rpc,
                   Deadline deadline, std::unique_ptr<RemoteHttp>* response) {
    if (Clock::now() >= deadline)
      return Error(408, "mcp_deadline_exceeded",
                   "Private MCP deadline expired");
    return RemoteHttp::Open(target, network, method, headers,
                            rpc ? OutputJson(*rpc) : std::string{}, kReplyBytes,
                            deadline, response);
  }
  ObjectReply Read(const Json& command, Session* session, RemoteHttp& response,
                   const Url& post, const RemoteHeaders& headers,
                   Deadline deadline,
                   const std::function<bool(const Json&)>& progress,
                   Json* result) {
    const auto* rpc = command.Find("jsonrpc");
    const bool legacy = session && session->stream;
    if (!rpc || !rpc->Has("id")) {
      std::string body;
      auto reply = response.ReadAll(&body);
      if (!reply.ok()) return reply;
      return response.Status() == 202 && body.empty() ? ObjectReply{}
                                                      : Protocol();
    }
    const bool stream = legacy || response.Mime() == "text/event-stream";
    if (!stream && response.Mime() != "application/json") return Protocol();
    auto events =
        stream && !legacy ? std::make_unique<Events>(&response) : nullptr;
    unsigned notifications = 0;
    uint64_t progress_bytes = 0;
    bool deliver_progress = true;
    for (unsigned messages = 0; messages < 256; ++messages) {
      std::string body, kind;
      auto reply = stream ? (legacy ? session->events.get() : events.get())
                                ->Next(&kind, &body)
                          : response.ReadAll(&body);
      if (!reply.ok()) return reply;
      if (stream && kind != "message") return Protocol();
      Json message;
      if (!Parse(body, &message) || !message.IsObject() ||
          message.GetString("jsonrpc") != "2.0")
        return Protocol();
      if (message.Has("method")) {
        const auto* method = message.Find("method");
        if (!stream || !method->IsString() || !Text(method->str, 512) ||
            message.Has("result") || message.Has("error"))
          return Protocol();
        if (const auto* id = message.Find("id")) {
          if (!id->IsString() && !IsInputInteger(*id)) return Protocol();
          auto answer = Object({{"jsonrpc", String("2.0")}, {"id", *id}});
          if (method->str == "ping")
            Set(&answer, "result", Object());
          else
            Set(&answer, "error",
                Object({{"code", Number(-32601)},
                        {"message", String("Method not available")}}));
          std::unique_ptr<RemoteHttp> ack;
          reply = Send("POST", post, headers, &answer, deadline, &ack);
          if (!reply.ok()) return reply;
          std::string ignored;
          reply = ack->ReadAll(&ignored);
          if (!reply.ok()) return reply;
          if (ack->Status() != 202 || !ignored.empty()) return Protocol();
        } else {
          const auto event = Response(command, 200, Object(), &message, true);
          progress_bytes += OutputJson(event).size();
          if (++notifications > 64 || progress_bytes > (1U << 20))
            deliver_progress = false;
          if (deliver_progress && progress && !progress(event))
            deliver_progress = false;
          // Progress is auxiliary. An ambiguous upload must not be retried,
          // and failure must not prevent draining the actual terminal result.
        }
        continue;
      }
      if (!Rpc(message, *rpc->Find("id"))) return Protocol();
      *result = std::move(message);
      return {};
    }
    return Error(413, "mcp_message_capacity", "MCP message limit");
  }
};
TunnelAgentBackend::TunnelAgentBackend(McpEndpoint target,
                                       FileFetchOptions network)
    : impl_(std::make_unique<Impl>()) {
  impl_->fixed = std::move(target);
  impl_->network = std::move(network);
  impl_->checked = CheckEndpoint(impl_->fixed, impl_->network);
  if (!impl_->fixed.tunnel_id.empty())
    impl_->checked =
        Error(400, "mcp_agent_target", "Agent requires a fixed URL");
  file_detail::ParseUrl(impl_->fixed.url, &impl_->target);
}
TunnelAgentBackend::~TunnelAgentBackend() { Clear(); }
void TunnelAgentBackend::Collect() {
  const auto now = Clock::now();
  for (auto i = impl_->sessions.begin(); i != impl_->sessions.end();) {
    if (now - i->second->created >= std::chrono::hours(1) ||
        now - i->second->touched >= std::chrono::minutes(5)) {
      impl_->Close(*i->second);
      i = impl_->sessions.erase(i);
    } else {
      ++i;
    }
  }
}
void TunnelAgentBackend::Clear() {
  for (auto& [key, session] : impl_->sessions) impl_->Close(*session);
  impl_->sessions.clear();
}
ObjectReply TunnelAgentBackend::Exchange(
    const Json& command, Deadline deadline,
    const std::function<bool(const Json&)>& progress) {
  const auto* rpc = command.Find("jsonrpc");
  const auto* fields = command.Find("headers");
  const auto type = command.GetString("command_type", "jsonrpc");
  const bool termination = type == "session_termination";
  if (!command.IsObject() || !Text(command.GetString("request_id"), 128) ||
      !Text(command.GetString("channel", "main"), 128) ||
      OutputJson(command).size() > (2U << 20) ||
      (!termination && type != "jsonrpc") || !fields ||
      (!termination &&
       (!rpc || !rpc->IsObject() || rpc->GetString("jsonrpc") != "2.0" ||
        !Text(rpc->GetString("method"), 512) || rpc->Has("result") ||
        rpc->Has("error") ||
        (rpc->Has("id") && !rpc->Find("id")->IsString() &&
         !IsInputInteger(*rpc->Find("id"))))))
    return Error(400, "mcp_agent_command", "Invalid tunnel command");
  if (!impl_->checked.ok())
    return TerminalError(command, impl_->checked, false);
  RemoteHeaders headers;
  std::string authority;
  if (!RequestHeaders(*fields, impl_->fixed, &headers, &authority))
    return TerminalError(command,
                         Error(403, "mcp_agent_credentials",
                               "Private MCP headers conflict with policy"),
                         false);
  Collect();
  deadline = std::min(deadline, Clock::now() + std::chrono::milliseconds(
                                                   impl_->network.timeout_ms));
  const auto method = rpc ? rpc->GetString("method") : "";
  const bool initialize = method == "initialize";
  const auto token =
      headers.contains("mcp-session-id") ? headers.at("mcp-session-id") : "";
  auto& sessions = impl_->sessions;
  const auto key = Digest(authority + ':' + token);
  auto found = sessions.find(key);
  if ((initialize && !token.empty()) ||
      (!token.empty() && found == sessions.end()))
    return TerminalError(command,
                         Error(404, "mcp_session_lost",
                               "Original private MCP session unavailable"),
                         false);
  if (initialize && sessions.size() >= 16)
    return TerminalError(command,
                         Error(503, "mcp_session_capacity",
                               "Private MCP session capacity occupied"),
                         false);
  Impl::Session* session =
      found == sessions.end() ? nullptr : found->second.get();
  const bool owned_legacy = session && session->stream;
  bool retain_legacy = false;
  ScopeExit close_lost_stream([&] {
    if (owned_legacy && !retain_legacy) sessions.erase(key);
  });
  if (session) {
    deadline = std::min(deadline, session->created + std::chrono::hours(1));
    session->touched = Clock::now();
    if (session->stream) {
      headers.erase("mcp-session-id");
      session->stream->BeginRead(deadline);
    }
  }
  if (termination && session && session->stream) {
    sessions.erase(found);
    ObjectReply reply;
    reply.value = Response(command, 204, Object());
    return reply;
  }
  if (termination && token.empty()) {
    ObjectReply reply;
    reply.value = Response(command, 204, Object());
    return reply;
  }
  const auto post = session ? session->post : impl_->target;
  std::unique_ptr<RemoteHttp> response;
  auto reply = impl_->Send(termination ? "DELETE" : "POST", post, headers,
                           termination ? nullptr : rpc, deadline, &response);
  if (termination && found != sessions.end()) sessions.erase(found);
  if (!reply.ok()) return TerminalError(command, reply, true);
  if (termination) {
    ObjectReply result;
    result.value =
        Response(command, response->Status(), ResponseHeaders(*response));
    return result;
  }
  std::unique_ptr<Impl::Session> prepared;
  if (initialize && (response->Status() == 400 || response->Status() == 404 ||
                     response->Status() == 405 || response->Status() == 415)) {
    // Only negotiation may fall back; tools/resources are never resubmitted.
    // A valid native RPC rejection is already a complete terminal answer.
    if (response->Mime() == "application/json") {
      std::string body;
      Json message;
      reply = response->ReadAll(&body);
      if (reply.ok() && Parse(body, &message) && rpc->Find("id") &&
          Rpc(message, *rpc->Find("id"))) {
        ObjectReply result;
        result.value = Response(command, response->Status(),
                                ResponseHeaders(*response), &message);
        return result;
      }
    }
    prepared = std::make_unique<Impl::Session>();
    reply = impl_->Send("GET", impl_->target, headers, nullptr, deadline,
                        &prepared->stream);
    if (!reply.ok()) return TerminalError(command, reply, false);
    if (prepared->stream->Status() != 200 ||
        prepared->stream->Mime() != "text/event-stream")
      return TerminalError(command, Protocol(), false);
    prepared->events = std::make_unique<Events>(prepared->stream.get());
    std::string kind, data;
    reply = prepared->events->Next(&kind, &data);
    if (!reply.ok()) return TerminalError(command, reply, false);
    if (kind != "endpoint" ||
        !file_detail::Redirect(impl_->target, data, &prepared->post) ||
        !SameOrigin(impl_->target, prepared->post) ||
        !file_detail::AllowedUrl(prepared->post, impl_->network))
      return TerminalError(command,
                           Error(403, "mcp_endpoint_denied",
                                 "Private MCP endpoint is not permitted"),
                           false);
    reply =
        impl_->Send("POST", prepared->post, headers, rpc, deadline, &response);
    if (!reply.ok()) return TerminalError(command, reply, true);
    session = prepared.get();
  }
  if (session && session->stream) {
    std::string ack;
    reply = response->ReadAll(&ack);
    if (!reply.ok()) return TerminalError(command, reply, true);
    if (response->Status() != 202 || !ack.empty())
      return TerminalError(command, Protocol(), true);
  }
  // A remote can allocate a session before returning an invalid initialization.
  // Retain ownership until validation succeeds, and never delete a colliding
  // token that already belongs to another live local session.
  const std::string initialized_token(response->Header("mcp-session-id"));
  bool keep_initialized = false;
  ScopeExit release_failed_initialize([&] {
    if (!initialize || prepared || keep_initialized ||
        !Text(initialized_token, 1024) ||
        sessions.contains(Digest(authority + ':' + initialized_token)))
      return;
    Impl::Session failed;
    failed.post = post;
    failed.headers = headers;
    failed.headers["mcp-session-id"] = initialized_token;
    impl_->Close(failed);
  });
  Json message;
  reply =
      impl_->Read(command, session, *response, session ? session->post : post,
                  headers, deadline, progress, &message);
  if (!reply.ok()) {
    if (response->Status() == 404) {
      reply.http_status = 404;
      reply.code = "mcp_session_lost";
    }
    if (response->Status() == 404 && found != sessions.end())
      sessions.erase(found);
    auto failed = TerminalError(command, reply, true);
    Set(&failed.value, "resp_headers", ResponseHeaders(*response));
    return failed;
  }
  auto received = ResponseHeaders(*response);
  if (initialize && response->Status() >= 200 && response->Status() < 300 &&
      message.Find("result")) {
    const auto remote = response->Header("mcp-session-id");
    if (prepared || !remote.empty()) {
      if (!prepared && !Text(remote, 1024))
        return TerminalError(command, Protocol(), true);
      if (!prepared) {
        prepared = std::make_unique<Impl::Session>();
        prepared->post = post;
        prepared->token = remote;
      } else {
        prepared->token = detail::NewRequestId("q4t-agent-");
        if (!Text(prepared->token, 128))
          return TerminalError(command,
                               Error(503, "mcp_entropy_unavailable",
                                     "Private MCP ID unavailable"),
                               true);
      }
      prepared->headers = headers;
      const auto protocol =
          message.Find("result")->GetString("protocolVersion");
      if (!Text(protocol, 128)) {
        // The fallback stream owns no modern session. A newly prepared modern
        // session must be cleaned even though it has not entered the cache.
        if (!prepared->stream &&
            !sessions.contains(Digest(authority + ':' + prepared->token))) {
          prepared->headers["mcp-session-id"] = prepared->token;
          impl_->Close(*prepared);
        }
        return TerminalError(command, Protocol(), true);
      }
      prepared->headers["mcp-protocol-version"] = protocol;
      if (!prepared->stream)
        prepared->headers["mcp-session-id"] = prepared->token;
      prepared->authority = authority;
      prepared->created = prepared->touched = Clock::now();
      Set(&received, "mcp-session-id", Array({String(prepared->token)}));
      const auto identity = Digest(authority + ':' + prepared->token);
      if (sessions.contains(identity))
        return TerminalError(command, Protocol(), true);
      sessions.emplace(identity, std::move(prepared));
      keep_initialized = true;
    }
  }
  ObjectReply result;
  const int status = session && session->stream && !message.IsNull()
                         ? 200
                         : response->Status();
  result.value = Response(command, status, std::move(received),
                          message.IsNull() ? nullptr : &message);
  if (response->Status() == 404 && found != sessions.end())
    sessions.erase(found);
  retain_legacy = true;
  return result;
}
}  // namespace q4t::server::mcp_detail
