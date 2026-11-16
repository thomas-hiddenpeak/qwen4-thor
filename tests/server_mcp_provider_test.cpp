#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <mutex>
#include <thread>

#include "../src/server/mcp_provider_internal.h"
#include "../src/server/mcp_transport_internal.h"
#include "../src/server/object_protocol.h"
#include "../src/server/remote_http_internal.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/responses_mcp.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
constexpr auto kProvider = "connector_googlecalendar";
Json Call(std::string name, Json args = Object()) {
  return Object(
      {{"name", String(std::move(name))}, {"arguments", std::move(args)}});
}
Json Events() {
  return Object(
      {{"items",
        Array(
            {Object({{"id", String("event_original")},
                     {"summary", String("项目验收")},
                     {"start", Object({{"date", String("2026-11-16")}})},
                     {"end", Object({{"date", String("2026-11-17")}})},
                     {"htmlLink",
                      String("https://calendar.google.com/event?eid=original")},
                     {"description", String("Project readiness record")}})})},
       {"nextPageToken", String("opaque+/=cursor")}});
}
struct Reply {
  int status = 200;
  std::string body;
  bool omit_body = false;
};
class ProviderPeer {
 public:
  explicit ProviderPeer(std::vector<Reply> replies)
      : replies_(std::move(replies)) {
    fd_ = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    socklen_t size = sizeof(address);
    if (fd_ < 0 || bind(fd_, reinterpret_cast<sockaddr*>(&address), size) ||
        getsockname(fd_, reinterpret_cast<sockaddr*>(&address), &size) ||
        listen(fd_, 4))
      throw std::runtime_error("provider fixture listener");
    port_ = ntohs(address.sin_port);
    thread_ = std::jthread([this](std::stop_token stop) {
      while (!stop.stop_requested()) {
        pollfd event{fd_, POLLIN, 0};
        if (poll(&event, 1, 10) <= 0) continue;
        const int client = accept4(fd_, nullptr, nullptr, SOCK_CLOEXEC);
        if (client < 0) continue;
        timeval timeout{2, 0};
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        std::string request;
        char buffer[1024];
        while (request.size() <= 32768 &&
               request.find("\r\n\r\n") == request.npos) {
          const auto got = recv(client, buffer, sizeof(buffer), 0);
          if (got <= 0) break;
          request.append(buffer, got);
        }
        Reply reply;
        {
          std::lock_guard lock(mutex_);
          const auto index = requests_.size();
          requests_.push_back(request);
          reply = index < replies_.size() ? replies_[index] : Reply{500, "{}"};
        }
        std::string output =
            "HTTP/1.1 " + std::to_string(reply.status) +
            " Result\r\nContent-Type: application/json\r\nContent-Length: " +
            std::to_string(reply.body.size()) +
            "\r\nConnection: close\r\n\r\n" +
            (reply.omit_body ? "" : reply.body);
        std::string_view pending = output;
        while (!pending.empty()) {
          const auto sent =
              send(client, pending.data(), pending.size(), MSG_NOSIGNAL);
          if (sent <= 0) break;
          pending.remove_prefix(sent);
        }
        close(client);
      }
    });
  }
  ~ProviderPeer() {
    thread_.request_stop();
    thread_.join();
    close(fd_);
  }
  std::vector<std::string> Requests() {
    std::lock_guard lock(mutex_);
    return requests_;
  }
  ObjectReply Fetch(const mcp_detail::ProviderRequest& request, Json* output) {
    if (!mcp_detail::ProviderOrigin(kProvider, request.url))
      return Error(403, "fixture_origin", "Unexpected adapter origin");
    file_detail::Url original, local;
    file_detail::ParseUrl(request.url, &original);
    file_detail::ParseUrl(
        "http://127.0.0.1:" + std::to_string(port_) + original.target, &local);
    FileFetchOptions options;
    options.public_networks = false;
    options.allowed_networks = {"127.0.0.1/32"};
    options.ports = {port_};
    options.max_redirects = 0;
    file_detail::RemoteHeaders headers{{"Accept", "application/json"}};
    if (request.token_query)
      local.target += "?access_token=synthetic-private-token";
    else
      headers["Authorization"] = "Bearer synthetic-private-token";
    std::unique_ptr<file_detail::RemoteHttp> response;
    auto reply = file_detail::RemoteHttp::Open(
        local, options, "GET", headers, "", (4U << 20) - bytes_,
        std::chrono::steady_clock::now() + 2s, &response);
    return reply.ok()
               ? mcp_detail::ReadProviderResponse(*response, &bytes_, output)
               : reply;
  }

