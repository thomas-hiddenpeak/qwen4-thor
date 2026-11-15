#include "mcp_transport_internal.h"

#include <set>

#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/lifetime.h"
#include "remote_http_internal.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
using file_detail::RemoteHeaders;
using file_detail::RemoteHttp;
using file_detail::Url;
using Deadline = std::chrono::steady_clock::time_point;
constexpr uint64_t kResponseBytes = 4U << 20;
ObjectReply Protocol() {
  return Error(502, "mcp_protocol_error", "Invalid MCP server response");
}
ObjectReply Capacity() {
  return Error(413, "mcp_response_capacity", "MCP response budget exceeded");
}
ObjectReply HttpError(int status) {
  auto reply = Error(502, "mcp_http_error", "MCP server HTTP error");
  reply.value = Object({{"upstream_status", Number(status)}});
  return reply;
}
bool SameOrigin(const Url& a, const Url& b) {
  return a.scheme == b.scheme && a.host == b.host && a.port == b.port;
}
bool ReservedHeader(std::string_view name) {
  for (const auto reserved :
       {"host", "content-length", "transfer-encoding", "connection", "expect",
        "trailer", "upgrade", "content-type", "accept", "accept-encoding",
        "user-agent", "proxy-authorization", "proxy-connection",
        "mcp-session-id", "mcp-protocol-version", "last-event-id"})
    if (name == reserved) return true;
  return false;
}
bool Visible(std::string_view value, size_t max) {
  if (value.empty() || value.size() > max) return false;
  for (unsigned char c : value)
    if (c < 0x21 || c > 0x7e) return false;
  return true;
}

// Reads complete events but does not wait for EOF after an RPC response.
// LF, CRLF and CR are accepted, with a single optional initial UTF-8 BOM.
class Events {
 public:
  explicit Events(RemoteHttp* http) : http_(http) {}
  ObjectReply Next(std::string* kind, std::string* data) {
    kind->clear();
    data->clear();
    for (;;) {
      std::string line;
      auto reply = Line(&line);
      if (!reply.ok()) return reply;
      if (first_) {
        first_ = false;
        if (line.starts_with("\xef\xbb\xbf")) line.erase(0, 3);
      }
      if (line.empty()) {
        if (!data->empty()) {
          data->pop_back();
          if (kind->empty()) *kind = "message";
          return {};
        }
        kind->clear();
        continue;
      }
      if (line.front() == ':') continue;
      const auto colon = line.find(':');
      const auto field = line.substr(0, colon);
      auto value = colon == line.npos
                       ? std::string_view{}
                       : std::string_view(line).substr(colon + 1);
      if (value.starts_with(' ')) value.remove_prefix(1);
      if (field == "event") {
        if (value.size() > 128) return Capacity();
        *kind = value;
      } else if (field == "data") {
        if (data->size() + value.size() + 1 > (1U << 20)) return Capacity();
        data->append(value);
        data->push_back('\n');
      }
    }
  }

 private:
  ObjectReply Line(std::string* line) {
    line->clear();
    for (;;) {
      if (index_ == bytes_.size()) {
        bool eof = false;
        auto reply = http_->Read(&bytes_, &eof);
        if (!reply.ok()) return reply;
        index_ = 0;
        if (eof) return Error(502, "mcp_stream_truncated", "MCP stream ended");
      }
      const char c = bytes_[index_++];
      if (skip_lf_) {
        skip_lf_ = false;
        if (c == '\n') continue;
      }
      if (c == '\r' || c == '\n') {
        skip_lf_ = c == '\r';
        return {};
      }
      if (line->size() == (1U << 20)) return Capacity();
      line->push_back(c);
    }
  }
  RemoteHttp* http_;
  std::string bytes_;
  size_t index_ = 0;
  bool skip_lf_ = false, first_ = true;
};

