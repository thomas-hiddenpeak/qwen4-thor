#include "mcp_provider_graph.h"

#include <algorithm>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"

namespace q4t::server::mcp_detail::graph_detail {
using namespace object_detail;
using namespace provider_detail;
bool Id(std::string_view value) {
  return !value.empty() && value.size() <= 2048 && value != "." &&
         value != ".." &&
         std::none_of(value.begin(), value.end(),
                      [](unsigned char c) { return c < 32 || c == 127; });
}
Json SearchSchema() {
  return Object(
      {{"query", TextSchema(4096, false)},
       {"max_results", LimitSchema(500)},
       {"offset", Object({{"type", Array({String("integer"), String("null")})},
                          {"minimum", Number(0)},
                          {"maximum", Number(1000000)}})}});
}
ObjectReply Search(std::string_view entity, const Json& args,
                   const ProviderFetch& fetch, Json* result, bool recent) {
  std::string field;
  int maximum = 0;
  if (!InputFields(args, {"query", "max_results", "offset"}, &field) ||
      !OptionalText(args, "query", 4096) || Text(args, "query").empty() ||
      !Limit(args, 25, 500, &maximum))
    return Invalid();
  int64_t offset = 0;
  if (const auto* value = args.Find("offset"); value && !value->IsNull()) {
    if (!IsInputInteger(*value) || value->number < 0 || value->number > 1000000)
      return Invalid();
    offset = value->AsInt();
  }
  ProviderRequest request{std::string(kGraph) + "/search/query"};
  request.method = "POST";
  request.headers["Prefer"] = "include-unknown-enum-members";
  auto query =
      Object({{"entityTypes", Array({String(std::string(entity))})},
              {"query", Object({{"queryString", String(Text(args, "query"))}})},
              {"from", Number(offset)},
              {"size", Number(maximum)}});
  if (recent)
    Set(&query, "sortProperties",
        Array({Object({{"name", String("lastModifiedDateTime")},
                       {"isDescending", Boolean(true)}})}));
  request.body = OutputJson(Object({{"requests", Array({std::move(query)})}}));
  Json response;
  auto reply = fetch(request, &response);
  if (!reply.ok()) return reply;
  const auto* values = response.GetArray("value");
  if (!values || values->array.size() != 1) return Protocol();
  const auto* containers = values->array[0].GetArray("hitsContainers");
  if (!containers || containers->array.size() != 1) return Protocol();
  const auto& container = containers->array[0];
  const auto* hits = container.GetArray("hits");
  const auto* more = container.Find("moreResultsAvailable");
  if (!hits || hits->array.size() > static_cast<size_t>(maximum) || !more ||
      !more->IsBool())
    return Protocol();
  for (const auto& hit : hits->array) {
    const auto* resource = hit.Find("resource");
    if (!hit.IsObject() || hit.GetString("hitId").empty() || !resource ||
        !resource->IsObject())
      return Protocol();
  }
  *result = response;
  Set(result, "hits", *hits);
  // Graph's total has resource-specific meaning; preserve it, never relabel
  // an events page count as the number of all matching events.
  Set(result, "next_offset", more->boolean ? Number(offset + maximum) : Json());
  return {};
}
namespace {
ObjectReply ReadCursor(std::string_view cursor, std::string_view path,
                       std::string* url,
                       std::map<std::string, std::string>* query,
                       bool authenticated_alias) {
  file_detail::Url parsed;
  if (!url || cursor.empty() || cursor.size() > 8192 ||
      cursor.find('#') != cursor.npos ||
      !file_detail::ParseUrl(cursor, &parsed) || parsed.scheme != "https" ||
      parsed.host != "graph.microsoft.com" || parsed.port != 443 ||
      !parsed.target.starts_with("/v1.0/") ||
      !CursorPath(std::string_view(parsed.target)
                      .substr(5, parsed.target.find('?') == parsed.target.npos
                                     ? parsed.target.size() - 5
                                     : parsed.target.find('?') - 5),
                  path, authenticated_alias))
    return Invalid();
  const auto decode = [](std::string_view raw, std::string* value) {
    value->clear();
    const auto hex = [](char c) {
      return c >= '0' && c <= '9'   ? c - '0'
             : c >= 'a' && c <= 'f' ? c - 'a' + 10
             : c >= 'A' && c <= 'F' ? c - 'A' + 10
                                    : -1;
    };
    for (size_t i = 0; i < raw.size(); ++i) {
      unsigned char c = raw[i];
      if (c == '%') {
        if (i + 2 >= raw.size() || hex(raw[i + 1]) < 0 || hex(raw[i + 2]) < 0)
          return false;
        c = hex(raw[i + 1]) * 16 + hex(raw[i + 2]);
        i += 2;
      } else if (c == '+')
        c = ' ';
      if (c < 32 || c == 127) return false;
      *value += c;
    }
    return true;
  };
  std::map<std::string, std::string> params;
  const auto mark = parsed.target.find('?');
  if (mark == parsed.target.npos || mark + 1 == parsed.target.size())
    return Invalid();
  std::string_view rest(parsed.target);
  rest.remove_prefix(mark + 1);
  while (!rest.empty()) {
    const auto end = rest.find('&');
    const auto item = rest.substr(0, end);
    const auto equal = item.find('=');
    std::string key, value;
    if (equal == item.npos || !decode(item.substr(0, equal), &key) ||
        key.empty() || !decode(item.substr(equal + 1), &value) ||
        !params.emplace(key, value).second || params.size() > 32)
      return Invalid();
    if (end == rest.npos) break;
    rest.remove_prefix(end + 1);
    if (rest.empty()) return Invalid();
  }
  if (query) *query = std::move(params);
  *url = cursor;
  return {};
}
}  // namespace
ObjectReply Cursor(std::string_view cursor, std::string_view path,
                   std::string* url, std::map<std::string, std::string>* query,
                   CursorContext* context, bool* authenticated) {
  if (authenticated) *authenticated = false;
  if (!cursor.starts_with("q4t-graph-1."))
    return ReadCursor(cursor, path, url, query, false);
  std::string raw;
  auto reply = OpenCursor(cursor, path, context, &raw);
  if (!reply.ok()) return reply;
  reply = ReadCursor(raw, path, url, query, true);
  if (reply.ok() && authenticated) *authenticated = true;
  return reply;
}
ObjectReply Page(const Json& raw, std::string_view path, const char* field,
                 size_t maximum, Json* result, CursorContext* context) {
  const auto* values = raw.GetArray("value");
  if (!raw.IsObject() || !values || values->array.size() > maximum)
    return Protocol();
  for (const auto& item : values->array)
    if (!item.IsObject() || item.GetString("id").empty()) return Protocol();
  const auto* cursor = raw.Find("@odata.nextLink");
  Json token;
  if (cursor) {
    std::string ignored;
    const bool protect = context && !context->authority.empty();
    if (!cursor->IsString() ||
        !ReadCursor(cursor->str, path, &ignored, nullptr, protect).ok())
      return Protocol();
    token = *cursor;
    if (protect) {
      auto reply = SealCursor(cursor->str, path, *context, &ignored);
      if (!reply.ok()) return reply;
      token = String(std::move(ignored));
    }
  }
  *result = raw;
  Set(result, field, *values);
  Set(result, "next_page_token", std::move(token));
  return {};
}
ObjectReply Profile(const Json& args, const ProviderFetch& fetch,
                    Json* result) {
  std::string field;
  if (!InputFields(args, {}, &field)) return Invalid();
  auto reply = fetch({std::string(kGraph) +
                      "/me?$select=id,displayName,mail,userPrincipalName"},
                     result);
  if (!reply.ok()) return reply;
  return result->GetString("id").empty() ? Protocol() : ObjectReply{};
}
void RemoveDownloadCapabilities(Json* value) {
  if (value->IsObject()) {
    std::erase_if(value->object, [](const auto& pair) {
      return pair.first == "@microsoft.graph.downloadUrl";
    });
    for (auto& [_, child] : value->object) RemoveDownloadCapabilities(&child);
  } else if (value->IsArray()) {
    for (auto& child : value->array) RemoveDownloadCapabilities(&child);
  }
}
}  // namespace q4t::server::mcp_detail::graph_detail

