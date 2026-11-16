#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <charconv>
#include <mutex>
#include <thread>
#include <tuple>

#include "../src/server/mcp_provider_internal.h"
#include "../src/server/mcp_provider_graph.h"
#include "../src/server/mcp_provider_common.h"
#include "../src/server/mcp_transport_internal.h"
#include "../src/server/object_protocol.h"
#include "../src/server/remote_http_internal.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/mcp_content.h"
#include "q4t/server/responses_mcp.h"
#include "q4t/server/schema_graph.h"
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
  std::string mime = "application/json";
  std::map<std::string, std::string> headers{};
};
class ProviderPeer {
 public:
  explicit ProviderPeer(std::vector<Reply> replies,
                        std::string provider = kProvider)
      : replies_(std::move(replies)), provider_(std::move(provider)) {
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
        while (request.size() <= 32768) {
          const auto end = request.find("\r\n\r\n");
          if (end != request.npos) {
            size_t length = 0;
            const auto pos = request.find("\r\nContent-Length: ");
            if (pos != request.npos && pos < end) {
              const char* begin = request.data() + pos + 18;
              const auto parsed =
                  std::from_chars(begin, request.data() + end, length);
              if (parsed.ec != std::errc{} || length > 32768) break;
            }
            if (request.size() >= end + 4 + length) break;
          }
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
        std::string extra;
        for (const auto& [key, value] : reply.headers)
          extra += key + ": " + value + "\r\n";
        std::string output =
            "HTTP/1.1 " + std::to_string(reply.status) +
            " Result\r\nContent-Type: " + reply.mime +
            "\r\nContent-Length: " + std::to_string(reply.body.size()) +
            "\r\nConnection: close\r\n" + extra + "\r\n" +
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
    file_detail::Url original, local;
    FileFetchOptions options;
    file_detail::RemoteHeaders headers;
    McpEndpoint endpoint;
    endpoint.provider_id = provider_;
    endpoint.authorization = "synthetic-private-token";
    endpoint.headers["X-Private-Tenant"] = "fixture-only";
    auto reply = mcp_detail::PrepareProviderRequest(
        endpoint, FileFetchOptions{}, request, &original, &headers, &options);
    if (!reply.ok()) return reply;
    // Only the test peer replaces the already-validated production URL with
    // a loopback socket. Production has no such override or test environment.
    file_detail::ParseUrl(
        "http://127.0.0.1:" + std::to_string(port_) + original.target, &local);
    options.public_networks = false;
    options.allow_http = true;
    options.allowed_networks = {"127.0.0.1/32"};
    options.ports = {port_};
    options.max_redirects = 0;
    std::unique_ptr<file_detail::RemoteHttp> response;
    reply = file_detail::RemoteHttp::Open(
        local, options, request.method, headers, request.body,
        (4U << 20) - bytes_, std::chrono::steady_clock::now() + 2s, &response);
    return reply.ok() ? mcp_detail::ReadProviderResponse(
                            *response, &bytes_, output, request.binary,
                            request.dropbox_content)
                      : reply;
  }

 private:
  int fd_ = -1, port_ = 0;
  uint64_t bytes_ = 0;
  std::mutex mutex_;
  std::vector<Reply> replies_;
  std::string provider_;
  std::vector<std::string> requests_;
  std::jthread thread_;
};
}  // namespace

namespace {
constexpr auto kDriveProvider = "connector_googledrive";
constexpr auto kGmailProvider = "connector_gmail";
Reply JsonReply(Json value) { return {200, OutputJson(value)}; }
ObjectReply ProviderCall(std::string_view provider, std::string name, Json args,
                         ProviderPeer& peer, Json* result, Json* error) {
  return mcp_detail::InvokeProvider(
      provider, "tools/call", Call(std::move(name), std::move(args)),
      [&](const auto& request, Json* value) {
        return peer.Fetch(request, value);
      },
      result, error);
}
Json DriveFile(std::string mime = "text/plain", std::string id = "file_1") {
  return Object(
      {{"id", String(std::move(id))},
       {"name", String("原始资料")},
       {"mimeType", String(std::move(mime))},
       {"webViewLink", String("https://drive.google.com/file/d/file_1/view")}});
}
std::string UrlBase64(std::string_view bytes) {
  auto value = mcp_detail::provider_detail::Base64(bytes);
  for (auto& c : value)
    if (c == '+')
      c = '-';
    else if (c == '/')
      c = '_';
  while (!value.empty() && value.back() == '=') value.pop_back();
  return value;
}
Json MailPart(std::string bytes, std::string mime = "text/plain",
              std::string filename = "") {
  return Object({{"mimeType", String(std::move(mime))},
                 {"filename", String(std::move(filename))},
                 {"body", Object({{"size", Number(bytes.size())},
                                  {"data", String(UrlBase64(bytes))}})}});
}
Json Mail(std::string id = "mail_1", Json payload = MailPart("Original body")) {
  Set(&payload, "headers",
      Array(
          {Object({{"name", String("Subject")}, {"value", String("项目验收")}}),
           Object({{"name", String("From")},
                   {"value", String("owner@example.test")}})}));
  return Object({{"id", String(std::move(id))},
                 {"threadId", String("thread_1")},
                 {"internalDate", String("1794787200000")},
                 {"snippet", String("Original snippet")},
                 {"payload", std::move(payload)}});
}
}  // namespace