class Client {
 public:
  Client(const McpEndpoint& endpoint, const FileFetchOptions& options,
         Deadline deadline)
      : options_(options), deadline_(deadline) {
    file_detail::ParseUrl(endpoint.url, &url_);
    headers_ = endpoint.headers;
    if (!endpoint.authorization.empty())
      headers_["Authorization"] = "Bearer " + endpoint.authorization;
    headers_["Accept"] = "application/json, text/event-stream";
    headers_["Content-Type"] = "application/json";
  }
  ObjectReply Initialize() {
    auto params =
        Object({{"protocolVersion", String("2025-06-18")},
                {"capabilities", Object()},
                {"clientInfo",
                 Object({{"name", String("q4t")}, {"version", String("1")}})}});
    const auto init = Request("initialize", params);
    std::unique_ptr<RemoteHttp> response;
    auto reply = Open("POST", url_, OutputJson(init), &response);
    if (!reply.ok()) return reply;
    if (response->Status() == 400 || response->Status() == 404 ||
        response->Status() == 405 || response->Status() == 415) {
      response.reset();
      reply = Open("GET", url_, "", &legacy_);
      if (!reply.ok()) return reply;
      if (legacy_->Status() != 200) return HttpError(legacy_->Status());
      if (legacy_->Mime() != "text/event-stream") return Protocol();
      events_ = std::make_unique<Events>(legacy_.get());
      std::string kind, data;
      reply = events_->Next(&kind, &data);
      if (!reply.ok()) return reply;
      Url post;
      if (kind != "endpoint" || !file_detail::Redirect(url_, data, &post) ||
          !SameOrigin(url_, post) || !file_detail::AllowedUrl(post, options_))
        return Error(403, "mcp_endpoint_denied",
                     "MCP endpoint is not permitted");
      url_ = std::move(post);
      reply = Send(init, &response);
      if (!reply.ok()) return reply;
    } else {
      if (response->Status() != 200) return HttpError(response->Status());
      const auto session = response->Header("mcp-session-id");
      if (response->HasHeader("mcp-session-id")) {
        if (!Visible(session, 1024)) return Protocol();
        headers_["Mcp-Session-Id"] = session;
      }
    }
    Json result, error;
    reply = Receive(init.GetInt("id"), response.get(), &result, &error);
    if (!reply.ok()) return reply;
    if (!error.IsNull() || !result.IsObject()) return Protocol();
    protocol = result.GetString("protocolVersion");
    if (protocol != "2025-06-18" && protocol != "2025-03-26" &&
        protocol != "2024-11-05")
      return Error(502, "mcp_protocol_version", "No common MCP version");
    const auto* caps = result.Find("capabilities");
    const auto* info = result.Find("serverInfo");
    if (!caps || !caps->IsObject() || !info || !info->IsObject() ||
        !info->Find("name") || !info->Find("name")->IsString() ||
        !info->Find("version") || !info->Find("version")->IsString())
      return Protocol();
    const auto* tools = caps->Find("tools");
    if (!tools || !tools->IsObject())
      return Error(502, "mcp_tools_unavailable",
                   "Server has no tools capability");
    headers_["MCP-Protocol-Version"] = protocol;
    Json notification =
        Object({{"jsonrpc", String("2.0")},
                {"method", String("notifications/initialized")}});
    return SendNotification(notification);
  }
  ObjectReply Invoke(std::string_view method, const Json& params, Json* result,
                     Json* error) {
    const auto request = Request(method, params);
    std::unique_ptr<RemoteHttp> response;
    auto reply = Send(request, &response);
    if (!reply.ok()) return reply;
    return Receive(request.GetInt("id"), response.get(), result, error);
  }
  bool Legacy() const { return legacy_ != nullptr; }
  // A best-effort session release has its own short deadline. It cannot change
  // the known tool outcome or retry any operation on an expired session.
  void Close() {
    if (Legacy() || !headers_.contains("Mcp-Session-Id")) return;
    std::unique_ptr<RemoteHttp> ignored;
    try {
      RemoteHttp::Open(url_, options_, "DELETE", headers_, "", 0,
                       std::min(deadline_, std::chrono::steady_clock::now() +
                                               std::chrono::milliseconds(100)),
                       &ignored);
    } catch (...) {
    }
  }
  std::string protocol;

