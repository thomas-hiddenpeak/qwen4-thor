#include "mcp_provider_graph.h"

#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::mcp_detail::graph_detail {
namespace {
using namespace object_detail;
using namespace provider_detail;
ObjectReply File(const Json& args, const ProviderFetch& fetch, Json* result,
                 Json* content) {
  std::string field;
  if (!InputFields(args, {"item_id", "drive_id", "site_id"}, &field) ||
      !Id(Text(args, "item_id")))
    return Invalid();
  for (const char* key : {"drive_id", "site_id"})
    if (!OptionalText(args, key, 2048) ||
        (!Text(args, key).empty() && !Id(Text(args, key))))
      return Invalid();
  auto item = Text(args, "item_id"), drive = Text(args, "drive_id");
  const auto site = Text(args, "site_id");
  if (!drive.empty() && !site.empty()) return Invalid();
  std::string base = !drive.empty() ? "/drives/" + ProviderEscape(drive)
                     : !site.empty()
                         ? "/sites/" + ProviderEscape(site) + "/drive"
                         : "/me/drive";
  std::set<std::string> seen;
  auto links = Array();
  for (unsigned hop = 0; hop < 4; ++hop) {
    const auto path = base + "/items/" + ProviderEscape(item);
    if (!seen.insert(path).second) return Protocol();
    std::string url = std::string(kGraph) + path;
    Query(&url, "$select",
          "id,name,size,file,folder,remoteItem,parentReference,"
          "webUrl,eTag,lastModifiedDateTime,"
          "@microsoft.graph.downloadUrl");
    Json metadata;
    auto reply = fetch({url}, &metadata);
    if (!reply.ok()) return reply;
    // A SharePoint list-item unique ID may resolve to a different driveItem
    // ID. Keep the authoritative identity instead of treating those aliases
    // as a mismatched object or inventing a conversion.
    if (!metadata.IsObject() || !Id(metadata.GetString("id")) ||
        metadata.GetString("name").empty())
      return Protocol();
    const auto* remote = metadata.Find("remoteItem");
    if (remote && !remote->IsNull()) {
      const auto* parent = remote->Find("parentReference");
      if (!remote->IsObject() || !parent || !parent->IsObject() ||
          !Id(remote->GetString("id")) || !Id(parent->GetString("driveId")))
        return Protocol();
      item = remote->GetString("id");
      base = "/drives/" + ProviderEscape(parent->GetString("driveId"));
      RemoveDownloadCapabilities(&metadata);
      links.array.push_back(std::move(metadata));
      continue;
    }
    const auto download = metadata.GetString("@microsoft.graph.downloadUrl");
    RemoveDownloadCapabilities(&metadata);
    *result = std::move(metadata);
    if (!links.array.empty()) Set(result, "resolved_links", std::move(links));
    const auto* folder = result->Find("folder");
    if (folder && folder->IsObject()) {
      Set(result, "kind", String("folder"));
      return {};
    }
    const auto* file = result->Find("file");
    if (!file || !file->IsObject()) return Protocol();
    const auto* size = result->Find("size");
    if (size && (!IsInputInteger(*size) || size->number < 0)) return Protocol();
    if (size && size->number > kBytes) return Capacity();
    file_detail::Url parsed;
    if (!file_detail::ParseUrl(download, &parsed) || parsed.scheme != "https" ||
        parsed.port != 443 || download.find('#') != download.npos)
      return Protocol();
    ProviderRequest request{download};
    request.binary = true;
    request.graph_download = true;
    Json bytes;
    reply = fetch(request, &bytes);
    if (!reply.ok()) return reply;
    const auto* data = bytes.Find("data");
    const auto* length = bytes.Find("size");
    if (!data || !data->IsString() || !length || !IsInputInteger(*length) ||
        (size && size->number != length->number))
      return Protocol();
    auto mime = file->GetString("mimeType");
    if (mime.empty())
      mime = bytes.GetString("mimeType", "application/octet-stream");
    content->array.push_back(
        Resource("q4t-provider://sharepoint" + path, mime, data->str));
    Set(result, "content_mime_type", String(mime));
    Set(result, "kind", String("file"));
    return {};
  }
  return Capacity();
}
}  // namespace
Json SharePointCatalog() {
  return Array(
      {Tool("get_profile",
            "Read the Graph profile; independently needs User.Read", Object(),
            Array()),
       Tool("get_site", "Read a SharePoint site by its opaque site_id or root",
            Object({{"site_id", TextSchema(2048, false)}}),
            Array({String("site_id")})),
       Tool("search",
            "Search accessible SharePoint and OneDrive documents with "
            "Graph KQL; preserve original hit IDs, resources and offset",
            SearchSchema(), Array({String("query")})),
       Tool("list_recent_documents",
            "List indexed documents by most recent "
            "modification time, not personal view history; "
            "optional Graph KQL query narrows the search",
            SearchSchema(), Array()),
       Tool("fetch",
            "Read an original file or folder using item_id and either "
            "drive_id or site_id (otherwise the current user's drive). "
            "Use resource.id or SharePoint listItemUniqueId from search.",
            Object({{"item_id", TextSchema(2048, false)},
                    {"drive_id", TextSchema(2048)},
                    {"site_id", TextSchema(2048)}}),
            Array({String("item_id")}))});
}
ObjectReply SharePointRead(std::string_view name, const Json& args,
                           const ProviderFetch& fetch, Json* result,
                           Json* content) {
  if (name == "get_profile") return Profile(args, fetch, result);
  if (name == "fetch") return File(args, fetch, result, content);
  if (name == "search") return Search("driveItem", args, fetch, result);
  if (name == "list_recent_documents") {
    if (!OptionalText(args, "query", 4096)) return Invalid();
    auto query = args;
    Set(&query, "query",
        String(Text(args, "query").empty()
                   ? "IsDocument:1"
                   : "(" + Text(args, "query") + ") AND IsDocument:1"));
    auto reply = Search("driveItem", query, fetch, result, true);
    if (reply.ok()) Set(result, "recency", String("lastModifiedDateTime"));
    return reply;
  }
  std::string field;
  if (name != "get_site" || !InputFields(args, {"site_id"}, &field) ||
      !Id(Text(args, "site_id")))
    return Invalid();
  auto reply = fetch(
      {std::string(kGraph) + "/sites/" + ProviderEscape(Text(args, "site_id"))},
      result);
  if (!reply.ok()) return reply;
  return result->GetString("id").empty() ? Protocol() : ObjectReply{};
}
}  // namespace q4t::server::mcp_detail::graph_detail
