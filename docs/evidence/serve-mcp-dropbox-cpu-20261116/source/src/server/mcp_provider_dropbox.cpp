#include "mcp_provider_common.h"

#include <algorithm>
#include <cstdio>

#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
using namespace provider_detail;
constexpr std::string_view kApi = "https://api.dropboxapi.com/2/";
constexpr std::string_view kContent = "https://content.dropboxapi.com/2/files/";
bool Path(std::string_view path, bool root = false) {
  if (path.size() > 2048 || path.find('\0') != path.npos ||
      detail::NormalizeUtf8(path) != path)
    return false;
  if (path.empty()) return root;
  if (path.front() == '/') return true;
  if (path.starts_with("id:")) return path.size() > 3;
  if (path.starts_with("rev:"))
    return path.size() >= 13 &&
           std::all_of(path.begin() + 4, path.end(), [](char c) {
             return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
           });
  if (path.starts_with("ns:")) {
    const auto slash = path.find('/', 3);
    const auto id = path.substr(3, slash == path.npos ? path.npos : slash - 3);
    return !id.empty() && std::all_of(id.begin(), id.end(), [](char c) {
      return c >= '0' && c <= '9';
    });
  }
  return false;
}
// Dropbox arguments in headers must be ASCII JSON. The normal serializer
// already escapes quotes/control bytes; convert only validated UTF-8 scalars.
std::string HeaderJson(const Json& value) {
  const auto json = OutputJson(value);
  if (detail::NormalizeUtf8(json) != json) return {};
  std::string out;
  for (size_t i = 0; i < json.size();) {
    const auto c = static_cast<unsigned char>(json[i++]);
    if (c < 128) {
      out += c;
      continue;
    }
    const unsigned count = c < 224 ? 1 : c < 240 ? 2 : 3;
    uint32_t cp = c & (count == 1 ? 31 : count == 2 ? 15 : 7);
    for (unsigned j = 0; j < count; ++j)
      cp = (cp << 6) | (static_cast<unsigned char>(json[i++]) & 63);
    char encoded[16];
    if (cp <= 0xffff)
      std::snprintf(encoded, sizeof(encoded), "\\u%04x", cp);
    else {
      cp -= 0x10000;
      std::snprintf(encoded, sizeof(encoded), "\\u%04x\\u%04x",
                    0xd800 + (cp >> 10), 0xdc00 + (cp & 1023));
    }
    out += encoded;
  }
  return out;
}
ProviderRequest Rpc(std::string_view route, Json args) {
  ProviderRequest request{std::string(kApi) + std::string(route)};
  request.method = "POST";
  request.body = OutputJson(args);
  return request;
}
ProviderRequest Download(std::string_view route, std::string_view path) {
  ProviderRequest request{std::string(kContent) + std::string(route)};
  request.binary = true;
  request.dropbox_content = true;
  request.headers["Dropbox-API-Arg"] =
      HeaderJson(Object({{"path", String(std::string(path))}}));
  return request;
}
std::string Mime(std::string_view filename, std::string_view actual) {
  if (!actual.empty() && actual != "application/octet-stream")
    return std::string(actual);
  const auto dot = filename.rfind('.');
  const auto extension =
      dot == filename.npos ? "" : http::Lower(filename.substr(dot + 1));
  // MIME is a decoder hint, never proof of valid content. The existing media
  // and document workers still validate the original bytes.
  static const std::map<std::string, std::string> types{
      {"txt", "text/plain"},
      {"md", "text/markdown"},
      {"csv", "text/csv"},
      {"tsv", "text/tab-separated-values"},
      {"json", "application/json"},
      {"html", "text/html"},
      {"htm", "text/html"},
      {"pdf", "application/pdf"},
      {"rtf", "application/rtf"},
      {"doc", "application/msword"},
      {"xls", "application/vnd.ms-excel"},
      {"ppt", "application/vnd.ms-powerpoint"},
      {"docx",
       "application/"
       "vnd.openxmlformats-officedocument.wordprocessingml.document"},
      {"xlsx",
       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
      {"pptx",
       "application/"
       "vnd.openxmlformats-officedocument.presentationml.presentation"},
      {"odt", "application/vnd.oasis.opendocument.text"},
      {"ods", "application/vnd.oasis.opendocument.spreadsheet"},
      {"odp", "application/vnd.oasis.opendocument.presentation"},
      {"png", "image/png"},
      {"jpg", "image/jpeg"},
      {"jpeg", "image/jpeg"},
      {"gif", "image/gif"},
      {"webp", "image/webp"},
      {"wav", "audio/wav"},
      {"mp3", "audio/mpeg"},
      {"ogg", "audio/ogg"},
      {"flac", "audio/flac"},
      {"mp4", "video/mp4"},
      {"webm", "video/webm"}};
  const auto found = types.find(extension);
  return found == types.end() ? "application/octet-stream" : found->second;
}
bool FileMetadata(const Json& value) {
  const auto* size = value.Find("size");
  return value.IsObject() && !value.GetString("id").empty() &&
         !value.GetString("name").empty() && size && IsInputInteger(*size) &&
         size->number >= 0 &&
         (!value.Has(".tag") || value.GetString(".tag") == "file");
}
ObjectReply Fetch(const Json& args, const ProviderFetch& fetch, Json* value,
                  Json* content) {
  std::string field;
  const auto* path = args.Find("path");
  if (!InputFields(args, {"path"}, &field) || !path || !path->IsString() ||
      !Path(path->str))
    return Invalid();
  Json bytes;
  auto reply = fetch(Download("download", path->str), &bytes);
  bool exported = false;
  if (!reply.ok()) {
    const auto reason = reply.value.GetString("provider_reason");
    if (reason == "export_required") {
      // One explicit alternate read format after a known rejection, never
      // retry a timeout, ambiguous transport loss, permission or quota error.
      exported = true;
      reply = fetch(Download("export", path->str), &bytes);
    } else if (reason == "folder") {
      reply =
          fetch(Rpc("files/get_metadata", Object({{"path", *path}})), value);
      if (!reply.ok()) return reply;
      if (value->GetString(".tag") != "folder" ||
          value->GetString("id").empty() || value->GetString("name").empty())
        return Protocol();
      if (path->str.starts_with("id:") && value->GetString("id") != path->str)
        return Protocol();
      Set(value, "kind", String("folder"));
      return {};
    }
  }
  if (!reply.ok()) return reply;
  const auto* metadata = bytes.Find("metadata");
  const auto* data = bytes.Find("data");
  const auto* size = bytes.Find("size");
  if (!metadata || !metadata->IsObject() || !data || !data->IsString() ||
      !size || !IsInputInteger(*size) || size->number < 0)
    return Protocol();
  const auto* file = exported ? metadata->Find("file_metadata") : metadata;
  const auto* actual = exported ? metadata->Find("export_metadata") : metadata;
  const auto* length = actual ? actual->Find("size") : nullptr;
  if (!file || !FileMetadata(*file) || !actual || !actual->IsObject() ||
      actual->GetString("name").empty() || !length ||
      !IsInputInteger(*length) || length->number != size->number)
    return Protocol();
  if ((path->str.starts_with("id:") && file->GetString("id") != path->str) ||
      (path->str.starts_with("rev:") &&
       file->GetString("rev") != path->str.substr(4)))
    return Protocol();
  *value = Object({{"file_metadata", *file},
                   {"kind", String("file")},
                   {"exported", Boolean(exported)}});
  if (exported) Set(value, "export_metadata", *actual);
  const auto mime =
      Mime(actual->GetString("name"), bytes.GetString("mimeType"));
  Set(value, "content_mime_type", String(mime));
  content->array.push_back(Resource(
      "q4t-provider://dropbox/" + ProviderEscape(file->GetString("id")), mime,
      data->str));
  return {};
}
Json SearchSchema() {
  return Object({{"query", Object({{"type", String("string")},
                                   {"maxLength", Number(1000)}})},
                 {"path", TextSchema(2048)},
                 {"max_results", LimitSchema(1000)},
                 {"next_page_token", TextSchema(8192)}});
}
ObjectReply Search(bool recent, const Json& args, const ProviderFetch& fetch,
                   Json* result) {
  std::string field;
  if (!InputFields(args, {"query", "path", "max_results", "next_page_token"},
                   &field))
    return Invalid();
  const auto cursor = Text(args, "next_page_token");
  if (!OptionalText(args, "next_page_token", 8192)) return Invalid();
  int maximum = 1000;
  ProviderRequest request;
  if (!cursor.empty()) {
    // The provider cursor owns all filters and the original page size.
    for (const auto* key : {"query", "path", "max_results"})
      if (args.Has(key) && !args.Find(key)->IsNull()) return Invalid();
    request =
        Rpc("files/search/continue_v2", Object({{"cursor", String(cursor)}}));
  } else {
    const auto* query = args.Find("query");
    const auto* path = args.Find("path");
    if ((!recent && !query) ||
        (query && (!query->IsString() || query->str.size() > 1000 ||
                   query->str.find('\0') != query->str.npos ||
                   detail::NormalizeUtf8(query->str) != query->str)) ||
        (path && !path->IsNull() &&
         (!path->IsString() || !Path(path->str, true))) ||
        !Limit(args, 100, 1000, &maximum))
      return Invalid();
    auto options = Object(
        {{"max_results", Number(maximum)}, {"file_status", String("active")}});
    if (path && path->IsString()) Set(&options, "path", *path);
    if (recent) {
      Set(&options, "order_by", String("last_modified_time"));
      Set(&options, "file_categories",
          Array({String("image"), String("document"), String("pdf"),
                 String("spreadsheet"), String("presentation"), String("audio"),
                 String("video"), String("paper"), String("others")}));
    }
    request =
        Rpc("files/search_v2", Object({{"query", query ? *query : String("")},
                                       {"options", std::move(options)}}));
  }
  Json raw;
  auto reply = fetch(request, &raw);
  if (!reply.ok()) return reply;
  const auto* matches = raw.GetArray("matches");
  const auto* more = raw.Find("has_more");
  const auto* next = raw.Find("cursor");
  if (!matches || matches->array.size() > static_cast<size_t>(maximum) ||
      !more || !more->IsBool() ||
      (next && !next->IsNull() &&
       (!next->IsString() || next->str.empty() || next->str.size() > 8192)) ||
      (more->boolean && (!next || !next->IsString())))
    return Protocol();
  for (const auto& match : matches->array) {
    const auto* wrapped = match.Find("metadata");
    const auto* metadata = wrapped ? wrapped->Find("metadata") : nullptr;
    if (!wrapped || wrapped->GetString(".tag") != "metadata" || !metadata ||
        !metadata->IsObject() || metadata->GetString("name").empty() ||
        metadata->GetString("id").empty() ||
        (metadata->GetString(".tag") != "file" &&
         metadata->GetString(".tag") != "folder"))
      return Protocol();
  }
  *result = std::move(raw);
  Set(result, "next_page_token", more->boolean ? *next : Json());
  if (recent) Set(result, "recency", String("last_modified_time"));
  return {};
}
Json Catalog() {
  auto tools =
      Array({Tool("get_profile",
                  "Read the current Dropbox account; "
                  "account_info.read is separate from file permissions",
                  Object(), Array())});
  for (const auto* name : {"search", "search_files", "list_recent_files"})
    tools.array.push_back(
        Tool(name,
             std::string_view(name) == "list_recent_files"
                 ? "List indexed files by last modification time; optional "
                   "query/path filter. "
                   "For continuation supply only next_page_token."
                 : "Search Dropbox files/folders by query (may be empty), "
                   "optional path and "
                   "page limit. For continuation supply only next_page_token.",
             SearchSchema(), Array()));
  for (const auto* name : {"fetch", "fetch_file"})
    tools.array.push_back(Tool(
        name,
        "Read original bytes by Dropbox path, id:, "
        "ns: or rev:. A known export-only file is exported once; folders "
        "return "
        "folder metadata. No shared URL or external target is accepted.",
        Object({{"path", TextSchema(2048, false)}}), Array({String("path")})));
  return tools;
}
}  // namespace
ObjectReply DropboxProvider(std::string_view method, const Json& params,
                            const ProviderFetch& fetch, Json* result) {
  if (method == "tools/list") {
    if (!params.IsObject() || !params.object.empty()) return Invalid();
    *result = Object({{"tools", Catalog()}});
    return {};
  }
  const auto* args = params.Find("arguments");
  if (method != "tools/call" || !args || !args->IsObject()) return Invalid();
  const auto name = params.GetString("name");
  Json value, content = Array();
  ObjectReply reply;
  if (name == "fetch" || name == "fetch_file")
    reply = Fetch(*args, fetch, &value, &content);
  else if (name == "search" || name == "search_files" ||
           name == "list_recent_files")
    reply = Search(name == "list_recent_files", *args, fetch, &value);
  else if (name == "get_profile") {
    std::string field;
    if (!InputFields(*args, {}, &field)) return Invalid();
    reply = fetch(Rpc("users/get_current_account", Json()), &value);
    if (reply.ok() && value.GetString("account_id").empty()) return Protocol();
  } else
    return Invalid();
  return reply.ok() ? Result(std::move(value), std::move(content), result)
                    : reply;
}
}  // namespace q4t::server::mcp_detail