 private:
  Json Request(std::string_view method, const Json& params) {
    return Object({{"jsonrpc", String("2.0")},
                   {"id", Number(++id_)},
                   {"method", String(std::string(method))},
                   {"params", params}});
  }
  ObjectReply Open(std::string_view method, const Url& url,
                   std::string_view body,
                   std::unique_ptr<RemoteHttp>* response) {
    if (++exchanges_ > 128) return Capacity();
    return RemoteHttp::Open(url, options_, method, headers_, body,
                            kResponseBytes, deadline_, response);
  }
  ObjectReply Send(const Json& message, std::unique_ptr<RemoteHttp>* response) {
    auto reply = Open("POST", url_, OutputJson(message), response);
    if (!reply.ok()) return reply;
    if (Legacy()) {
      if ((*response)->Status() != 202) return HttpError((*response)->Status());
      std::string ignored;
      reply = (*response)->ReadAll(&ignored);
      if (!reply.ok()) return reply;
      if (!ignored.empty()) return Protocol();
    } else if ((*response)->Status() != 200) {
      // Never resubmit a tools/call after ambiguous failure or session expiry.
      // This short-lived session is discarded; a later distinct invocation
      // negotiates a new one before any independently authorized call.
      return HttpError((*response)->Status());
    }
    return {};
  }
  ObjectReply SendNotification(const Json& message) {
    std::unique_ptr<RemoteHttp> response;
    auto reply = Open("POST", url_, OutputJson(message), &response);
    if (!reply.ok()) return reply;
    if (response->Status() != 202) return HttpError(response->Status());
    std::string body;
    reply = response->ReadAll(&body);
    if (!reply.ok()) return reply;
    return body.empty() ? ObjectReply{} : Protocol();
  }
  ObjectReply Receive(int64_t expected, RemoteHttp* response, Json* result,
                      Json* error) {
    *result = Json{};
    *error = Json{};
    const bool stream = Legacy() || response->Mime() == "text/event-stream";
    std::unique_ptr<Events> local;
    if (stream && !Legacy()) local = std::make_unique<Events>(response);
    if (!stream && response->Mime() != "application/json") return Protocol();
    for (int messages = 0; messages < 256; ++messages) {
      std::string body;
      ObjectReply reply;
      if (stream) {
        std::string kind;
        reply = (Legacy() ? events_.get() : local.get())->Next(&kind, &body);
        if (reply.ok() && kind != "message") return Protocol();
      } else {
        reply = response->ReadAll(&body);
      }
      if (!reply.ok()) return reply;
      Json message;
      if (!Parse(body, &message) || !message.IsObject() ||
          message.GetString("jsonrpc") != "2.0")
        return Protocol();
      const auto* method = message.Find("method");
      if (method) {
        if (!stream || !method->IsString() || method->str.empty() ||
            message.Find("result") || message.Find("error"))
          return Protocol();
        const auto* id = message.Find("id");
        if (id) {
          if (!(id->IsString() || id->IsNumber())) return Protocol();
          Json answer = Object({{"jsonrpc", String("2.0")}, {"id", *id}});
          if (method->str == "ping")
            Set(&answer, "result", Object());
          else
            Set(&answer, "error",
                Object({{"code", Number(-32601)},
                        {"message", String("Method not available")}}));
          reply = SendNotification(answer);
          if (!reply.ok()) return reply;
        }
        continue;
      }
      const auto* id = message.Find("id");
      if (!id || !id->IsNumber() || message.GetInt("id", -1) != expected)
        return Protocol();
      const auto* value = message.Find("result");
      const auto* failed = message.Find("error");
      if (bool(value) == bool(failed)) return Protocol();
      if (value) {
        if (!value->IsObject()) return Protocol();
        *result = *value;
      } else {
        if (!failed->IsObject() || !failed->Find("code") ||
            !failed->Find("code")->IsNumber() || !failed->Find("message") ||
            !failed->Find("message")->IsString())
          return Protocol();
        *error = *failed;
      }
      return {};
    }
    return Capacity();
  }
  const FileFetchOptions& options_;
  Deadline deadline_;
  Url url_;
  RemoteHeaders headers_;
  std::unique_ptr<RemoteHttp> legacy_;
  std::unique_ptr<Events> events_;
  uint64_t id_ = 0;
  int exchanges_ = 0;
};
}  // namespace

