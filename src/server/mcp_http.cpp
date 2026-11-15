#include "mcp_transport_internal.h"

#include <sys/random.h>

#include <algorithm>
#include <set>

#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/mcp_content.h"
#include "remote_http_internal.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
using file_detail::RemoteHeaders;
using file_detail::RemoteHttp;
using file_detail::Url;
using Deadline = std::chrono::steady_clock::time_point;
using Clock = std::chrono::steady_clock;
constexpr uint64_t kResponseBytes = 4U << 20;
int64_t NowMillis() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}
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
    entropy_ = getrandom(&id_, sizeof(id_), 0) == sizeof(id_);
    id_ &= (1ULL << 52) - 1;
  }
  ObjectReply Initialize() {
    if (!entropy_)
      return Error(503, "mcp_entropy_unavailable", "MCP ID unavailable");
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
    tools_ = caps->Find("tools") && caps->Find("tools")->IsObject();
    resources_ = caps->Find("resources") && caps->Find("resources")->IsObject();
    headers_["MCP-Protocol-Version"] = protocol;
    Json notification =
        Object({{"jsonrpc", String("2.0")},
                {"method", String("notifications/initialized")}});
    return SendNotification(notification);
  }
  ObjectReply Capability(std::string_view method) const {
    const bool resource = method == "resources/read";
    if (resource ? resources_ : tools_) return {};
    return Error(
        502, resource ? "mcp_resources_unavailable" : "mcp_tools_unavailable",
        "Server lacks the requested MCP capability");
  }
  ObjectReply Restore(const Json& state) {
    const auto session = state.GetString("session_id");
    protocol = state.GetString("protocol");
    if (!entropy_ || state.GetBool("legacy") ||
        (protocol != "2025-06-18" && protocol != "2025-03-26" &&
         protocol != "2024-11-05") ||
        (!session.empty() && !Visible(session, 1024)) || !state.Find("tools") ||
        !state.Find("tools")->IsBool() || !state.Find("resources") ||
        !state.Find("resources")->IsBool())
      return Protocol();
    if (!session.empty()) headers_["Mcp-Session-Id"] = session;
    headers_["MCP-Protocol-Version"] = protocol;
    tools_ = state.GetBool("tools");
    resources_ = state.GetBool("resources");
    return {};
  }
  void Snapshot(Json* state) const {
    Set(state, "protocol", String(protocol));
    Set(state, "legacy", Boolean(Legacy()));
    Set(state, "tools", Boolean(tools_));
    Set(state, "resources", Boolean(resources_));
    const auto session = headers_.find("Mcp-Session-Id");
    Set(state, "session_id",
        String(session == headers_.end() ? "" : session->second));
  }
  void Begin(Deadline deadline) {
    deadline_ = deadline;
    exchanges_ = 0;
    if (legacy_) legacy_->BeginRead(deadline);
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
                       Clock::now() + std::chrono::milliseconds(100), &ignored);
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
      // A later independently authorized invocation may negotiate anew.
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
  FileFetchOptions options_;
  Deadline deadline_;
  Url url_;
  RemoteHeaders headers_;
  std::unique_ptr<RemoteHttp> legacy_;
  std::unique_ptr<Events> events_;
  uint64_t id_ = 0;
  int exchanges_ = 0;
  bool entropy_ = false, tools_ = false, resources_ = false;
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
static ObjectReply Perform(Client& client, std::string_view method,
                           const Json& params,
                           const std::function<bool()>& permit,
                           McpTransportReceipt* receipt) {
  auto reply = client.Capability(method);
  if (!reply.ok()) return reply;
  receipt->protocol = client.protocol;
  receipt->legacy_sse = client.Legacy();
  if (method == "tools/call" || method == "resources/read") {
    if (!permit || !permit())
      return Error(409, "mcp_call_not_permitted", "MCP dispatch not confirmed");
    receipt->call_permitted = receipt->outcome_unknown = true;
    reply = client.Invoke(method, params, &receipt->result, &receipt->error);
    if (!reply.ok()) return reply;
    // A matched complete RPC response is a known outcome even when its typed
    // content is unusable. Neither case authorizes replaying the operation.
    receipt->outcome_unknown = false;
    if (receipt->error.IsNull()) {
      auto checked = method == "tools/call"
                         ? ValidateMcpToolResult(receipt->result)
                         : ValidateMcpResourcesResult(receipt->result);
      if (!checked) return Protocol();
    }
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
ObjectReply Exchange(const McpEndpoint& endpoint,
                     const FileFetchOptions& deployment,
                     std::string_view method, const Json& params,
                     Deadline deadline, const std::function<bool()>& permit,
                     McpTransportReceipt* receipt) {
  Client client(endpoint, deployment, deadline);
  ScopeExit close([&] { client.Close(); });
  auto reply = client.Initialize();
  if (!reply.ok()) return reply;
  return Perform(client, method, params, permit, receipt);
}

struct Sessions::Impl {
  struct Entry {
    Json state;
    std::unique_ptr<Client> client;
    Deadline touched;
  };
  std::map<std::string, Entry> entries;
};
Sessions::Sessions() : impl_(std::make_unique<Impl>()) {}
Sessions::~Sessions() { Clear(); }
size_t Sessions::Size() const { return impl_->entries.size(); }
void Sessions::Clear(bool release_remote) {
  for (auto& [id, entry] : impl_->entries)
    if (release_remote && entry.client) entry.client->Close();
  impl_->entries.clear();
}
void Sessions::Collect() {
  const auto now = Clock::now();
  for (auto it = impl_->entries.begin(); it != impl_->entries.end();) {
    const auto& entry = it->second;
    if (now - entry.touched >=
            std::chrono::milliseconds(entry.state.GetInt("idle_ms")) ||
        NowMillis() >= entry.state.GetInt("expires_ms")) {
      if (entry.client) entry.client->Close();
      it = impl_->entries.erase(it);
    } else {
      ++it;
    }
  }
}
ObjectReply Sessions::Exchange(
    const McpEndpoint& endpoint, const FileFetchOptions& deployment,
    std::string_view method, const Json& params, Deadline deadline,
    Json* session, bool allow_new, int idle_ms, int lifetime_ms,
    const std::function<bool(const Json&)>& checkpoint,
    const std::function<bool()>& permit, McpTransportReceipt* receipt) {
  const auto invalid = [] {
    return Error(503, "mcp_session_invalid", "Invalid private MCP session");
  };
  const auto lost = [] {
    return Error(409, "mcp_session_lost", "Original MCP session unavailable");
  };
  if (!session || !session->IsObject() ||
      !session->GetString("id").starts_with("mcps_") ||
      !ValidId(session->GetString("id")) ||
      session->GetString("binding").size() != 64 ||
      session->GetString("scope").size() != 64 || idle_ms < 1 ||
      idle_ms > 300000 || lifetime_ms < 1 || lifetime_ms > 3600000 ||
      OutputJson(*session).size() > 4096)
    return invalid();
  Collect();
  auto& entries = impl_->entries;
  auto id = session->GetString("id");
  auto found = entries.find(id);
  const bool initialized = session->Find("protocol");
  const auto now = NowMillis();
  bool replace = false;
  if (found != entries.end()) {
    for (const auto* field :
         {"id", "binding", "scope", "protocol", "session_id", "legacy", "tools",
          "resources", "created_ms", "expires_ms", "idle_ms"}) {
      const auto* prior = found->second.state.Find(field);
      const auto* incoming = session->Find(field);
      if (!prior || !incoming || OutputJson(*prior) != OutputJson(*incoming))
        return invalid();
    }
  }
  if (initialized) {
    const auto created = session->GetInt("created_ms");
    const auto expires = session->GetInt("expires_ms");
    if (!created || expires <= created || expires - created > 3600000 ||
        session->GetInt("idle_ms") < 1 || session->GetInt("idle_ms") > 300000 ||
        session->GetInt("idle_expires_ms") < created ||
        session->GetInt("idle_expires_ms") > expires + 300000)
      return invalid();
    // Absolute expiry never extends on access or process restart. A legacy
    // connection cannot be reconstructed from a token after its owner dies.
    const auto idle_expires =
        found == entries.end() ? session->GetInt("idle_expires_ms")
                               : found->second.state.GetInt("idle_expires_ms");
    replace = now < created || now >= expires || now >= idle_expires ||
              session->GetBool("lost") ||
              (found != entries.end() && !found->second.client) ||
              (session->GetBool("legacy") && found == entries.end());
    if (replace && !allow_new) {
      Set(session, "lost", Boolean(true));
      return lost();
    }
  } else if (!allow_new) {
    return lost();
  }
  if (replace) {
    if (found != entries.end()) {
      if (found->second.client) found->second.client->Close();
      entries.erase(found);
    }
    id = detail::NewRequestId("mcps_");
    if (id.empty())
      return Error(503, "mcp_entropy_unavailable", "MCP ID unavailable");
    *session = Object({{"id", String(id)},
                       {"binding", *session->Find("binding")},
                       {"scope", *session->Find("scope")}});
    found = entries.end();
  }
  if (found == entries.end()) {
    const auto scope = session->GetString("scope");
    const auto count_scope = [&] {
      return std::count_if(entries.begin(), entries.end(), [&](const auto& e) {
        return e.second.state.GetString("scope") == scope;
      });
    };
    // Modern HTTP has no retained stream. Its authenticated snapshot can
    // restore the same remote session after cache eviction; do not DELETE it
    // while the catalog/call still owns it. Legacy connections stay bounded.
    while (entries.size() >= 16 || count_scope() >= 4) {
      auto victim = entries.end();
      for (auto it = entries.begin(); it != entries.end(); ++it) {
        if ((it->second.client && it->second.client->Legacy()) ||
            (count_scope() >= 4 &&
             it->second.state.GetString("scope") != scope))
          continue;
        if (victim == entries.end() ||
            it->second.touched < victim->second.touched)
          victim = it;
      }
      if (victim == entries.end())
        return Error(503, "mcp_session_capacity",
                     "MCP session capacity occupied");
      entries.erase(victim);
    }
    auto client = std::make_unique<Client>(endpoint, deployment, deadline);
    const bool resume = session->Find("protocol");
    auto reply = resume ? client->Restore(*session) : client->Initialize();
    if (!reply.ok()) {
      client->Close();
      return reply;
    }
    if (!resume) {
      Set(session, "created_ms", Number(now));
      Set(session, "expires_ms", Number(now + lifetime_ms));
      Set(session, "idle_ms", Number(idle_ms));
      client->Snapshot(session);
    }
    found =
        entries
            .emplace(id, Impl::Entry{*session, std::move(client), Clock::now()})
            .first;
  }
  auto& entry = found->second;
  entry.client->Begin(std::min(
      deadline,
      Clock::now() + std::chrono::milliseconds(std::max<int64_t>(
                         0, entry.state.GetInt("expires_ms") - NowMillis()))));
  Set(&entry.state, "idle_expires_ms",
      Number(NowMillis() + entry.state.GetInt("idle_ms")));
  *session = entry.state;
  if (!checkpoint || !checkpoint(entry.state))
    return Error(409, "mcp_session_not_confirmed", "MCP session not confirmed");
  // Publication may consume the remaining session lifetime. Do not ask for
  // tool permission after expiry; HTTP also uses the absolute session limit
  // if permission itself takes time to publish.
  if (NowMillis() >= entry.state.GetInt("expires_ms")) {
    entry.client->Close();
    entry.client.reset();
    Set(&entry.state, "lost", Boolean(true));
    *session = entry.state;
    return lost();
  }
  auto reply = Perform(*entry.client, method, params, permit, receipt);
  entry.touched = Clock::now();
  if (!reply.ok() && (entry.client->Legacy() ||
                      reply.value.GetInt("upstream_status") == 404)) {
    // No retry, including HTTP 404. The failed invocation keeps its original
    // unknown/known result. A subsequent independent operation may initialize
    // again, but a resource always requires its exact original session.
    entry.client->Close();
    entry.client.reset();
    Set(&entry.state, "lost", Boolean(true));
    *session = entry.state;
  }
  return reply;
}
}  // namespace q4t::server::mcp_detail