namespace q4t::server::mcp_detail {
bool GraphProviderId(std::string_view id) {
  return id == "connector_outlookcalendar" || id == "connector_outlookemail" ||
         id == "connector_microsoftteams" || id == "connector_sharepoint";
}
ObjectReply GraphProvider(std::string_view id, std::string_view method,
                          const io::Json& params, const ProviderFetch& fetch,
                          io::Json* result, std::string_view cursor_authority) {
  using namespace object_detail;
  using namespace provider_detail;
  if (!GraphProviderId(id)) return Invalid();
  const bool calendar = id == "connector_outlookcalendar";
  const bool teams = id == "connector_microsoftteams";
  const bool sharepoint = id == "connector_sharepoint";
  if (method == "tools/list") {
    if (!params.IsObject() || !params.object.empty()) return Invalid();
    // Microsoft advises clients to treat access tokens as opaque. Native
    // tool schemas are capabilities, not guessed OAuth grants. Each read is
    // authorized by Graph itself; User.Read is only needed for get_profile.
    *result = Object(
        {{"tools", teams        ? graph_detail::TeamsCatalog()
                   : sharepoint ? graph_detail::SharePointCatalog()
                                : graph_detail::OutlookCatalog(calendar)}});
    return {};
  }
  const auto* args = params.Find("arguments");
  if (method != "tools/call" || !args || !args->IsObject()) return Invalid();
  Json value, content = Array();
  const auto name = params.GetString("name");
  graph_detail::CursorContext context;
  context.authority = cursor_authority;
  if (!cursor_authority.empty()) {
    auto bound = *args;
    std::erase_if(bound.object, [](const auto& field) {
      return field.first == "next_page_token" || field.first == "max_results" ||
             field.second.IsNull();
    });
    if (!InputValueIdentity(Object({{"provider", String(std::string(id))},
                                    {"tool", String(name)},
                                    {"arguments", std::move(bound)}}),
                            &context.binding))
      return Protocol();
    const auto* limit = args->Find("max_results");
    context.explicit_limit = limit && !limit->IsNull();
    context.limit = teams ? 0 : args->GetInt("max_results", 25);
  }
  auto reply =
      teams ? graph_detail::TeamsRead(name, *args, fetch, &value, &context)
      : sharepoint
          ? graph_detail::SharePointRead(name, *args, fetch, &value, &content)
          : graph_detail::OutlookRead(calendar, name, *args, fetch, &value,
                                      &context);
  if (!reply.ok()) return reply;
  graph_detail::RemoveDownloadCapabilities(&value);
  return Result(std::move(value), std::move(content), result);
}
}  // namespace q4t::server::mcp_detail