ObjectReply CheckEndpoint(const McpEndpoint& endpoint,
                          const FileFetchOptions& deployment) {
  Url url;
  if (!file_detail::ParseUrl(endpoint.url, &url))
    return Error(400, "mcp_url_invalid", "Invalid MCP HTTP(S) endpoint");
  if (!file_detail::ValidOptions(deployment))
    return Error(500, "mcp_policy_invalid", "Invalid MCP deployment policy");
  if (!file_detail::AllowedUrl(url, deployment))
    return Error(403, "mcp_target_denied", "MCP target is not permitted");
  return CheckHeaders(endpoint);
}
ObjectReply CheckHeaders(const McpEndpoint& endpoint) {
  size_t total = endpoint.authorization.size();
  if (!endpoint.authorization.empty() && !Visible(endpoint.authorization, 8192))
    return Error(400, "mcp_headers_invalid", "Invalid MCP authorization");
  std::set<std::string> names;
  for (const auto& [key, value] : endpoint.headers) {
    std::string name;
    std::string_view parsed;
    const std::string line = key + ": " + value;
    total += line.size() + 2;
    if (total > 16384 || endpoint.headers.size() > 64 ||
        !http::Field(line, &name, &parsed) || name != http::Lower(key) ||
        ReservedHeader(name) || !names.insert(name).second ||
        (name == "authorization" && !endpoint.authorization.empty()))
      return Error(400, "mcp_headers_invalid", "Invalid MCP headers");
  }
  return {};
}
ObjectReply Exchange(const McpEndpoint& endpoint,
                     const FileFetchOptions& deployment,
                     std::string_view method, const Json& params,
                     Deadline deadline, const std::function<bool()>& permit,
                     McpTransportReceipt* receipt) {
  Client client(endpoint, deployment, deadline);
  ScopeExit close([&] { client.Close(); });
  auto reply = client.Initialize();
  if (!reply.ok()) return reply;
  receipt->protocol = client.protocol;
  receipt->legacy_sse = client.Legacy();
  if (method == "tools/call") {
    if (!permit || !permit())
      return Error(409, "mcp_call_not_permitted", "MCP dispatch not confirmed");
    receipt->call_permitted = receipt->outcome_unknown = true;
    reply = client.Invoke(method, params, &receipt->result, &receipt->error);
    if (!reply.ok()) return reply;
    if (receipt->error.IsNull()) {
      const auto* content = receipt->result.Find("content");
      const auto* structured = receipt->result.Find("structuredContent");
      const auto* error = receipt->result.Find("isError");
      if (!content || !content->IsArray() ||
          (structured && !structured->IsObject()) ||
          (error && !error->IsBool()))
        return Protocol();
    }
    receipt->outcome_unknown = false;
    return {};
  }
  auto tools = Array();
  std::set<std::string> cursors, names;
  Json page_params = params;
  for (int page = 0; page < 32; ++page) {
    Json result;
    reply = client.Invoke(method, page_params, &result, &receipt->error);
    if (!reply.ok() || !receipt->error.IsNull()) return reply;
    const auto* list = result.GetArray("tools");
    if (!list) return Protocol();
    for (const auto& tool : list->array) {
      const auto* name = tool.Find("name");
      const auto* schema = tool.Find("inputSchema");
      if (!tool.IsObject() || !name || !name->IsString() || name->str.empty() ||
          name->str.size() > 256 || !names.insert(name->str).second ||
          !schema || !schema->IsObject())
        return Protocol();
      tools.array.push_back(tool);
      if (tools.array.size() > 256 || OutputJson(tools).size() > (1U << 20))
        return Capacity();
    }
    const auto* cursor = result.Find("nextCursor");
    if (!cursor) {
      receipt->result = Object({{"tools", std::move(tools)}});
      return {};
    }
    if (!cursor->IsString() || cursor->str.empty() ||
        cursor->str.size() > 8192 || !cursors.insert(cursor->str).second)
      return Protocol();
    page_params = Object({{"cursor", *cursor}});
  }
  return Capacity();
}
}  // namespace q4t::server::mcp_detail