 private:
  int fd_ = -1, port_ = 0;
  uint64_t bytes_ = 0;
  std::mutex mutex_;
  std::vector<Reply> replies_;
  std::vector<std::string> requests_;
  std::jthread thread_;
};
}  // namespace

Q4T_TEST(mcp_provider_fixed_origins_and_opaque_encoding) {
  for (const auto* url :
       {"https://www.googleapis.com/calendar/v3/calendars/primary",
        "https://oauth2.googleapis.com/tokeninfo",
        "https://openidconnect.googleapis.com/v1/userinfo"})
    Q4T_CHECK(mcp_detail::ProviderOrigin(kProvider, url));
  for (const auto* url :
       {"http://www.googleapis.com/calendar", "https://127.0.0.1/",
        "https://www.googleapis.com.attacker.invalid/",
        "https://www.googleapis.com:8443/", "file:///private"})
    Q4T_CHECK(!mcp_detail::ProviderOrigin(kProvider, url));
  Q4T_CHECK(mcp_detail::ProviderEscape("a/b?x=%20&z=+#") ==
            "a%2Fb%3Fx%3D%2520%26z%3D%2B%23");
  return true;
}
Q4T_TEST(mcp_provider_calendar_scoped_catalog_actual_http) {
  for (const auto& [scope, expected] :
       std::vector<std::pair<std::string, size_t>>{
           {"https://www.googleapis.com/auth/calendar.events", 4},
           {"openid", 1},
           {"unrelated", 0},
           {"openid https://www.googleapis.com/auth/calendar.readonly", 5}}) {
    ProviderPeer peer({{200, OutputJson(Object({{"scope", String(scope)}}))}});
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  kProvider, "tools/list", Object(),
                  [&](const auto& request, Json* out) {
                    return peer.Fetch(request, out);
                  },
                  &result, &error)
                  .ok());
    Q4T_CHECK(error.IsNull() && result.GetArray("tools") &&
              result.GetArray("tools")->array.size() == expected);
    const auto requests = peer.Requests();
    Q4T_CHECK(requests.size() == 1 &&
              requests[0].starts_with("GET /tokeninfo?") &&
              requests[0].find("Authorization:") == requests[0].npos);
    Q4T_CHECK(OutputJson(result).find("synthetic-private-token") ==
              std::string::npos);
    for (const auto& tool : result.GetArray("tools")->array)
      Q4T_CHECK(tool.Find("inputSchema")->GetString("type") == "object" &&
                tool.Find("annotations")->GetBool("readOnlyHint"));
  }
  return true;
}
Q4T_TEST(mcp_provider_calendar_search_actual_http_cursor_and_original_data) {
  ProviderPeer peer({{200, OutputJson(Events())}});
  Json result, error;
  auto args = Object({{"query", String("report & owners")},
                      {"calendar_id", String("team@example.com")},
                      {"next_page_token", String("cursor+/=%20")},
                      {"max_results", Number(2)},
                      {"time_min", String("2026-11-16T08:00:00+08:00")}});
  Q4T_CHECK(mcp_detail::InvokeProvider(
                kProvider, "tools/call", Call("search_events", args),
                [&](const auto& request, Json* out) {
                  return peer.Fetch(request, out);
                },
                &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  const auto requests = peer.Requests();
  Q4T_CHECK(requests.size() == 1);
  const auto& request = requests[0];
  Q4T_CHECK(request.find("/calendars/team%40example.com/events?") !=
                request.npos &&
            request.find("pageToken=cursor%2B%2F%3D%2520") != request.npos &&
            request.find("timeMin=2026-11-16T00%3A00%3A00Z") != request.npos &&
            request.find("q=report%20%26%20owners") != request.npos &&
            request.find("Authorization: Bearer synthetic-private-token") !=
                request.npos);
  const auto* value = result.Find("structuredContent");
  Q4T_CHECK(value && value->GetString("next_page_token") == "opaque+/=cursor");
  const auto& event = value->GetArray("events")->array.front();
  Q4T_CHECK(event.GetString("id") == "event_original" &&
            event.GetString("start") == "2026-11-16" &&
            event.Find("start_details")->GetString("date") == "2026-11-16" &&
            event.GetString("display_title") == "项目验收");
  return true;
}
Q4T_TEST(mcp_provider_calendar_naive_window_uses_original_calendar_timezone) {
  ProviderPeer peer(
      {{200, R"({"timeZone":"Asia/Shanghai"})"}, {200, OutputJson(Events())}});
  Json result, error;
  Q4T_CHECK(
      mcp_detail::InvokeProvider(
          kProvider, "tools/call",
          Call("search", Object({{"time_min", String("2026-11-16T00:00:00")},
                                 {"time_max", String("2026-11-17T00:00:00")}})),
          [&](const auto& request, Json* out) {
            return peer.Fetch(request, out);
          },
          &result, &error)
          .ok());
  Q4T_CHECK(error.IsNull());
  const auto requests = peer.Requests();
  Q4T_CHECK(requests.size() == 2 &&
            requests[0].starts_with("GET "
                                    "/calendar/v3/calendars/primary/"
                                    "events?maxResults=1&fields=timeZone ") &&
            requests[1].find("timeMin=2026-11-15T16%3A00%3A00Z") !=
                std::string::npos &&
            requests[1].find("timeMax=2026-11-16T16%3A00%3A00Z") !=
                std::string::npos);
  return true;
}
Q4T_TEST(mcp_provider_calendar_time_offsets_fraction_and_wide_years) {
  std::string utc;
  for (const auto& [value, expected] :
       std::vector<std::pair<std::string, std::string>>{
           {"2026-11-16T01:00:00-07:00", "2026-11-16T08:00:00Z"},
           {"2026-11-16T01:00:00.123456789Z", "2026-11-16T01:00:00.123456789Z"},
           {"2500-01-01T00:00:00Z", "2500-01-01T00:00:00Z"}}) {
    Q4T_CHECK(mcp_detail::ProviderCalendarTime(value, "", &utc).ok());
    Q4T_CHECK(utc == expected);
  }
  Q4T_CHECK(mcp_detail::ProviderCalendarTime("2026-07-01T12:00:00",
                                             "America/Los_Angeles", &utc)
                .ok() &&
            utc == "2026-07-01T19:00:00Z");
  Q4T_CHECK(
      mcp_detail::ProviderCalendarTime("2026-01-01", "Asia/Shanghai", &utc)
          .ok() &&
      utc == "2025-12-31T16:00:00Z");
  return true;
}
Q4T_TEST(mcp_provider_calendar_ambiguous_nonexistent_invalid_time_rejected) {
  std::string utc;
  for (const auto* value :
       {"2026-11-01T01:30:00", "2026-03-08T02:30:00", "2026-02-30T00:00:00",
        "2026-01-01T24:01:00", "2026-01-01T12:00:00Z trailing",
        "2026-01-01T12:00:00.Z", "2026-01-01T12:00:00+25:00",
        "2026-01-01T12:00:00+01:60"})
    Q4T_CHECK(
        !mcp_detail::ProviderCalendarTime(value, "America/Los_Angeles", &utc)
             .ok());
  Q4T_CHECK(!mcp_detail::ProviderCalendarTime("2026-01-01T00:00:00",
                                              "../private", &utc)
                 .ok());
  return true;
}
Q4T_TEST(mcp_provider_calendar_invalid_arguments_never_fetch) {
  int requests = 0;
  const auto fetch = [&](const auto&, Json*) {
    ++requests;
    return ObjectReply{};
  };
  Json fractional;
  Q4T_CHECK(Parse("1.5", &fractional));
  for (const auto& args :
       {Object({{"max_results", fractional}}),
        Object({{"max_results", Number(0)}}),
        Object({{"calendar_id", String("..")}}),
        Object({{"query", Boolean(true)}}),
        Object({{"time_min", String("invalid-date")}}),
        Object({{"time_min", String("2026-02-30T00:00:00")}}),
        Object({{"time_min", String("2026-01-01T00:00:00")},
                {"time_max", String("invalid-date")}}),
        Object({{"url", String("http://127.0.0.1/private")}})}) {
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(kProvider, "tools/call",
                                         Call("search", args), fetch, &result,
                                         &error)
                  .ok() &&
              error.GetInt("code") == -32602);
  }
  for (const auto* name : {"read_event", "fetch", "unknown", "get_profile"}) {
    Json result, error;
    Q4T_CHECK(
        mcp_detail::InvokeProvider(kProvider, "tools/call",
                                   Call(name, Object({{"event_id", Json()}})),
                                   fetch, &result, &error)
            .ok() &&
        error.GetInt("code") == -32602);
  }
  Q4T_CHECK(requests == 0);
  return true;
}
Q4T_TEST(mcp_provider_actual_http_errors_known_redacted_and_never_retried) {
  for (int status : {400, 401, 403, 404, 429, 500, 503}) {
    ProviderPeer peer(
        {{status, R"({"error":"synthetic-private-token"})", true}});
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  kProvider, "tools/call", Call("get_profile"),
                  [&](const auto& request, Json* out) {
                    return peer.Fetch(request, out);
                  },
                  &result, &error)
                  .ok());
    Q4T_CHECK(error.Find("data") &&
              error.Find("data")->GetInt("upstream_status") == status &&
              OutputJson(error).find("synthetic-private-token") ==
                  std::string::npos &&
              peer.Requests().size() == 1 && result.IsNull());
  }
  return true;
}
Q4T_TEST(
    mcp_provider_malformed_and_incomplete_responses_do_not_become_success) {
  for (const auto* body :
       {"[]", R"({"items":null})", R"({"items":[{}]})",
        R"({"nextPageToken":42})", R"({"items":[],"items":[]})"}) {
    ProviderPeer peer({{200, body}});
    Json result, error;
    Q4T_CHECK(!mcp_detail::InvokeProvider(
                   kProvider, "tools/call", Call("search"),
                   [&](const auto& request, Json* out) {
                     return peer.Fetch(request, out);
                   },
                   &result, &error)
                   .ok());
    Q4T_CHECK(result.IsNull() && peer.Requests().size() == 1);
  }
  return true;
}
Q4T_TEST(mcp_provider_deployment_authority_and_transport_binding) {
  McpDeploymentPolicy policy;
  const std::string raw = R"({"version":1,"targets":[{"kind":"connector_id",
    "id":"connector_googlecalendar","transport":"provider","generation":"v1",
    "scopes":["client:a"]}]})";
  Q4T_CHECK(ParseMcpDeploymentPolicy(raw, &policy));
  Q4T_CHECK(McpDeploymentPolicyDescription(&policy).GetInt(
                "native_provider_count") == 1);
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesMcpDefinition(
      Object({{"type", String("mcp")},
              {"server_label", String("Calendar")},
              {"connector_id", String(kProvider)},
              {"authorization", String("synthetic-private-token")}}),
      &definition, &error));
  Q4T_CHECK(ResolveMcpDeploymentTarget(&policy, "client:a", &definition).ok());
  Q4T_CHECK(definition.credentials.provider_id == kProvider &&
            definition.credentials.url.empty() &&
            definition.credentials.tunnel_id.empty());
  const auto binding = definition.target_binding;
  Q4T_CHECK(ResolveMcpDeploymentTarget(&policy, "client:b", &definition)
                .http_status == 403);
  Q4T_CHECK(definition.credentials.provider_id.empty() &&
            definition.target_binding.empty());
  policy.targets[0].generation = "v2";
  Q4T_CHECK(ResolveMcpDeploymentTarget(&policy, "client:a", &definition).ok() &&
            definition.target_binding != binding);
  Json changed;
  Q4T_CHECK(Parse(raw, &changed));
  Set(&Field(changed, "targets").array[0], "url",
      String("https://attacker.invalid"));
  Q4T_CHECK(!ParseMcpDeploymentPolicy(OutputJson(changed), &policy));
  return true;
}
Q4T_TEST(mcp_provider_missing_credentials_and_cancel_prevent_worker_admission) {
  McpEndpoint endpoint;
  endpoint.provider_id = kProvider;
  FileFetchOptions network;
  network.max_redirects = 0;
  Q4T_CHECK(mcp_detail::CheckEndpoint(endpoint, network).code ==
            "mcp_credentials_required");
  endpoint.authorization = "synthetic-private-token";
  endpoint.url = "https://attacker.invalid";
  Q4T_CHECK(mcp_detail::CheckEndpoint(endpoint, network).code ==
            "mcp_provider_invalid");
  endpoint.url.clear();
  McpTransportReceipt receipt;
  int workers = 0, permits = 0;
  GrammarControl control;
  control.worker_started = [&] { ++workers; };
  BinaryAccess access{"client:a", std::chrono::steady_clock::now() + 2s,
                      [] { return true; }};
  Q4T_CHECK(InvokeMcp(
                access, endpoint, network, "tools/call", Call("get_profile"),
                [&] {
                  ++permits;
                  return ObjectReply{};
                },
                &receipt, control)
                .http_status == 499);
  Q4T_CHECK(workers == 0 && permits == 0 && !receipt.call_permitted);
  return true;
}

