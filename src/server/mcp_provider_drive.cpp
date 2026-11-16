#include "mcp_provider_common.h"

#include <algorithm>
#include <map>
#include <set>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
using namespace provider_detail;
constexpr std::string_view kDrive = "https://www.googleapis.com/drive/v3/";
constexpr std::string_view kFileFields =
    "id,name,mimeType,description,size,createdTime,modifiedTime,webViewLink,"
    "resourceKey,shortcutDetails,capabilities(canDownload),trashed";
std::string Literal(std::string_view input) {
  std::string value = "'";
  for (const auto c : input) {
    if (c == '\\' || c == '\'') value += '\\';
    value += c;
  }
  return value + '\'';
}
ObjectReply Catalog(const ProviderFetch& fetch, Json* result) {
  std::set<std::string> scopes;
  auto reply = GoogleScopes(fetch, &scopes);
  if (!reply.ok()) return reply;
  const bool all = Scope(scopes, "drive") || Scope(scopes, "drive.readonly");
  const bool content = all || Scope(scopes, "drive.file");
  const bool metadata = content || Scope(scopes, "drive.metadata") ||
                        Scope(scopes, "drive.metadata.readonly");
  auto tools = Array();
  if (metadata) {
    tools.array.push_back(Tool("get_profile", "Read the authorized Drive user",
                               Object(), Array()));
    auto properties = Object({{"query", TextSchema(4096)},
                              {"drive_id", TextSchema(1024)},
                              {"folder_id", TextSchema(1024)},
                              {"max_results", LimitSchema(1000)},
                              {"next_page_token", TextSchema(8192)}});
    tools.array.push_back(Tool("search",
                               "Search Drive file content and names; "
                               "query is literal text, not Drive query syntax",
                               properties, Array()));
    std::erase_if(properties.object,
                  [](const auto& item) { return item.first == "query"; });
    tools.array.push_back(Tool("recent_documents",
                               "List one page of files "
                               "ordered by modification time",
                               properties, Array()));
  }
  if (all)
    tools.array.push_back(Tool("list_drives", "List accessible shared drives",
                               Object({{"max_results", LimitSchema(100)},
                                       {"next_page_token", TextSchema(8192)}}),
                               Array()));
  if (content)
    tools.array.push_back(
        Tool("fetch",
             "Read original file metadata and "
             "content, exporting native documents without dropping sheets. "
             "Pending Vids downloads return resume_arguments; pass them back "
             "after retry_after_seconds to poll that operation once.",
             Object({{"file_id", TextSchema(1024, false)},
                     {"resource_key", TextSchema(1024)},
                     {"operation_name", TextSchema(2048)}}),
             Array({String("file_id")})));
  *result = Object({{"tools", std::move(tools)}});
  return {};
}
ProviderRequest FileRequest(std::string url, std::string_view id,
                            std::string_view resource_key,
                            bool binary = false) {
  ProviderRequest request{std::move(url), false, binary};
  if (!resource_key.empty())
    request.headers["X-Goog-Drive-Resource-Keys"] =
        std::string(id) + '/' + std::string(resource_key);
  return request;
}
ObjectReply FetchFile(const Json& args, const ProviderFetch& fetch, Json* value,
                      Json* content) {
  std::string field;
  if (!InputFields(args, {"file_id", "resource_key", "operation_name"},
                   &field) ||
      !OptionalText(args, "file_id", 1024) ||
      !OptionalText(args, "resource_key", 1024) ||
      !OptionalText(args, "operation_name", 2048))
    return Invalid();
  auto id = Text(args, "file_id"), key = Text(args, "resource_key");
  if (!Identifier(id) || (!key.empty() && !Identifier(key))) return Invalid();
  const auto operation = Text(args, "operation_name");
  if (!operation.empty()) {
    // The operation identifies its content. Don't query unrelated current file
    // metadata and claim that it proves the identity of a resumed operation.
    *value = Object();
    return GoogleDriveDownload(id, key, operation, fetch, value, content);
  }
  const auto requested = id;
  auto shortcuts = Array();
  std::set<std::string> seen;
  for (unsigned hop = 0; hop < 4; ++hop) {
    if (!seen.insert(id).second) return Protocol();
    std::string base = std::string(kDrive) + "files/" + ProviderEscape(id);
    std::string url = base;
    Query(&url, "supportsAllDrives", "true");
    Query(&url, "fields", kFileFields);
    Json file;
    auto reply = fetch(FileRequest(url, id, key), &file);
    if (!reply.ok()) return reply;
    if (!file.IsObject() || file.GetString("id") != id ||
        file.GetString("name").empty() || file.GetString("mimeType").empty())
      return Protocol();
    const auto mime = file.GetString("mimeType");
    if (mime == "application/vnd.google-apps.shortcut") {
      const auto* target = file.Find("shortcutDetails");
      if (!target || !target->IsObject()) return Protocol();
      id = target->GetString("targetId");
      key = target->GetString("targetResourceKey");
      if (!Identifier(id) || (!key.empty() && !Identifier(key)))
        return Protocol();
      shortcuts.array.push_back(std::move(file));
      continue;
    }
    *value = Object({{"requested_file_id", String(requested)},
                     {"file", file},
                     {"shortcuts", std::move(shortcuts)}});
    if (mime == "application/vnd.google-apps.folder") {
      // A folder is metadata, not an empty document. Search's folder_id
      // selects its children explicitly and preserves pagination.
      Set(value, "kind", String("folder"));
      return {};
    }
    if (const auto* capabilities = file.Find("capabilities");
        capabilities && capabilities->Find("canDownload") &&
        !capabilities->GetBool("canDownload"))
      return Error(403, "mcp_provider_access_denied",
                   "The provider does not permit downloading this file");
    if (mime == "application/vnd.google-apps.vid")
      return GoogleDriveDownload(id, key, {}, fetch, value, content);
    std::string exported = mime;
    if (mime.starts_with("application/vnd.google-apps.")) {
      static const std::map<std::string, std::string> formats{
          {"application/vnd.google-apps.document",
           "application/"
           "vnd.openxmlformats-officedocument.wordprocessingml.document"},
          {"application/vnd.google-apps.spreadsheet",
           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
          {"application/vnd.google-apps.presentation",
           "application/"
           "vnd.openxmlformats-officedocument.presentationml.presentation"},
          {"application/vnd.google-apps.drawing", "image/png"},
          {"application/vnd.google-apps.script",
           "application/vnd.google-apps.script+json"}};
      const auto found = formats.find(mime);
      exported = found == formats.end() ? "application/pdf" : found->second;
      url = base + "/export";
      Query(&url, "mimeType", exported);
    } else {
      url = base;
      Query(&url, "alt", "media");
      Query(&url, "supportsAllDrives", "true");
    }
    Json bytes;
    reply = fetch(FileRequest(url, id, key, true), &bytes);
    if (!reply.ok()) return reply;
    const auto* data = bytes.Find("data");
    if (!data || !data->IsString()) return Protocol();
    content->array.push_back(
        Resource("q4t-provider://googledrive/" + ProviderEscape(id), exported,
                 data->str));
    Set(value, "content_mime_type", String(exported));
    Set(value, "kind", String("file"));
    return {};
  }
  return Capacity();
}
ObjectReply Read(std::string_view name, const Json& args,
                 const ProviderFetch& fetch, Json* value, Json* content) {
  std::string field;
  if (name == "get_profile") {
    if (!InputFields(args, {}, &field)) return Invalid();
    auto reply = fetch({std::string(kDrive) + "about?fields=user"}, value);
    if (!reply.ok()) return reply;
    const auto* user = value->Find("user");
    if (!user || !user->IsObject()) return Protocol();
    return {};
  }
  if (name == "fetch") return FetchFile(args, fetch, value, content);
  const bool drives = name == "list_drives";
  if (!drives && name != "search" && name != "recent_documents")
    return Invalid();
  if (!(drives ? InputFields(args, {"max_results", "next_page_token"}, &field)
        : name == "search" ? InputFields(args,
                                         {"query", "drive_id", "folder_id",
                                          "max_results", "next_page_token"},
                                         &field)
                           : InputFields(args,
                                         {"drive_id", "folder_id",
                                          "max_results", "next_page_token"},
                                         &field)))
    return Invalid();
  int maximum = 0;
  if (!Limit(args, 50, drives ? 100 : 1000, &maximum) ||
      !OptionalText(args, "next_page_token", 8192) ||
      !OptionalText(args, "query", 4096) ||
      !OptionalText(args, "drive_id", 1024) ||
      !OptionalText(args, "folder_id", 1024))
    return Invalid();
  const auto drive = Text(args, "drive_id"), folder = Text(args, "folder_id");
  if ((!drive.empty() && !Identifier(drive)) ||
      (!folder.empty() && !Identifier(folder)))
    return Invalid();
  std::string url = std::string(kDrive) + (drives ? "drives" : "files");
  Query(&url, "pageSize", std::to_string(maximum));
  if (!Text(args, "next_page_token").empty())
    Query(&url, "pageToken", Text(args, "next_page_token"));
  if (!drives) {
    std::string query = "trashed = false";
    if (!Text(args, "query").empty())
      query += " and fullText contains " + Literal(Text(args, "query"));
    if (!folder.empty()) query += " and " + Literal(folder) + " in parents";
    Query(&url, "q", query);
    Query(&url, "orderBy", "modifiedTime desc");
    Query(&url, "supportsAllDrives", "true");
    Query(&url, "includeItemsFromAllDrives", "true");
    Query(&url, "corpora", drive.empty() ? "user" : "drive");
    if (!drive.empty()) Query(&url, "driveId", drive);
    Query(&url, "fields",
          "nextPageToken,incompleteSearch,files(" + std::string(kFileFields) +
              ')');
  } else {
    Query(&url, "fields", "nextPageToken,drives(id,name,createdTime)");
  }
  Json response;
  auto reply = fetch({url}, &response);
  return reply.ok()
             ? Page(response, drives ? "drives" : "files", maximum, value)
             : reply;
}
}  // namespace
ObjectReply GoogleDriveProvider(std::string_view method, const Json& params,
                                const ProviderFetch& fetch, Json* result) {
  if (method == "tools/list") {
    if (!params.IsObject() || !params.object.empty()) return Invalid();
    return Catalog(fetch, result);
  }
  const auto* args = params.Find("arguments");
  if (method != "tools/call" || !params.IsObject() || !args ||
      !args->IsObject())
    return Invalid();
  auto content = Array();
  Json value;
  auto reply = Read(params.GetString("name"), *args, fetch, &value, &content);
  return reply.ok() ? Result(std::move(value), std::move(content), result)
                    : reply;
}
}  // namespace q4t::server::mcp_detail
