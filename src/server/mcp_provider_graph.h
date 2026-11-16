#pragma once

#include "mcp_provider_common.h"

namespace q4t::server::mcp_detail::graph_detail {
inline constexpr std::string_view kGraph = "https://graph.microsoft.com/v1.0";
struct CursorContext {
  std::string_view authority;
  std::string binding;
  int limit = 0;
  bool explicit_limit = false;
};
ObjectReply SealCursor(std::string_view url, std::string_view path,
                       const CursorContext& context, std::string* token);
ObjectReply OpenCursor(std::string_view token, std::string_view path,
                       CursorContext* context, std::string* url);
bool CursorPath(std::string_view actual, std::string_view expected,
                bool authenticated_alias);
bool Id(std::string_view value);
io::Json SearchSchema();
ObjectReply Search(std::string_view entity, const io::Json& args,
                   const ProviderFetch& fetch, io::Json* result,
                   bool recent = false);
ObjectReply Cursor(std::string_view cursor, std::string_view path,
                   std::string* url,
                   std::map<std::string, std::string>* query = nullptr,
                   CursorContext* context = nullptr,
                   bool* authenticated = nullptr);
ObjectReply Page(const io::Json& raw, std::string_view path, const char* field,
                 size_t maximum, io::Json* result,
                 CursorContext* context = nullptr);
ObjectReply Profile(const io::Json& args, const ProviderFetch& fetch,
                    io::Json* result);
io::Json OutlookCatalog(bool calendar);
ObjectReply OutlookRead(bool calendar, std::string_view name,
                        const io::Json& args, const ProviderFetch& fetch,
                        io::Json* result, CursorContext* context = nullptr);
io::Json TeamsCatalog();
ObjectReply TeamsRead(std::string_view name, const io::Json& args,
                      const ProviderFetch& fetch, io::Json* result,
                      CursorContext* context = nullptr);
io::Json SharePointCatalog();
ObjectReply SharePointRead(std::string_view name, const io::Json& args,
                           const ProviderFetch& fetch, io::Json* result,
                           io::Json* content);
void RemoveDownloadCapabilities(io::Json* value);
}  // namespace q4t::server::mcp_detail::graph_detail
