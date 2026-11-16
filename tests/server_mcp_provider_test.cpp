#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <mutex>
#include <thread>
#include <tuple>

#include "../src/server/mcp_provider_internal.h"
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
            " Result\r\nContent-Type: " + reply.mime +
            "\r\nContent-Length: " + std::to_string(reply.body.size()) +
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
    if (!mcp_detail::ProviderOrigin(provider_, request.url))
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
    for (const auto& [key, value] : request.headers) headers[key] = value;
    if (request.token_query)
      local.target += "?access_token=synthetic-private-token";
    else
      headers["Authorization"] = "Bearer synthetic-private-token";
    std::unique_ptr<file_detail::RemoteHttp> response;
    auto reply = file_detail::RemoteHttp::Open(
        local, options, "GET", headers, "", (4U << 20) - bytes_,
        std::chrono::steady_clock::now() + 2s, &response);
    return reply.ok() ? mcp_detail::ReadProviderResponse(*response, &bytes_,
                                                         output, request.binary)
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