Q4T_TEST(mcp_google_fixed_origins_are_provider_specific) {
  Q4T_CHECK(mcp_detail::ProviderOrigin(
      kGmailProvider,
      "https://gmail.googleapis.com/gmail/v1/users/me/messages"));
  Q4T_CHECK(!mcp_detail::ProviderOrigin(
      kDriveProvider,
      "https://gmail.googleapis.com/gmail/v1/users/me/messages"));
  Q4T_CHECK(!mcp_detail::ProviderOrigin(
      kGmailProvider, "https://www.googleapis.com/drive/v3/files"));
  for (const auto* id : {kProvider, kDriveProvider, kGmailProvider}) {
    Q4T_CHECK(mcp_detail::NativeProvider(id));
    Q4T_CHECK(mcp_detail::ProviderOrigin(
        id, "https://oauth2.googleapis.com/tokeninfo"));
    Q4T_CHECK(!mcp_detail::ProviderOrigin(
        id, "https://gmail.googleapis.com.attacker.invalid/"));
    Q4T_CHECK(
        !mcp_detail::ProviderOrigin(id, "https://gmail.googleapis.com:8443/"));
  }
  return true;
}
Q4T_TEST(mcp_google_drive_catalog_exact_permissions) {
  for (const auto& [scope, count] : std::vector<std::pair<std::string, size_t>>{
           {"drive.readonly", 5},
           {"drive.file", 4},
           {"drive.metadata.readonly", 3},
           {"userinfo.email", 0}}) {
    ProviderPeer peer(
        {JsonReply(Object(
            {{"scope", String("https://www.googleapis.com/auth/" + scope)}}))},
        kDriveProvider);
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  kDriveProvider, "tools/list", Object(),
                  [&](const auto& request, Json* value) {
                    return peer.Fetch(request, value);
                  },
                  &result, &error)
                  .ok());
    Q4T_CHECK(error.IsNull() &&
              result.GetArray("tools")->array.size() == count);
    const auto text = OutputJson(result);
    Q4T_CHECK((text.find("\"fetch\"") != text.npos) == (count >= 4));
    Q4T_CHECK((text.find("\"list_drives\"") != text.npos) == (count == 5));
  }
  return true;
}
Q4T_TEST(mcp_google_gmail_catalog_exact_permissions) {
  for (const auto& [scope, count] :
       std::vector<std::pair<std::string, size_t>>{{"gmail.readonly", 6},
                                                   {"gmail.metadata", 1},
                                                   {"userinfo.email", 0}}) {
    ProviderPeer peer(
        {JsonReply(Object(
            {{"scope", String("https://www.googleapis.com/auth/" + scope)}}))},
        kGmailProvider);
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  kGmailProvider, "tools/list", Object(),
                  [&](const auto& request, Json* value) {
                    return peer.Fetch(request, value);
                  },
                  &result, &error)
                  .ok());
    Q4T_CHECK(error.IsNull() &&
              result.GetArray("tools")->array.size() == count);
  }
  return true;
}
Q4T_TEST(mcp_google_drive_search_escape_shared_drive_cursor) {
  ProviderPeer peer({JsonReply(Object({{"files", Array({DriveFile()})},
                                       {"incompleteSearch", Boolean(true)},
                                       {"nextPageToken", String("next+/=")}}))},
                    kDriveProvider);
  Json result, error;
  const auto query = "owner's \\ report";
  Q4T_CHECK(ProviderCall(kDriveProvider, "search",
                         Object({{"query", String(query)},
                                 {"drive_id", String("drive_1")},
                                 {"folder_id", String("folder_1")},
                                 {"next_page_token", String("cursor+/=")},
                                 {"max_results", Number(2)}}),
                         peer, &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  const auto* page = result.Find("structuredContent");
  Q4T_CHECK(page->GetBool("incompleteSearch") &&
            page->GetString("next_page_token") == "next+/=" &&
            page->GetArray("files")->array[0].GetString("name") == "原始资料");
  const auto requests = peer.Requests();
  Q4T_CHECK(requests.size() == 1);
  for (const auto* expected :
       {"driveId=drive_1", "corpora=drive", "pageToken=cursor%2B%2F%3D",
        "owner%5C%27s%20%5C%5C%20report", "supportsAllDrives=true", "folder_1"})
    Q4T_CHECK(requests[0].find(expected) != requests[0].npos);
  return true;
}
Q4T_TEST(mcp_google_drive_profile_shared_drives_and_recent) {
  ProviderPeer peer(
      {JsonReply(Object({{"user", Object({{"emailAddress",
                                           String("user@example.test")}})}})),
       JsonReply(
           Object({{"drives", Array({Object({{"id", String("d1")},
                                             {"name", String("Shared")}})})}})),
       JsonReply(Object({{"files", Array()}}))},
      kDriveProvider);
  Json result, error;
  for (const auto* tool : {"get_profile", "list_drives", "recent_documents"}) {
    Q4T_CHECK(
        ProviderCall(kDriveProvider, tool, Object(), peer, &result, &error)
            .ok());
    Q4T_CHECK(error.IsNull());
  }
  const auto requests = peer.Requests();
  Q4T_CHECK(requests.size() == 3);
  Q4T_CHECK(requests[0].find("/about?fields=user") != requests[0].npos);
  Q4T_CHECK(requests[1].find("/drives?pageSize=50") != requests[1].npos);
  Q4T_CHECK(requests[2].find("orderBy=modifiedTime%20desc") !=
            requests[2].npos);
  return true;
}
Q4T_TEST(mcp_google_drive_blob_becomes_native_resource) {
  const std::string source = "原始项目文档\nNo fabricated text.\n";
  ProviderPeer peer(
      {JsonReply(DriveFile()), {200, source, false, "text/plain"}},
      kDriveProvider);
  Json result, error;
  Q4T_CHECK(ProviderCall(kDriveProvider, "fetch",
                         Object({{"file_id", String("file_1")}}), peer, &result,
                         &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  Q4T_CHECK(ValidateMcpToolResult(result));
  const auto* resource = result.GetArray("content")->array[1].Find("resource");
  Q4T_CHECK(resource->GetString("blob") ==
            mcp_detail::provider_detail::Base64(source));
  Q4T_CHECK(resource->GetString("mimeType") == "text/plain");
  McpContentProjection projection;
  Q4T_CHECK(ProjectMcpToolResult(result, Array(), &projection));
  Q4T_CHECK(OutputJson(projection.content).find("input_file") !=
            std::string::npos);
  Q4T_CHECK(peer.Requests()[1].find("alt=media") != std::string::npos);
  return true;
}
Q4T_TEST(mcp_google_drive_exports_preserve_complete_office_formats) {
  for (const auto& [type, mime] :
       std::vector<std::pair<std::string, std::string>>{
           {"document",
            "application/"
            "vnd.openxmlformats-officedocument.wordprocessingml.document"},
           {"spreadsheet",
            "application/"
            "vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
           {"presentation",
            "application/"
            "vnd.openxmlformats-officedocument.presentationml.presentation"}}) {
    ProviderPeer peer(
        {JsonReply(DriveFile("application/vnd.google-apps." + type)),
         {200, "PK-fixture-transport-bytes", false, mime}},
        kDriveProvider);
    Json result, error;
    Q4T_CHECK(ProviderCall(kDriveProvider, "fetch",
                           Object({{"file_id", String("file_1")}}), peer,
                           &result, &error)
                  .ok());
    Q4T_CHECK(error.IsNull());
    Q4T_CHECK(
        result.Find("structuredContent")->GetString("content_mime_type") ==
        mime);
    Q4T_CHECK(peer.Requests()[1].find("/export?mimeType=" +
                                      mcp_detail::ProviderEscape(mime)) !=
              std::string::npos);
  }
  return true;
}
Q4T_TEST(mcp_google_drive_shortcut_uses_fixed_origin_and_resource_key) {
  auto shortcut = DriveFile("application/vnd.google-apps.shortcut");
  Set(&shortcut, "shortcutDetails",
      Object({{"targetId", String("file_2")},
              {"targetResourceKey", String("resource_key")}}));
  auto file = DriveFile("text/plain", "file_2");
  Set(&file, "webContentLink", String("https://attacker.invalid/never-read"));
  ProviderPeer peer({JsonReply(shortcut),
                     JsonReply(file),
                     {200, "content", false, "text/plain"}},
                    kDriveProvider);
  Json result, error;
  Q4T_CHECK(ProviderCall(kDriveProvider, "fetch",
                         Object({{"file_id", String("file_1")}}), peer, &result,
                         &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  const auto requests = peer.Requests();
  Q4T_CHECK(requests.size() == 3);
  for (size_t i = 1; i < 3; ++i) {
    Q4T_CHECK(requests[i].find("/files/file_2") != std::string::npos);
    Q4T_CHECK(
        requests[i].find("X-Goog-Drive-Resource-Keys: file_2/resource_key") !=
        std::string::npos);
    Q4T_CHECK(requests[i].find("attacker.invalid") == std::string::npos);
  }
  Q4T_CHECK(
      result.Find("structuredContent")->GetArray("shortcuts")->array.size() ==
      1);
  return true;
}
Q4T_TEST(mcp_google_drive_shortcut_cycle_and_download_denial) {
  auto shortcut = DriveFile("application/vnd.google-apps.shortcut");
  Set(&shortcut, "shortcutDetails", Object({{"targetId", String("file_1")}}));
  ProviderPeer cycle({JsonReply(shortcut)}, kDriveProvider);
  Json result, error;
  Q4T_CHECK(!ProviderCall(kDriveProvider, "fetch",
                          Object({{"file_id", String("file_1")}}), cycle,
                          &result, &error)
                 .ok());
  Q4T_CHECK(cycle.Requests().size() == 1);
  auto denied = DriveFile();
  Set(&denied, "capabilities", Object({{"canDownload", Boolean(false)}}));
  ProviderPeer peer({JsonReply(denied)}, kDriveProvider);
  Q4T_CHECK(ProviderCall(kDriveProvider, "fetch",
                         Object({{"file_id", String("file_1")}}), peer, &result,
                         &error)
                .ok());
  Q4T_CHECK(error.IsObject() && result.IsNull());
  Q4T_CHECK(peer.Requests().size() == 1);
  return true;
}
Q4T_TEST(mcp_google_invalid_arguments_perform_no_io) {
  for (const auto& [provider, name, args] :
       std::vector<std::tuple<std::string, std::string, Json>>{
           {kDriveProvider, "fetch",
            Object({{"file_id", String("../secret")}})},
           {kDriveProvider, "fetch",
            Object({{"file_id", String("file_1")},
                    {"resource_key", String("key\r\nInjected")}})},
           {kDriveProvider, "search", Object({{"max_results", Number(1001)}})},
           {kDriveProvider, "list_drives",
            Object({{"max_results", Number(101)}})},
           {kDriveProvider, "recent_documents",
            Object({{"url", String("https://elsewhere.invalid/")}})},
           {kGmailProvider, "read_email",
            Object({{"message_id", String("..")}})},
           {kGmailProvider, "read_email",
            Object({{"message_id", String("m1")},
                    {"include_attachments", Number(1)}})},
           {kGmailProvider, "search_emails",
            Object(
                {{"query", String("project")}, {"max_results", Number(21)}})},
           {kGmailProvider, "search_email_ids", Object()},
           {kGmailProvider, "batch_read_email",
            Object({{"message_ids",
                     Array({String("mail_1"), String("../wrong")})}})}}) {
    size_t calls = 0;
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  provider, "tools/call", Call(name, args),
                  [&](const auto&, Json*) {
                    ++calls;
                    return ObjectReply{};
                  },
                  &result, &error)
                  .ok());
    Q4T_CHECK(calls == 0 && error.GetInt("code") == -32602 && result.IsNull());
  }
  return true;
}
Q4T_TEST(mcp_google_gmail_profile_and_single_page_ids) {
  ProviderPeer peer(
      {JsonReply(Object({{"emailAddress", String("user@example.test")},
                         {"messagesTotal", Number(3)}})),
       JsonReply(Object(
           {{"messages", Array({Object({{"id", String("mail_1")},
                                        {"threadId", String("thread_1")}})})},
            {"nextPageToken", String("next+/=")},
            {"resultSizeEstimate", Number(300)}}))},
      kGmailProvider);
  Json result, error;
  Q4T_CHECK(ProviderCall(kGmailProvider, "get_profile", Object(), peer, &result,
                         &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  Q4T_CHECK(ProviderCall(
                kGmailProvider, "search_email_ids",
                Object({{"query", String("from:owner@example.test label:项目")},
                        {"next_page_token", String("cursor+/=")},
                        {"max_results", Number(500)}}),
                peer, &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull() && peer.Requests().size() == 2);
  const auto* page = result.Find("structuredContent");
  Q4T_CHECK(page->GetString("next_page_token") == "next+/=" &&
            page->GetInt("resultSizeEstimate") == 300);
  Q4T_CHECK(peer.Requests()[1].find("pageToken=cursor%2B%2F%3D") !=
            std::string::npos);
  return true;
}
Q4T_TEST(mcp_google_gmail_search_hydrates_metadata_in_original_order) {
  ProviderPeer peer(
      {JsonReply(
           Object({{"messages", Array({Object({{"id", String("mail_2")}}),
                                       Object({{"id", String("mail_1")}})})}})),
       JsonReply(Mail("mail_2")), JsonReply(Mail("mail_1"))},
      kGmailProvider);
  Json result, error;
  Q4T_CHECK(ProviderCall(kGmailProvider, "search_emails",
                         Object({{"query", String("project")}}), peer, &result,
                         &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  const auto* messages = result.Find("structuredContent")->GetArray("messages");
  Q4T_CHECK(messages->array.size() == 2 &&
            messages->array[0].GetString("id") == "mail_2" &&
            messages->array[1].GetString("subject") == "项目验收");
  Q4T_CHECK(peer.Requests().size() == 3 &&
            peer.Requests()[1].find("format=metadata") != std::string::npos);
  return true;
}
Q4T_TEST(mcp_google_gmail_recent_empty_page_preserves_cursor) {
  ProviderPeer peer(
      {JsonReply(Object({{"nextPageToken", String("continue_empty")}}))},
      kGmailProvider);
  Json result, error;
  Q4T_CHECK(ProviderCall(kGmailProvider, "get_recent_emails", Object(), peer,
                         &result, &error)
                .ok());
  Q4T_CHECK(
      error.IsNull() &&
      result.Find("structuredContent")->GetArray("messages")->array.empty());
  Q4T_CHECK(result.Find("structuredContent")->GetString("next_page_token") ==
            "continue_empty");
  Q4T_CHECK(peer.Requests().size() == 1);
  return true;
}
Q4T_TEST(mcp_google_gmail_full_multipart_body_and_attachment_metadata) {
  auto attachment = MailPart("attachment", "application/pdf", "report.pdf");
  Set(&attachment, "body",
      Object({{"attachmentId", String("attachment_1")}, {"size", Number(10)}}));
  auto payload = MailPart("", "multipart/mixed");
  Set(&payload, "parts",
      Array({MailPart("原始正文\n"), MailPart("<p>Original</p>", "text/html"),
             attachment}));
  ProviderPeer peer({JsonReply(Mail("mail_1", payload))}, kGmailProvider);
  Json result, error;
  Q4T_CHECK(ProviderCall(kGmailProvider, "read_email",
                         Object({{"message_id", String("mail_1")}}), peer,
                         &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull() && ValidateMcpToolResult(result));
  Q4T_CHECK(result.GetArray("content")->array.size() == 3 &&
            peer.Requests().size() == 1);
  Q4T_CHECK(result.GetArray("content")
                ->array[1]
                .Find("resource")
                ->GetString("text") == "原始正文\n");
  const auto* body = result.Find("structuredContent")
                         ->Find("payload")
                         ->GetArray("parts")
                         ->array[2]
                         .Find("body");
  Q4T_CHECK(!body->GetBool("content_read", true) &&
            body->GetString("attachmentId") == "attachment_1");
  Q4T_CHECK(OutputJson(*result.Find("structuredContent"))
                .find(UrlBase64("原始正文\n")) == std::string::npos);
  return true;
}
Q4T_TEST(mcp_google_gmail_external_text_body_and_explicit_attachment) {
  for (bool attachment : {false, true}) {
    auto payload = MailPart("", attachment ? "application/pdf" : "text/plain",
                            attachment ? "report.pdf" : "");
    Set(&payload, "body",
        Object(
            {{"size", Number(13)}, {"attachmentId", String("attachment_1")}}));
    ProviderPeer peer(
        {JsonReply(Mail("mail_1", payload)),
         JsonReply(Object({{"data", String(UrlBase64("Original body"))},
                           {"size", Number(13)}}))},
        kGmailProvider);
    Json result, error;
    Q4T_CHECK(
        ProviderCall(kGmailProvider, "read_email",
                     Object({{"message_id", String("mail_1")},
                             {"include_attachments", Boolean(attachment)}}),
                     peer, &result, &error)
            .ok());
    Q4T_CHECK(error.IsNull() && peer.Requests().size() == 2 &&
              ValidateMcpToolResult(result));
    const auto* resource =
        result.GetArray("content")->array[1].Find("resource");
    Q4T_CHECK(attachment ? resource->GetString("blob") == "T3JpZ2luYWwgYm9keQ=="
                         : resource->GetString("text") == "Original body");
  }
  return true;
}
Q4T_TEST(mcp_google_gmail_charset_conversion_and_invalid_encoding) {
  for (const auto& [charset, good] : std::vector<std::pair<std::string, bool>>{
           {"ISO-8859-1", true},
           {"UTF-8", false},
           {"not-a-real-charset", false}}) {
    auto message = Mail("mail_1", MailPart(std::string("caf\xe9", 4)));
    auto payload = *message.Find("payload");
    auto headers = *payload.GetArray("headers");
    headers.array.push_back(Object(
        {{"name", String("Content-Type")},
         {"value", String("text/plain; charset = \"" + charset + "\"")}}));
    Set(&payload, "headers", headers);
    Set(&message, "payload", payload);
    ProviderPeer peer({JsonReply(message)}, kGmailProvider);
    Json result, error;
    auto reply = ProviderCall(kGmailProvider, "read_email",
                              Object({{"message_id", String("mail_1")}}), peer,
                              &result, &error);
    Q4T_CHECK(reply.ok() == good);
    if (good)
      Q4T_CHECK(result.GetArray("content")
                    ->array[1]
                    .Find("resource")
                    ->GetString("text") == "café");
    else
      Q4T_CHECK(result.IsNull());
  }
  return true;
}
Q4T_TEST(mcp_google_gmail_batch_order_and_partial_error_no_fabrication) {
  const auto args =
      Object({{"message_ids", Array({String("mail_2"), String("mail_1")})}});
  for (bool fail : {false, true}) {
    ProviderPeer peer(
        {JsonReply(Mail("mail_2")), fail ? Reply{404, "secret upstream error"}
                                         : JsonReply(Mail("mail_1"))},
        kGmailProvider);
    Json result, error;
    Q4T_CHECK(ProviderCall(kGmailProvider, "batch_read_email", args, peer,
                           &result, &error)
                  .ok());
    Q4T_CHECK(peer.Requests().size() == 2);
    if (fail) {
      Q4T_CHECK(error.IsObject() && result.IsNull());
      Q4T_CHECK(OutputJson(error).find("secret") == std::string::npos);
    } else {
      const auto* mails =
          result.Find("structuredContent")->GetArray("messages");
      Q4T_CHECK(mails->array.size() == 2 &&
                mails->array[0].GetString("id") == "mail_2");
    }
  }
  return true;
}
Q4T_TEST(mcp_google_catalog_schemas_accept_executable_arguments) {
  for (const auto* provider : {kDriveProvider, kGmailProvider}) {
    Json result, error;
    Q4T_CHECK(
        mcp_detail::InvokeProvider(
            provider, "tools/list", Object(),
            [](const auto&, Json* value) {
              *value = Object(
                  {{"scope",
                    String("https://www.googleapis.com/auth/drive.readonly "
                           "https://www.googleapis.com/auth/gmail.readonly")}});
              return ObjectReply{};
            },
            &result, &error)
            .ok());
    Q4T_CHECK(error.IsNull());
    for (const auto& tool : result.GetArray("tools")->array) {
      OutputSchemaGraph graph;
      std::string param;
      Q4T_CHECK(graph.Init(*tool.Find("inputSchema"), false, &param, "schema",
                           OutputSchemaPurpose::kToolResult));
      auto args = Object();
      const auto name = tool.GetString("name");
      if (name == "fetch") Set(&args, "file_id", String("file_1"));
      if (name == "read_email") Set(&args, "message_id", String("mail_1"));
      if (name == "batch_read_email")
        Set(&args, "message_ids", Array({String("mail_1"), String("mail_1")}));
      if (name == "search_emails" || name == "search_email_ids")
        Set(&args, "query", String("project"));
      Q4T_CHECK(graph.Check(args));
    }
  }
  ProviderPeer peer({JsonReply(Mail()), JsonReply(Mail())}, kGmailProvider);
  Json result, error;
  Q4T_CHECK(ProviderCall(kGmailProvider, "batch_read_email",
                         Object({{"message_ids", Array({String("mail_1"),
                                                        String("mail_1")})}}),
                         peer, &result, &error)
                .ok());
  Q4T_CHECK(
      error.IsNull() &&
      result.Find("structuredContent")->GetArray("messages")->array.size() ==
          2);
  return true;
}

Q4T_TEST(mcp_google_base64url_canonical_and_binary_roundtrip) {
  for (const auto& bytes : std::vector<std::string>{
           "", "f", "fo", "foo", std::string("\0\xff\xfe", 3), "中文"}) {
    std::string decoded;
    Q4T_CHECK(
        mcp_detail::provider_detail::DecodeBase64Url(UrlBase64(bytes), &decoded)
            .ok() &&
        decoded == bytes);
  }
  for (const auto* value :
       {"Z", "Zh", "Zg=", "=Zg=", "Zg===", "Zg+_", "Zg\n", "AA=A"}) {
    std::string decoded = "prior";
    Q4T_CHECK(
        !mcp_detail::provider_detail::DecodeBase64Url(value, &decoded).ok() &&
        decoded.empty());
  }
  std::string decoded;
  Q4T_CHECK(
      mcp_detail::provider_detail::DecodeBase64Url("Zg==", &decoded).ok() &&
      decoded == "f");
  return true;
}
Q4T_TEST(mcp_google_malformed_payload_and_response_not_success) {
  for (auto message : std::vector<Json>{Object({{"id", String("mail_1")}}),
                                        Mail("wrong_id"), Mail()}) {
    if (message.GetString("id") == "mail_1" && message.Has("payload")) {
      auto payload = *message.Find("payload");
      Set(&payload, "body",
          Object({{"size", Number(2)}, {"data", String("Zg")}}));
      Set(&message, "payload", payload);
    }
    ProviderPeer peer({JsonReply(message)}, kGmailProvider);
    Json result, error;
    Q4T_CHECK(!ProviderCall(kGmailProvider, "read_email",
                            Object({{"message_id", String("mail_1")}}), peer,
                            &result, &error)
                   .ok());
    Q4T_CHECK(result.IsNull());
  }
  ProviderPeer peer({JsonReply(Object({{"files", String("invalid")}}))},
                    kDriveProvider);
  Json result, error;
  Q4T_CHECK(
      !ProviderCall(kDriveProvider, "search", Object(), peer, &result, &error)
           .ok());
  Q4T_CHECK(result.IsNull());
  return true;
}
Q4T_TEST(mcp_google_mime_depth_and_native_output_budget) {
  auto payload = MailPart("body");
  for (int i = 0; i < 18; ++i) {
    auto parent = MailPart("", "multipart/mixed");
    Set(&parent, "parts", Array({payload}));
    payload = std::move(parent);
  }
  ProviderPeer peer({JsonReply(Mail("mail_1", payload))}, kGmailProvider);
  Json result, error;
  Q4T_CHECK(!ProviderCall(kGmailProvider, "read_email",
                          Object({{"message_id", String("mail_1")}}), peer,
                          &result, &error)
                 .ok());
  Q4T_CHECK(result.IsNull());
  auto huge = Object({{"source", String(std::string(3U << 20, 'x'))}});
  Q4T_CHECK(
      mcp_detail::provider_detail::Result(huge, Array(), &result).http_status ==
      413);
  Q4T_CHECK(result.IsNull());
  return true;
}

namespace {
constexpr auto kOutlookMail = "connector_outlookemail";
constexpr auto kOutlookCalendar = "connector_outlookcalendar";
Json GraphMessage(std::string id = "message+/=1") {
  return Object(
      {{"id", String(std::move(id))},
       {"subject", String("实际主题")},
       {"body", Object({{"contentType", String("text")},
                        {"content", String("原始正文")}})},
       {"from",
        Object({{"emailAddress",
                 Object({{"address", String("owner@example.test")}})}})}});
}
Json GraphEvent(std::string id = "event+/=1") {
  return Object(
      {{"id", String(std::move(id))},
       {"subject", String("实际会议")},
       {"type", String("occurrence")},
       {"seriesMasterId", String("master_original")},
       {"start", Object({{"dateTime", String("2026-11-16T09:00:00.0000000")},
                         {"timeZone", String("China Standard Time")}})}});
}
Json GraphHits(bool more = false, int total = 1, Json resource = Json()) {
  auto hit =
      Object({{"hitId", String("opaque+/=event")},
              {"rank", Number(1)},
              {"summary", String("Original summary")},
              {"resource", resource.IsNull()
                               ? Object({{"subject", String("Original event")}})
                               : std::move(resource)}});
  return Object(
      {{"value",
        Array({Object({{"hitsContainers",
                        Array({Object({{"hits", total ? Array({hit}) : Array()},
                                       {"total", Number(total)},
                                       {"moreResultsAvailable",
                                        Boolean(more)}})})}})})}});
}
}  // namespace

Q4T_TEST(mcp_graph_origins_exclude_google_and_other_clouds) {
  for (const auto* id : {kOutlookMail, kOutlookCalendar}) {
    Q4T_CHECK(mcp_detail::NativeProvider(id));
    Q4T_CHECK(
        mcp_detail::ProviderOrigin(id, "https://graph.microsoft.com/v1.0/me"));
    for (const auto* url : {"https://oauth2.googleapis.com/tokeninfo",
                            "https://graph.microsoft.com.evil.invalid/",
                            "http://graph.microsoft.com/v1.0/me",
                            "https://graph.microsoft.com:8443/v1.0/me"})
      Q4T_CHECK(!mcp_detail::ProviderOrigin(id, url));
  }
  Q4T_CHECK(!mcp_detail::ProviderOrigin(kGmailProvider,
                                        "https://graph.microsoft.com/v1.0/me"));
  return true;
}
Q4T_TEST(mcp_graph_catalog_schemas_no_extra_profile_dependency) {
  for (const auto* provider : {kOutlookMail, kOutlookCalendar}) {
    size_t calls = 0;
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  provider, "tools/list", Object(),
                  [&](const auto&, Json*) {
                    ++calls;
                    return ObjectReply{};
                  },
                  &result, &error)
                  .ok());
    const auto* tools = result.GetArray("tools");
    Q4T_CHECK(calls == 0 && error.IsNull() && tools &&
              tools->array.size() == (provider == kOutlookMail ? 6U : 5U));
    for (const auto& tool : tools->array) {
      OutputSchemaGraph graph;
      std::string param;
      Q4T_CHECK(graph.Init(*tool.Find("inputSchema"), false, &param, "schema",
                           OutputSchemaPurpose::kToolResult));
      const auto name = tool.GetString("name");
      auto args = Object();
      if (name == "search_events" || name == "search_messages")
        Set(&args, "query", String("project"));
      if (name == "fetch_event") Set(&args, "event_id", String("event+/=1"));
      if (name == "fetch_message")
        Set(&args, "message_id", String("message+/=1"));
      if (name == "fetch_events_batch")
        Set(&args, "event_ids", Array({String("event+/=1")}));
      if (name == "fetch_messages_batch")
        Set(&args, "message_ids", Array({String("message+/=1")}));
      Q4T_CHECK(graph.Check(args));
    }
  }
  return true;
}
Q4T_TEST(mcp_graph_profile_permission_failure_does_not_disable_mail) {
  ProviderPeer peer({{403, "private error"}, JsonReply(GraphMessage())},
                    kOutlookMail);
  Json result, error;
  Q4T_CHECK(
      ProviderCall(kOutlookMail, "get_profile", Object(), peer, &result, &error)
          .ok());
  Q4T_CHECK(error.Find("data")->GetInt("upstream_status") == 403);
  Q4T_CHECK(ProviderCall(kOutlookMail, "fetch_message",
                         Object({{"message_id", String("message+/=1")}}), peer,
                         &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull() &&
            result.Find("structuredContent")->GetString("subject") ==
                "实际主题");
  Q4T_CHECK(peer.Requests().size() == 2);
  return true;
}
Q4T_TEST(mcp_graph_profile_preserves_original_fields) {
  ProviderPeer peer({JsonReply(Object({{"id", String("user-original")},
                                       {"displayName", String("原用户")},
                                       {"mail", Json()},
                                       {"userPrincipalName",
                                        String("original@example.test")}}))},
                    kOutlookCalendar);
  Json result, error;
  Q4T_CHECK(ProviderCall(kOutlookCalendar, "get_profile", Object(), peer,
                         &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull() &&
            result.Find("structuredContent")->Find("mail")->IsNull());
  Q4T_CHECK(peer.Requests()[0].find("GET /v1.0/me?") != std::string::npos);
  return true;
}
Q4T_TEST(mcp_graph_mail_search_and_encoded_cursor_keep_query) {
  const std::string next =
      "https://graph.microsoft.com/v1.0/me/"
      "messages?%24search=%22from%3aa%2bb%40example.test%22&%24top=2&%"
      "24skiptoken=opaque%2b%2f%3d";
  ProviderPeer peer({JsonReply(Object({{"value", Array({GraphMessage()})},
                                       {"@odata.nextLink", String(next)}})),
                     JsonReply(Object({{"value", Array()}}))},
                    kOutlookMail);
  auto args = Object(
      {{"query", String("from:a+b@example.test")}, {"max_results", Number(2)}});
  Json result, error;
  Q4T_CHECK(
      ProviderCall(kOutlookMail, "search_messages", args, peer, &result, &error)
          .ok());
  Q4T_CHECK(error.IsNull() &&
            result.Find("structuredContent")->GetString("next_page_token") ==
                next);
  Set(&args, "next_page_token", String(next));
  Q4T_CHECK(
      ProviderCall(kOutlookMail, "search_messages", args, peer, &result, &error)
          .ok());
  Q4T_CHECK(error.IsNull() && peer.Requests().size() == 2);
  Q4T_CHECK(peer.Requests()[1].find("%24skiptoken=opaque%2b%2f%3d") !=
            std::string::npos);
  return true;
}
Q4T_TEST(mcp_graph_mail_metadata_cursor_does_not_expose_full_body) {
  auto message = GraphMessage();
  Set(&message, "attachments",
      Array({Object({{"contentBytes", String("private")}})}));
  ProviderPeer peer({JsonReply(Object({{"value", Array({message})}}))},
                    kOutlookMail);
  Json result, error;
  Q4T_CHECK(ProviderCall(kOutlookMail, "list_messages",
                         Object({{"next_page_token",
                                  String("https://graph.microsoft.com/v1.0/me/"
                                         "messages?$skiptoken=opaque")}}),
                         peer, &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  for (const char* field : {"value", "messages"}) {
    const auto& metadata =
        result.Find("structuredContent")->GetArray(field)->array[0];
    Q4T_CHECK(metadata.GetString("id") == "message+/=1" &&
              !metadata.Has("body") && !metadata.Has("attachments"));
  }
  Q4T_CHECK(OutputJson(result).find("contentBytes") == std::string::npos);
  return true;
}
Q4T_TEST(mcp_graph_mail_recent_folder_opaque_ids) {
  ProviderPeer peer({JsonReply(Object({{"value", Array({GraphMessage()})}}))},
                    kOutlookMail);
  Json result, error;
  Q4T_CHECK(ProviderCall(kOutlookMail, "get_recent_emails",
                         Object({{"folder_id", String("folder+/=")}}), peer,
                         &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  const auto request = peer.Requests()[0];
  Q4T_CHECK(request.find("/mailFolders/folder%2B%2F%3D/messages?") !=
            request.npos);
  Q4T_CHECK(request.find("$orderby=receivedDateTime%20desc") != request.npos);
  return true;
}
Q4T_TEST(mcp_graph_cursor_wrong_authority_or_changed_contract_no_io) {
  for (const auto* cursor :
       {"https://evil.invalid/v1.0/me/messages?$skip=1",
        "http://graph.microsoft.com/v1.0/me/messages?$skip=1",
        "https://graph.microsoft.com/v1.0/me/events?$skip=1",
        "https://graph.microsoft.com/v1.0/me/messages?$skip=1&%24skip=2",
        "https://graph.microsoft.com/v1.0/me/"
        "messages?$skip=1&$expand=attachments",
        "https://graph.microsoft.com/v1.0/me/messages?$skip=1&$select=id,body",
        "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=%zz",
        "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=x&$top=3"}) {
    size_t calls = 0;
    Json result, error;
    Q4T_CHECK(
        mcp_detail::InvokeProvider(
            kOutlookMail, "tools/call",
            Call("list_messages", Object({{"next_page_token", String(cursor)},
                                          {"max_results", Number(2)}})),
            [&](const auto&, Json*) {
              ++calls;
              return ObjectReply{};
            },
            &result, &error)
            .ok());
    Q4T_CHECK(calls == 0 && error.GetInt("code") == -32602);
  }
  return true;
}
Q4T_TEST(mcp_graph_mail_full_body_format_and_permission_are_explicit) {
  for (bool missing : {false, true}) {
    auto message = GraphMessage();
    Set(&message, "body",
        missing ? Json()
                : Object({{"contentType", String("html")},
                          {"content", String("<b>原文</b>")}}));
    ProviderPeer peer({JsonReply(message)}, kOutlookMail);
    Json result, error;
    Q4T_CHECK(ProviderCall(kOutlookMail, "fetch_message",
                           Object({{"message_id", String("message+/=1")}}),
                           peer, &result, &error)
                  .ok());
    if (missing)
      Q4T_CHECK(error.IsObject() && result.IsNull());
    else
      Q4T_CHECK(error.IsNull() && result.Find("structuredContent")
                                          ->Find("body")
                                          ->GetString("contentType") == "html");
    Q4T_CHECK(
        peer.Requests()[0].find("Prefer: outlook.body-content-type=\"text\"") !=
        std::string::npos);
  }
  return true;
}
Q4T_TEST(mcp_graph_mail_batch_order_and_known_failure) {
  const auto args =
      Object({{"message_ids", Array({String("m2"), String("m1")})}});
  for (bool fail : {false, true}) {
    ProviderPeer peer({JsonReply(GraphMessage("m2")),
                       fail ? Reply{429, "private quota details"}
                            : JsonReply(GraphMessage("m1"))},
                      kOutlookMail);
    Json result, error;
    Q4T_CHECK(ProviderCall(kOutlookMail, "fetch_messages_batch", args, peer,
                           &result, &error)
                  .ok());
    Q4T_CHECK(peer.Requests().size() == 2);
    if (fail)
      Q4T_CHECK(error.Find("data")->GetInt("upstream_status") == 429 &&
                result.IsNull());
    else
      Q4T_CHECK(error.IsNull() && result.Find("structuredContent")
                                          ->GetArray("messages")
                                          ->array[0]
                                          .GetString("id") == "m2");
  }
  return true;
}
Q4T_TEST(mcp_graph_calendar_window_expands_recurrence_and_keeps_timezone) {
  ProviderPeer peer({JsonReply(Object({{"value", Array({GraphEvent()})}}))},
                    kOutlookCalendar);
  Json result, error;
  Q4T_CHECK(
      ProviderCall(kOutlookCalendar, "list_events",
                   Object({{"time_min", String("2026-11-16T08:00:00+08:00")},
                           {"time_max", String("2026-11-17T08:00:00+08:00")},
                           {"timezone_str", String("China Standard Time")}}),
                   peer, &result, &error)
          .ok());
  Q4T_CHECK(error.IsNull());
  const auto request = peer.Requests()[0];
  Q4T_CHECK(request.find("/calendar/calendarView?") != request.npos &&
            request.find("startDateTime=2026-11-16T00%3A00%3A00Z") !=
                request.npos &&
            request.find("Prefer: outlook.timezone=\"China Standard Time\"") !=
                request.npos);
  const auto& event =
      result.Find("structuredContent")->GetArray("events")->array[0];
  Q4T_CHECK(event.GetString("type") == "occurrence" &&
            event.GetString("seriesMasterId") == "master_original");
  Q4T_CHECK(event.Find("start")->GetString("timeZone") ==
            "China Standard Time");
  return true;
}
Q4T_TEST(mcp_graph_calendar_invalid_windows_and_headers_never_fetch) {
  for (const auto& args : std::vector<Json>{
           Object({{"time_min", String("2026-11-16")}}),
           Object({{"time_min", String("2026-11-17")},
                   {"time_max", String("2026-11-16")}}),
           Object({{"time_min", String("2026-11-16T00:00:00.1Z")},
                   {"time_max", String("2026-11-16T00:00:00Z")}}),
           Object({{"time_min", String("2026-02-30")},
                   {"time_max", String("2026-11-16")}}),
           Object({{"timezone_str", String("UTC\"\r\nInjected: yes")}})}) {
    size_t calls = 0;
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  kOutlookCalendar, "tools/call", Call("list_events", args),
                  [&](const auto&, Json*) {
                    ++calls;
                    return ObjectReply{};
                  },
                  &result, &error)
                  .ok());
    Q4T_CHECK(calls == 0 && error.GetInt("code") == -32602);
  }
  return true;
}
Q4T_TEST(mcp_graph_calendar_search_uses_fixed_read_only_post) {
  ProviderPeer peer({JsonReply(GraphHits(true))}, kOutlookCalendar);
  Json result, error;
  Q4T_CHECK(ProviderCall(kOutlookCalendar, "search_events",
                         Object({{"query", String(R"(项目 \ "quoted")")},
                                 {"offset", Number(20)},
                                 {"max_results", Number(10)}}),
                         peer, &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  const auto request = peer.Requests()[0];
  Q4T_CHECK(request.starts_with("POST /v1.0/search/query HTTP/1.1\r\n"));
  Json body;
  Q4T_CHECK(
      q4t::io::ParseJson(request.substr(request.find("\r\n\r\n") + 4), &body));
  const auto& search = body.GetArray("requests")->array[0];
  Q4T_CHECK(search.GetArray("entityTypes")->array[0].str == "event" &&
            search.GetInt("from") == 20 && search.GetInt("size") == 10);
  Q4T_CHECK(result.Find("structuredContent")->GetInt("next_offset") == 30);
  Q4T_CHECK(result.Find("structuredContent")
                ->GetArray("hits")
                ->array[0]
                .GetString("hitId") == "opaque+/=event");
  return true;
}
Q4T_TEST(mcp_graph_calendar_empty_search_does_not_invent_total) {
  ProviderPeer peer({JsonReply(GraphHits(false, 0))}, kOutlookCalendar);
  Json result, error;
  Q4T_CHECK(ProviderCall(kOutlookCalendar, "search_events",
                         Object({{"query", String("none")}}), peer, &result,
                         &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  const auto* value = result.Find("structuredContent");
  Q4T_CHECK(value->GetArray("hits")->array.empty() &&
            value->Find("next_offset")->IsNull());
  Q4T_CHECK(value->GetArray("value")
                ->array[0]
                .GetArray("hitsContainers")
                ->array[0]
                .GetInt("total", -1) == 0);
  return true;
}
Q4T_TEST(mcp_graph_calendar_fetch_and_batch_preserve_opaque_ids) {
  ProviderPeer peer({JsonReply(GraphEvent()), JsonReply(GraphEvent()),
                     JsonReply(GraphEvent())},
                    kOutlookCalendar);
  Json result, error;
  Q4T_CHECK(ProviderCall(kOutlookCalendar, "fetch_event",
                         Object({{"event_id", String("event+/=1")},
                                 {"calendar_id", String("calendar+/=")}}),
                         peer, &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  Q4T_CHECK(peer.Requests()[0].find(
                "/calendars/calendar%2B%2F%3D/events/event%2B%2F%3D1") !=
            std::string::npos);
  Q4T_CHECK(ProviderCall(kOutlookCalendar, "fetch_events_batch",
                         Object({{"event_ids", Array({String("event+/=1"),
                                                      String("event+/=1")})}}),
                         peer, &result, &error)
                .ok());
  Q4T_CHECK(
      error.IsNull() &&
      result.Find("structuredContent")->GetArray("events")->array.size() == 2);
  return true;
}
Q4T_TEST(mcp_graph_calendar_cursor_keeps_original_time_window) {
  const std::string cursor =
      "https://graph.microsoft.com/v1.0/me/calendar/"
      "calendarView?startDateTime=2026-11-16T00%3a00%3a00Z&endDateTime=2026-"
      "11-17T00%3a00%3a00Z&$skiptoken=opaque";
  auto args = Object({{"time_min", String("2026-11-16T08:00:00+08:00")},
                      {"time_max", String("2026-11-17T08:00:00+08:00")},
                      {"next_page_token", String(cursor)}});
  ProviderPeer peer({JsonReply(Object({{"value", Array()}}))},
                    kOutlookCalendar);
  Json result, error;
  Q4T_CHECK(
      ProviderCall(kOutlookCalendar, "list_events", args, peer, &result, &error)
          .ok());
  Q4T_CHECK(error.IsNull());
  Set(&args, "time_max", String("2026-11-18T08:00:00+08:00"));
  Q4T_CHECK(
      ProviderCall(kOutlookCalendar, "list_events", args, peer, &result, &error)
          .ok());
  Q4T_CHECK(error.GetInt("code") == -32602 && peer.Requests().size() == 1);
  return true;
}
Q4T_TEST(mcp_graph_malformed_page_search_or_full_body_fails) {
  for (const auto& [name, body] : std::vector<std::pair<std::string, Json>>{
           {"list_messages", Object({{"value", Array({Object()})}})},
           {"list_messages",
            Object({{"value", Array()},
                    {"@odata.nextLink", String("https://evil.invalid/next")}})},
           {"fetch_message",
            Object({{"id", String("m1")}, {"body", String("not-object")}})}}) {
    ProviderPeer peer({JsonReply(body)}, kOutlookMail);
    Json result, error;
    const auto args = name == "fetch_message"
                          ? Object({{"message_id", String("m1")}})
                          : Object();
    Q4T_CHECK(
        !ProviderCall(kOutlookMail, name, args, peer, &result, &error).ok());
    Q4T_CHECK(result.IsNull());
  }
  ProviderPeer peer({JsonReply(Object({{"value", Array()}}))},
                    kOutlookCalendar);
  Json result, error;
  Q4T_CHECK(!ProviderCall(kOutlookCalendar, "search_events",
                          Object({{"query", String("x")}}), peer, &result,
                          &error)
                 .ok());
  Q4T_CHECK(result.IsNull());
  return true;
}
Q4T_TEST(mcp_graph_batch_and_search_invalid_arguments_never_fetch) {
  for (const auto& [provider, name, args] :
       std::vector<std::tuple<std::string, std::string, Json>>{
           {kOutlookMail, "fetch_messages_batch",
            Object({{"message_ids", Array({String("m1"), String("..")})}})},
           {kOutlookMail, "search_messages", Object()},
           {kOutlookCalendar, "search_events",
            Object({{"query", String("x")}, {"offset", Number(-1)}})},
           {kOutlookCalendar, "fetch_event",
            Object({{"event_id", String("e1")},
                    {"url", String("https://elsewhere.invalid")}})}}) {
    size_t calls = 0;
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  provider, "tools/call", Call(name, args),
                  [&](const auto&, Json*) {
                    ++calls;
                    return ObjectReply{};
                  },
                  &result, &error)
                  .ok());
    Q4T_CHECK(calls == 0 && result.IsNull() && error.GetInt("code") == -32602);
  }
  return true;
}

namespace {
constexpr auto kTeams = "connector_microsoftteams";
constexpr auto kSharePoint = "connector_sharepoint";
Json TeamsMessage(std::string id = "message+/=1") {
  auto message = GraphMessage(std::move(id));
  Set(&message, "chatId", String("chat+/=1"));
  Set(&message, "mentions",
      Array({Object({{"id", Number(0)}, {"mentionText", String("原人名")}})}));
  return message;
}
Json SharePointFile(std::string id = "file1", size_t size = 4) {
  return Object(
      {{"id", String(id)},
       {"name", String("record.txt")},
       {"size", Number(size)},
       {"file", Object({{"mimeType", String("text/plain")}})},
       {"webUrl", String("https://tenant.sharepoint.com/record.txt")},
       {"@microsoft.graph.downloadUrl",
        String("https://download.files.1drv.com/original?secret=cap")}});
}
}  // namespace
Q4T_TEST(mcp_graph_teams_sharepoint_catalog_usable_schemas) {
  for (const auto* provider : {kTeams, kSharePoint}) {
    size_t calls = 0;
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  provider, "tools/list", Object(),
                  [&](const auto&, Json*) {
                    ++calls;
                    return ObjectReply{};
                  },
                  &result, &error)
                  .ok());
    const auto* tools = result.GetArray("tools");
    Q4T_CHECK(calls == 0 && error.IsNull() && tools &&
              tools->array.size() ==
                  (std::string_view(provider) == kTeams ? 4 : 5));
    for (const auto& tool : tools->array) {
      OutputSchemaGraph graph;
      std::string param;
      Q4T_CHECK(graph.Init(*tool.Find("inputSchema"), false, &param, "schema",
                           OutputSchemaPurpose::kToolResult));
      auto args = Object();
      const auto name = tool.GetString("name");
      if (name == "search") Set(&args, "query", String("项目"));
      if (name == "get_site") Set(&args, "site_id", String("root"));
      if (name == "get_chat_members") Set(&args, "chat_id", String("chat1"));
      if (name == "fetch") {
        if (std::string_view(provider) == kTeams) {
          Set(&args, "chat_id", String("chat1"));
          Set(&args, "message_id", String("message1"));
        } else
          Set(&args, "item_id", String("file1"));
      }
      Q4T_CHECK(graph.Check(args));
    }
  }
  return true;
}
Q4T_TEST(mcp_graph_teams_search_preserves_original_hits) {
  auto hits = GraphHits(false, 1, TeamsMessage());
  ProviderPeer peer({JsonReply(hits)}, kTeams);
  Json result, error;
  Q4T_CHECK(ProviderCall(kTeams, "search",
                         Object({{"query", String("project")}}), peer, &result,
                         &error)
                .ok());
  Q4T_CHECK(error.IsNull());
  const auto request = peer.Requests()[0];
  Q4T_CHECK(request.starts_with("POST /v1.0/search/query ") &&
            request.find("Prefer: include-unknown-enum-members") !=
                request.npos);
  Json body;
  Q4T_CHECK(
      q4t::io::ParseJson(request.substr(request.find("\r\n\r\n") + 4), &body));
  Q4T_CHECK(body.GetArray("requests")
                ->array[0]
                .GetArray("entityTypes")
                ->array[0]
                .str == "chatMessage");
  Q4T_CHECK(result.Find("structuredContent")
                ->GetArray("hits")
                ->array[0]
                .Find("resource")
                ->GetString("chatId") == "chat+/=1");
  return true;
}
Q4T_TEST(mcp_graph_teams_chat_channel_reply_paths_keep_content) {
  for (int mode = 0; mode < 3; ++mode) {
    auto args = Object({{"message_id", String("m+/=")}});
    if (!mode)
      Set(&args, "chat_id", String("chat+/="));
    else {
      Set(&args, "team_id", String("team+/="));
      Set(&args, "channel_id", String("channel+/="));
      if (mode == 2) Set(&args, "reply_id", String("reply+/="));
    }
    auto original = TeamsMessage(mode == 2 ? "reply+/=" : "m+/=");
    Set(&original, "replyToId", mode == 2 ? String("m+/=") : Json());
    ProviderPeer peer({JsonReply(original)}, kTeams);
    Json result, error;
    Q4T_CHECK(ProviderCall(kTeams, "fetch", args, peer, &result, &error).ok());
    Q4T_CHECK(error.IsNull() && OutputJson(*result.Find("structuredContent")) ==
                                    OutputJson(original));
    const auto request = peer.Requests()[0];
    Q4T_CHECK(request.find(!mode ? "/chats/chat%2B%2F%3D/messages/m%2B%2F%3D"
                                 : "/teams/team%2B%2F%3D/channels/"
                                   "channel%2B%2F%3D/messages/m%2B%2F%3D") !=
              request.npos);
    if (mode == 2)
      Q4T_CHECK(request.find("/replies/reply%2B%2F%3D") != request.npos);
  }
  return true;
}
Q4T_TEST(mcp_graph_teams_member_paging_retains_tenant_and_opaque_ids) {
  const std::string cursor =
      "https://graph.microsoft.com/v1.0/chats/c1/"
      "members?$skiptoken=opaque%2B%2F%3D";
  const auto member = Object({{"id", String("opaque:member+/=")},
                              {"tenantId", String("other-tenant")},
                              {"roles", Array({String("owner")})}});
  ProviderPeer peer({JsonReply(Object({{"value", Array({member})},
                                       {"@odata.nextLink", String(cursor)}})),
                     JsonReply(Object({{"value", Array()}}))},
                    kTeams);
  Json result, error;
  auto args = Object({{"chat_id", String("c1")}});
  Q4T_CHECK(
      ProviderCall(kTeams, "get_chat_members", args, peer, &result, &error)
          .ok());
  Q4T_CHECK(
      error.IsNull() &&
      OutputJson(
          result.Find("structuredContent")->GetArray("members")->array[0]) ==
          OutputJson(member));
  Q4T_CHECK(
      peer.Requests()[0].starts_with("GET /v1.0/chats/c1/members HTTP/1.1"));
  Set(&args, "next_page_token", String(cursor));
  Q4T_CHECK(
      ProviderCall(kTeams, "get_chat_members", args, peer, &result, &error)
          .ok());
  Q4T_CHECK(
      error.IsNull() &&
      result.Find("structuredContent")->Find("next_page_token")->IsNull());
  return true;
}
Q4T_TEST(mcp_graph_teams_deleted_vs_unauthorized_body_explicit) {
  for (bool deleted : {false, true}) {
    auto original =
        Object({{"id", String("m1")},
                {"body", Json()},
                {"deletedDateTime",
                 deleted ? String("2026-11-16T00:00:00Z") : Json()}});
    ProviderPeer peer({JsonReply(original)}, kTeams);
    Json result, error;
    Q4T_CHECK(ProviderCall(kTeams, "fetch",
                           Object({{"chat_id", String("c1")},
                                   {"message_id", String("m1")}}),
                           peer, &result, &error)
                  .ok());
    Q4T_CHECK(deleted
                  ? error.IsNull() &&
                        result.Find("structuredContent")->Find("body")->IsNull()
                  : result.IsNull() && error.GetInt("code") == -32000);
  }
  return true;
}
Q4T_TEST(mcp_graph_sharepoint_site_search_and_recent_use_current_api) {
  ProviderPeer peer({JsonReply(Object({{"id", String("tenant,site,web")},
                                       {"displayName", String("项目")}})),
                     JsonReply(GraphHits(true)), JsonReply(GraphHits())},
                    kSharePoint);
  Json result, error;
  Q4T_CHECK(ProviderCall(kSharePoint, "get_site",
                         Object({{"site_id", String("root")}}), peer, &result,
                         &error)
                .ok() &&
            error.IsNull());
  Q4T_CHECK(ProviderCall(kSharePoint, "search",
                         Object({{"query", String("Project")}}), peer, &result,
                         &error)
                .ok() &&
            error.IsNull());
  Q4T_CHECK(ProviderCall(kSharePoint, "list_recent_documents", Object(), peer,
                         &result, &error)
                .ok() &&
            error.IsNull());
  Q4T_CHECK(result.Find("structuredContent")->GetString("recency") ==
            "lastModifiedDateTime");
  const auto requests = peer.Requests();
  Q4T_CHECK(requests[0].starts_with("GET /v1.0/sites/root "));
  for (size_t i = 1; i < 3; ++i) {
    Q4T_CHECK(requests[i].starts_with("POST /v1.0/search/query "));
    Json body;
    Q4T_CHECK(q4t::io::ParseJson(
        requests[i].substr(requests[i].find("\r\n\r\n") + 4), &body));
    const auto& query = body.GetArray("requests")->array[0];
    Q4T_CHECK(query.GetArray("entityTypes")->array[0].str == "driveItem");
    if (i == 1)
      Q4T_CHECK(!query.Has("sortProperties"));
    else {
      Q4T_CHECK(query.Find("query")->GetString("queryString") ==
                "IsDocument:1");
      const auto& sort = query.GetArray("sortProperties")->array[0];
      Q4T_CHECK(sort.GetString("name") == "lastModifiedDateTime" &&
                sort.GetBool("isDescending"));
    }
  }
  return true;
}
Q4T_TEST(mcp_graph_sharepoint_download_actual_http_strips_all_credentials) {
  ProviderPeer peer(
      {JsonReply(SharePointFile()), {200, "real", false, "text/plain"}},
      kSharePoint);
  Json result, error;
  Q4T_CHECK(ProviderCall(kSharePoint, "fetch",
                         Object({{"item_id", String("file1")},
                                 {"drive_id", String("drive+/=")}}),
                         peer, &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull() && ValidateMcpToolResult(result));
  const auto requests = peer.Requests();
  Q4T_CHECK(requests.size() == 2 &&
            requests[0].find("Authorization: Bearer synthetic-private-token") !=
                requests[0].npos);
  Q4T_CHECK(requests[0].find("/drives/drive%2B%2F%3D/items/file1?") !=
            requests[0].npos);
  Q4T_CHECK(requests[1].starts_with("GET /original?secret=cap ") &&
            requests[1].find("Authorization") == requests[1].npos &&
            requests[1].find("fixture-only") == requests[1].npos &&
            requests[1].find("synthetic-private-token") == requests[1].npos);
  Q4T_CHECK(OutputJson(result).find("secret=cap") == std::string::npos &&
            OutputJson(result).find("downloadUrl") == std::string::npos);
  const auto& resource = result.GetArray("content")->array[1];
  Q4T_CHECK(resource.Find("resource")->GetString("blob") == "cmVhbA==");
  return true;
}
Q4T_TEST(mcp_graph_download_policy_never_widens_network_or_credentials) {
  McpEndpoint endpoint;
  endpoint.provider_id = kSharePoint;
  endpoint.authorization = "private-token";
  endpoint.headers = {{"Cookie", "private-cookie"}, {"X-Private", "secret"}};
  FileFetchOptions deployment;
  deployment.allowed_networks = {"127.0.0.1/32", "10.0.0.0/8"};
  deployment.allowed_hosts = {"download.files.1drv.com"};
  deployment.ca_file = "/private/ca.pem";
  mcp_detail::ProviderRequest request{
      "https://download.files.1drv.com/file?private=cap"};
  request.binary = true;
  request.graph_download = true;
  file_detail::Url url;
  file_detail::RemoteHeaders headers;
  FileFetchOptions options;
  Q4T_CHECK(mcp_detail::PrepareProviderRequest(endpoint, deployment, request,
                                               &url, &headers, &options)
                .ok());
  Q4T_CHECK(headers.size() == 1 && headers.at("Accept") == "*/*" &&
            options.allowed_hosts == deployment.allowed_hosts &&
            options.ca_file == deployment.ca_file &&
            options.ports == deployment.ports &&
            options.allowed_networks.empty() && !options.allow_http &&
            !options.max_redirects);
  Q4T_CHECK(!file_detail::AllowedAddress(
      q4t::service::ingress::Address::Parse("127.0.0.1"), options));
  Q4T_CHECK(!file_detail::AllowedAddress(
      q4t::service::ingress::Address::Parse("10.1.2.3"), options));
  deployment.public_networks = false;
  Q4T_CHECK(mcp_detail::PrepareProviderRequest(endpoint, deployment, request,
                                               &url, &headers, &options)
                .ok() &&
            !options.public_networks);
  for (int variant = 0; variant < 6; ++variant) {
    auto bad = request;
    if (variant == 0) bad.url = "http://download.files.1drv.com/file";
    if (variant == 1) bad.url = "https://user@download.files.1drv.com/file";
    if (variant == 2) bad.url = "https://download.files.1drv.com:8443/file";
    if (variant == 3) bad.headers["Authorization"] = "leak";
    if (variant == 4) bad.method = "POST";
    if (variant == 5) bad.token_query = true;
    Q4T_CHECK(!mcp_detail::PrepareProviderRequest(endpoint, deployment, bad,
                                                  &url, &headers, &options)
                   .ok());
  }
  endpoint.provider_id = kOutlookMail;
  Q4T_CHECK(!mcp_detail::PrepareProviderRequest(endpoint, deployment, request,
                                                &url, &headers, &options)
                 .ok());
  return true;
}
Q4T_TEST(mcp_graph_write_methods_and_arbitrary_posts_denied) {
  McpEndpoint endpoint;
  endpoint.provider_id = kTeams;
  endpoint.authorization = "opaque";
  for (const auto* method : {"POST", "PUT", "PATCH", "DELETE"}) {
    mcp_detail::ProviderRequest request{
        "https://graph.microsoft.com/v1.0/chats/c1/messages"};
    request.method = method;
    file_detail::Url url;
    file_detail::RemoteHeaders headers;
    FileFetchOptions options;
    Q4T_CHECK(!mcp_detail::PrepareProviderRequest(endpoint, FileFetchOptions{},
                                                  request, &url, &headers,
                                                  &options)
                   .ok());
  }
  return true;
}
Q4T_TEST(mcp_graph_sharepoint_remote_item_and_folder_do_not_fake_file) {
  auto link = Object(
      {{"id", String("link1")},
       {"name", String("shortcut")},
       {"remoteItem", Object({{"id", String("folder1")},
                              {"parentReference",
                               Object({{"driveId", String("other+/=")}})}})}});
  auto folder = Object({{"id", String("folder1")},
                        {"name", String("资料目录")},
                        {"folder", Object({{"childCount", Number(2)}})}});
  ProviderPeer peer({JsonReply(link), JsonReply(folder)}, kSharePoint);
  Json result, error;
  Q4T_CHECK(ProviderCall(kSharePoint, "fetch",
                         Object({{"item_id", String("link1")}}), peer, &result,
                         &error)
                .ok() &&
            error.IsNull());
  const auto* value = result.Find("structuredContent");
  Q4T_CHECK(value->GetString("kind") == "folder" &&
            value->GetArray("resolved_links")->array[0].GetString("id") ==
                "link1" &&
            result.GetArray("content")->array.size() == 1);
  Q4T_CHECK(peer.Requests()[1].find("/drives/other%2B%2F%3D/items/folder1?") !=
            std::string::npos);
  return true;
}
Q4T_TEST(mcp_graph_sharepoint_size_errors_never_return_partial_content) {
  for (int mode = 0; mode < 3; ++mode) {
    auto file = SharePointFile("file1", mode == 0   ? 5
                                        : mode == 1 ? 5000000
                                                    : 4);
    const Reply reply = mode == 2 ? Reply{403, "private download capability"}
                                  : Reply{200, "real", false, "text/plain"};
    ProviderPeer peer({JsonReply(file), reply}, kSharePoint);
    Json result, error;
    const auto status = ProviderCall(kSharePoint, "fetch",
                                     Object({{"item_id", String("file1")}}),
                                     peer, &result, &error);
    Q4T_CHECK(result.IsNull());
    Q4T_CHECK(mode == 2 ? status.ok() && error.Find("data")->GetInt(
                                             "upstream_status") == 403
                        : !status.ok());
    Q4T_CHECK(peer.Requests().size() == (mode == 1 ? 1 : 2));
  }
  return true;
}
Q4T_TEST(mcp_graph_sharepoint_download_capability_is_not_model_content) {
  auto hits = GraphHits(false, 1, SharePointFile());
  ProviderPeer peer({JsonReply(hits)}, kSharePoint);
  Json result, error;
  Q4T_CHECK(ProviderCall(kSharePoint, "search",
                         Object({{"query", String("project")}}), peer, &result,
                         &error)
                .ok() &&
            error.IsNull());
  Q4T_CHECK(OutputJson(result).find("secret=cap") == std::string::npos &&
            OutputJson(result).find("downloadUrl") == std::string::npos &&
            OutputJson(result).find("tenant.sharepoint.com/record.txt") !=
                std::string::npos);
  return true;
}
Q4T_TEST(mcp_graph_teams_sharepoint_invalid_inputs_never_fetch) {
  for (const auto& [provider, name, args] :
       std::vector<std::tuple<std::string, std::string, Json>>{
           {kTeams, "fetch", Object({{"message_id", String("m1")}})},
           {kTeams, "fetch",
            Object({{"message_id", String("m1")},
                    {"chat_id", String("c1")},
                    {"team_id", String("t1")}})},
           {kTeams, "fetch",
            Object({{"message_id", String("m1")},
                    {"chat_id", String("c1")},
                    {"reply_id", String("r1")}})},
           {kTeams, "get_chat_members",
            Object(
                {{"chat_id", String("c1")},
                 {"next_page_token", String("https://graph.microsoft.com/v1.0/"
                                            "chats/c2/members?$skip=1")}})},
           {kSharePoint, "fetch",
            Object({{"item_id", String("i1")},
                    {"drive_id", String("d1")},
                    {"site_id", String("s1")}})},
           {kSharePoint, "fetch",
            Object({{"item_id", String("i1")},
                    {"url", String("https://evil.invalid/")}})},
           {kSharePoint, "list_recent_documents",
            Object({{"offset", Number(-1)}})},
           {kSharePoint, "get_site", Object({{"site_id", String("..")}})}}) {
    size_t calls = 0;
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  provider, "tools/call", Call(name, args),
                  [&](const auto&, Json*) {
                    ++calls;
                    return ObjectReply{};
                  },
                  &result, &error)
                  .ok());
    Q4T_CHECK(calls == 0 && result.IsNull() && error.GetInt("code") == -32602);
  }
  return true;
}
namespace {
constexpr auto kDropbox = "connector_dropbox";
Json DropboxMetadata(std::string name = "Record.TXT", size_t size = 4) {
  return Object({{"id", String("id:file1")},
                 {"name", String(name)},
                 {"rev", String("a123456789")},
                 {"size", Number(size)},
                 {"path_display", String("/Project/" + name)}});
}
Reply DropboxBytes(Json metadata, std::string body = "real") {
  return {200,
          std::move(body),
          false,
          "application/octet-stream",
          {{"Dropbox-API-Result", OutputJson(metadata)}}};
}
Json DropboxMatches(bool more = false) {
  auto file = DropboxMetadata();
  Set(&file, ".tag", String("file"));
  return Object(
      {{"matches",
        Array({Object(
            {{"metadata",
              Object({{".tag", String("metadata")}, {"metadata", file}})},
             {"match_type", Object({{".tag", String("file_content")}})}})})},
       {"has_more", Boolean(more)},
       {"cursor", more ? String("cursor+/=") : Json()}});
}
Json RequestJson(const std::string& request) {
  Json body;
  q4t::io::ParseJson(request.substr(request.find("\r\n\r\n") + 4), &body);
  return body;
}
}  // namespace
Q4T_TEST(mcp_dropbox_catalog_schema_and_independent_permissions) {
  size_t calls = 0;
  Json result, error;
  Q4T_CHECK(mcp_detail::InvokeProvider(
                kDropbox, "tools/list", Object(),
                [&](const auto&, Json*) {
                  ++calls;
                  return ObjectReply{};
                },
                &result, &error)
                .ok());
  const auto* tools = result.GetArray("tools");
  Q4T_CHECK(calls == 0 && error.IsNull() && tools && tools->array.size() == 6);
  for (const auto& tool : tools->array) {
    OutputSchemaGraph schema;
    std::string param;
    Q4T_CHECK(schema.Init(*tool.Find("inputSchema"), false, &param, "schema",
                          OutputSchemaPurpose::kToolResult));
    const auto name = tool.GetString("name");
    auto args = Object();
    if (name == "search" || name == "search_files")
      Set(&args, "query", String("project"));
    if (name == "fetch" || name == "fetch_file")
      Set(&args, "path", String("/项目.txt"));
    Q4T_CHECK(schema.Check(args));
  }
  ProviderPeer peer(
      {{403, "private profile denial"}, DropboxBytes(DropboxMetadata())},
      kDropbox);
  Q4T_CHECK(
      ProviderCall(kDropbox, "get_profile", Object(), peer, &result, &error)
          .ok());
  Q4T_CHECK(error.Find("data")->GetInt("upstream_status") == 403);
  Q4T_CHECK(ProviderCall(kDropbox, "fetch_file",
                         Object({{"path", String("id:file1")}}), peer, &result,
                         &error)
                .ok() &&
            error.IsNull());
  Q4T_CHECK(peer.Requests().size() == 2 &&
            peer.Requests()[1].starts_with("GET /2/files/download "));
  return true;
}
Q4T_TEST(mcp_dropbox_profile_uses_original_account_fields) {
  auto account = Object({{"account_id", String("dbid:original")},
                         {"name", Object({{"display_name", String("原用户")}})},
                         {"email_verified", Boolean(false)}});
  ProviderPeer peer({JsonReply(account)}, kDropbox);
  Json result, error;
  Q4T_CHECK(
      ProviderCall(kDropbox, "get_profile", Object(), peer, &result, &error)
          .ok());
  Q4T_CHECK(error.IsNull() && OutputJson(*result.Find("structuredContent")) ==
                                  OutputJson(account));
  Q4T_CHECK(
      peer.Requests()[0].starts_with("POST /2/users/get_current_account ") &&
      RequestJson(peer.Requests()[0]).IsNull());
  return true;
}
Q4T_TEST(mcp_dropbox_search_cursor_and_recent_preserve_contract) {
  ProviderPeer peer(
      {JsonReply(DropboxMatches(true)),
       JsonReply(Object({{"matches", Array()}, {"has_more", Boolean(false)}})),
       JsonReply(DropboxMatches())},
      kDropbox);
  Json result, error;
  Q4T_CHECK(ProviderCall(kDropbox, "search",
                         Object({{"query", String("项目")},
                                 {"path", String("ns:123/Project")},
                                 {"max_results", Number(3)}}),
                         peer, &result, &error)
                .ok());
  Q4T_CHECK(error.IsNull() &&
            result.Find("structuredContent")->GetString("next_page_token") ==
                "cursor+/=");
  Q4T_CHECK(ProviderCall(kDropbox, "search_files",
                         Object({{"next_page_token", String("cursor+/=")}}),
                         peer, &result, &error)
                .ok() &&
            error.IsNull());
  Q4T_CHECK(
      result.Find("structuredContent")->GetArray("matches")->array.empty());
  Q4T_CHECK(ProviderCall(kDropbox, "list_recent_files", Object(), peer, &result,
                         &error)
                .ok() &&
            error.IsNull());
  Q4T_CHECK(result.Find("structuredContent")->GetString("recency") ==
            "last_modified_time");
  const auto requests = peer.Requests();
  Q4T_CHECK(RequestJson(requests[0]).GetString("query") == "项目");
  Q4T_CHECK(RequestJson(requests[0]).Find("options")->GetString("path") ==
            "ns:123/Project");
  Q4T_CHECK(requests[1].starts_with("POST /2/files/search/continue_v2 ") &&
            RequestJson(requests[1]).GetString("cursor") == "cursor+/=");
  const auto recent = RequestJson(requests[2]);
  Q4T_CHECK(recent.GetString("query") == "" &&
            recent.Find("options")->GetString("order_by") ==
                "last_modified_time");
  for (const auto& category :
       recent.Find("options")->GetArray("file_categories")->array)
    Q4T_CHECK(category.str != "folder");
  return true;
}
Q4T_TEST(mcp_dropbox_unicode_path_header_is_ascii_and_roundtrips) {
  const std::string path = "/项目/🧪\r\n\"quoted\".txt";
  ProviderPeer peer({DropboxBytes(DropboxMetadata())}, kDropbox);
  Json result, error;
  Q4T_CHECK(ProviderCall(kDropbox, "fetch", Object({{"path", String(path)}}),
                         peer, &result, &error)
                .ok() &&
            error.IsNull());
  const auto request = peer.Requests()[0];
  const auto begin = request.find("Dropbox-API-Arg: ") + 17;
  const auto encoded =
      request.substr(begin, request.find("\r\n", begin) - begin);
  for (unsigned char c : encoded) Q4T_CHECK(c >= 32 && c < 128);
  Json argument;
  Q4T_CHECK(q4t::io::ParseJson(encoded, &argument) &&
            argument.GetString("path") == path);
  Q4T_CHECK(result.Find("structuredContent")->GetString("content_mime_type") ==
            "text/plain");
  Q4T_CHECK(result.GetArray("content")
                ->array[1]
                .Find("resource")
                ->GetString("blob") == "cmVhbA==");
  return true;
}
Q4T_TEST(mcp_dropbox_revision_download_checks_returned_revision) {
  ProviderPeer peer({DropboxBytes(DropboxMetadata())}, kDropbox);
  Json result, error;
  Q4T_CHECK(ProviderCall(kDropbox, "fetch_file",
                         Object({{"path", String("rev:a123456789")}}), peer,
                         &result, &error)
                .ok() &&
            error.IsNull());
  Q4T_CHECK(result.Find("structuredContent")
                ->Find("file_metadata")
                ->GetString("rev") == "a123456789");
  return true;
}
Q4T_TEST(mcp_dropbox_export_only_file_reads_actual_export_once) {
  const auto file = DropboxMetadata("sheet.gsheet", 0);
  const auto metadata =
      Object({{"file_metadata", file},
              {"export_metadata",
               Object({{"name", String("sheet.xlsx")}, {"size", Number(4)}})}});
  ProviderPeer peer(
      {{409,
        R"({"error_summary":"private detail","error":{".tag":"unsupported_file"}})"},
       DropboxBytes(metadata, std::string("PK\x03\x04", 4))},
      kDropbox);
  Json result, error;
  Q4T_CHECK(ProviderCall(kDropbox, "fetch",
                         Object({{"path", String("id:file1")}}), peer, &result,
                         &error)
                .ok() &&
            error.IsNull());
  const auto* value = result.Find("structuredContent");
  Q4T_CHECK(value->GetBool("exported") &&
            value->Find("export_metadata")->GetString("name") == "sheet.xlsx");
  Q4T_CHECK(
      value->GetString("content_mime_type") ==
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
  Q4T_CHECK(peer.Requests().size() == 2 &&
            peer.Requests()[1].starts_with("GET /2/files/export "));
  Q4T_CHECK(OutputJson(result).find("private detail") == std::string::npos);
  return true;
}
Q4T_TEST(mcp_dropbox_folder_fallback_preserves_metadata) {
  const auto folder = Object({{".tag", String("folder")},
                              {"id", String("id:folder1")},
                              {"name", String("Project")}});
  ProviderPeer peer(
      {{409, R"({"error":{".tag":"path","path":{".tag":"not_file"}}})"},
       JsonReply(folder)},
      kDropbox);
  Json result, error;
  Q4T_CHECK(ProviderCall(kDropbox, "fetch",
                         Object({{"path", String("id:folder1")}}), peer,
                         &result, &error)
                .ok() &&
            error.IsNull());
  Q4T_CHECK(result.Find("structuredContent")->GetString("kind") == "folder" &&
            result.GetArray("content")->array.size() == 1 &&
            peer.Requests()[1].starts_with("POST /2/files/get_metadata "));
  return true;
}
Q4T_TEST(mcp_dropbox_known_errors_never_retry_or_expose_private_body) {
  for (const auto& fail : std::vector<Reply>{
           {401, "private-token"},
           {403, "private-token"},
           {429, "private-token"},
           {409, R"({"error":{".tag":"path","path":{".tag":"not_found"}}})"},
           {409, "private-malformed-json"},
           {409, "private-truncated", true}}) {
    ProviderPeer peer({fail}, kDropbox);
    Json result, error;
    Q4T_CHECK(ProviderCall(kDropbox, "fetch",
                           Object({{"path", String("id:file1")}}), peer,
                           &result, &error)
                  .ok());
    Q4T_CHECK(error.Find("data")->GetInt("upstream_status") == fail.status &&
              result.IsNull() && peer.Requests().size() == 1 &&
              OutputJson(error).find("private-") == std::string::npos);
  }
  return true;
}
Q4T_TEST(mcp_dropbox_header_size_identity_and_truncation_are_not_success) {
  for (int mode = 0; mode < 5; ++mode) {
    auto metadata = DropboxMetadata();
    if (mode == 2) Set(&metadata, "size", Number(5));
    if (mode == 3) Set(&metadata, "id", String("id:other"));
    auto response = DropboxBytes(metadata);
    if (mode == 0) response.headers.clear();
    if (mode == 1) response.headers["Dropbox-API-Result"] = "{bad-json";
    if (mode == 4) response.omit_body = true;
    ProviderPeer peer({response}, kDropbox);
    Json result, error;
    Q4T_CHECK(!ProviderCall(kDropbox, "fetch",
                            Object({{"path", String("id:file1")}}), peer,
                            &result, &error)
                   .ok());
    Q4T_CHECK(result.IsNull() && peer.Requests().size() == 1);
  }
  return true;
}
Q4T_TEST(mcp_dropbox_header_metadata_charged_to_shared_budget) {
  const auto metadata = OutputJson(DropboxMetadata());
  std::unique_ptr<file_detail::RemoteHttp> response;
  Q4T_CHECK(file_detail::RemoteHttp::Buffered(
                200,
                {{"content-type", "application/octet-stream"},
                 {"dropbox-api-result", metadata}},
                "real", &response)
                .ok());
  uint64_t bytes = (4U << 20) - metadata.size() - 3;
  Json value;
  Q4T_CHECK(
      mcp_detail::ReadProviderResponse(*response, &bytes, &value, true, true)
          .http_status == 413);
  Q4T_CHECK(value.IsNull());
  return true;
}
Q4T_TEST(mcp_dropbox_fixed_endpoints_reject_uploads_and_other_providers) {
  Q4T_CHECK(mcp_detail::ProviderOrigin(
      kDropbox, "https://api.dropboxapi.com/2/files/search_v2"));
  for (const auto* url :
       {"https://api.dropboxapi.com.evil.invalid/",
        "http://api.dropboxapi.com/", "https://graph.microsoft.com/v1.0/me"})
    Q4T_CHECK(!mcp_detail::ProviderOrigin(kDropbox, url));
  McpEndpoint endpoint;
  endpoint.provider_id = kDropbox;
  endpoint.authorization = "opaque";
  for (const auto* url : {"https://content.dropboxapi.com/2/files/upload",
                          "https://api.dropboxapi.com/2/files/delete_v2",
                          "https://api.dropboxapi.com/2/users/"
                          "get_current_account?override=yes"}) {
    mcp_detail::ProviderRequest request{url};
    request.method = "POST";
    request.body = "{}";
    file_detail::Url parsed;
    file_detail::RemoteHeaders headers;
    FileFetchOptions options;
    Q4T_CHECK(!mcp_detail::PrepareProviderRequest(endpoint, FileFetchOptions{},
                                                  request, &parsed, &headers,
                                                  &options)
                   .ok());
  }
  return true;
}
Q4T_TEST(mcp_dropbox_invalid_arguments_and_changed_cursor_do_no_io) {
  for (const auto& [name, args] : std::vector<std::pair<std::string, Json>>{
           {"fetch", Object({{"path", String("https://evil.invalid/file")}})},
           {"fetch", Object({{"path", String("rev:bad")}})},
           {"fetch", Object({{"path", String(std::string("/nul\0byte", 9))}})},
           {"search", Object()},
           {"search_files", Object({{"next_page_token", String("cursor")},
                                    {"query", String("different")}})},
           {"list_recent_files", Object({{"max_results", Number(1001)}})}}) {
    size_t calls = 0;
    Json result, error;
    Q4T_CHECK(mcp_detail::InvokeProvider(
                  kDropbox, "tools/call", Call(name, args),
                  [&](const auto&, Json*) {
                    ++calls;
                    return ObjectReply{};
                  },
                  &result, &error)
                  .ok());
    Q4T_CHECK(calls == 0 && result.IsNull() && error.GetInt("code") == -32602);
  }
  return true;
}
Q4T_TEST(mcp_dropbox_malformed_search_response_never_fabricates_matches) {
  for (const auto& raw : std::vector<Json>{
           Object({{"matches", Array()}, {"has_more", Boolean(true)}}),
           Object(
               {{"matches", Array({Object()})}, {"has_more", Boolean(false)}}),
           Object({{"matches", Array()}, {"has_more", String("false")}})}) {
    ProviderPeer peer({JsonReply(raw)}, kDropbox);
    Json result, error;
    Q4T_CHECK(!ProviderCall(kDropbox, "search",
                            Object({{"query", String("x")}}), peer, &result,
                            &error)
                   .ok());
    Q4T_CHECK(result.IsNull());
  }
  return true;
}
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