Q4T_TEST(mcp_provider_calendar_event_aliases_keep_identifiers_and_fields) {
  for (const auto* name : {"fetch", "read_event"}) {
    const auto event = Events().GetArray("items")->array.front();
    ProviderPeer peer({{200, OutputJson(event)}});
    Json result, error;
    Q4T_CHECK(
        mcp_detail::InvokeProvider(
            kProvider, "tools/call",
            Call(name, Object({{"event_id", String("event/a?b")},
                               {"timezone_str", String("Asia/Shanghai")}})),
            [&](const auto& request, Json* out) {
              return peer.Fetch(request, out);
            },
            &result, &error)
            .ok());
    Q4T_CHECK(error.IsNull() &&
              result.Find("structuredContent")->GetString("id") ==
                  "event_original");
    const auto requests = peer.Requests();
    Q4T_CHECK(
        requests.size() == 1 &&
        requests[0].find("/events/event%2Fa%3Fb?timeZone=Asia%2FShanghai") !=
            std::string::npos);
  }
  return true;
}
Q4T_TEST(mcp_provider_redirect_and_transport_failure_never_retry_or_fabricate) {
  ProviderPeer peer({{302, "{}"}});
  Json result, error;
  auto reply = mcp_detail::InvokeProvider(
      kProvider, "tools/call", Call("get_profile"),
      [&](const auto& request, Json* out) { return peer.Fetch(request, out); },
      &result, &error);
  Q4T_CHECK(reply.ok() && result.IsNull() && error.Find("data") &&
            error.Find("data")->GetInt("upstream_status") == 302 &&
            peer.Requests().size() == 1);
  int calls = 0;
  reply = mcp_detail::InvokeProvider(
      kProvider, "tools/call", Call("get_profile"),
      [&](const auto&, Json*) {
        ++calls;
        return Error(503, "mcp_peer_lost", "Provider connection lost");
      },
      &result, &error);
  Q4T_CHECK(!reply.ok() && calls == 1 && result.IsNull() && error.IsNull());
  return true;
}
Q4T_TEST(mcp_provider_shared_response_budget_never_accepts_partial_data) {
  std::unique_ptr<file_detail::RemoteHttp> response;
  Q4T_CHECK(file_detail::RemoteHttp::Buffered(
                200, {{"Content-Type", "application/json"}},
                R"({"value":"original"})", &response)
                .ok());
  uint64_t bytes = (4U << 20) - 1;
  Json value;
  Q4T_CHECK(
      mcp_detail::ReadProviderResponse(*response, &bytes, &value).http_status ==
      413);
  Q4T_CHECK(value.IsNull() && bytes == (4U << 20) - 1);
  return true;
}
