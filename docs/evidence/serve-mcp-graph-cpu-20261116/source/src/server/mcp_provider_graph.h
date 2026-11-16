#pragma once

#include "mcp_provider_common.h"

namespace q4t::server::mcp_detail::graph_detail {
inline constexpr std::string_view kGraph = "https://graph.microsoft.com/v1.0";
bool Id(std::string_view value);
io::Json SearchSchema();
ObjectReply Search(std::string_view entity, const io::Json& args,
                   const ProviderFetch& fetch, io::Json* result,
                   bool recent = false);
ObjectReply Cursor(std::string_view cursor, std::string_view path,
                   std::string* url,
                   std::map<std::string, std::string>* query = nullptr);
ObjectReply Page(const io::Json& raw, std::string_view path, const char* field,
                 size_t maximum, io::Json* result);
ObjectReply Profile(const io::Json& args, const ProviderFetch& fetch,
                    io::Json* result);
io::Json OutlookCatalog(bool calendar);
ObjectReply OutlookRead(bool calendar, std::string_view name,
                        const io::Json& args, const ProviderFetch& fetch,
                        io::Json* result);
io::Json TeamsCatalog();
ObjectReply TeamsRead(std::string_view name, const io::Json& args,
                      const ProviderFetch& fetch, io::Json* result);
io::Json SharePointCatalog();
ObjectReply SharePointRead(std::string_view name, const io::Json& args,
                           const ProviderFetch& fetch, io::Json* result,
                           io::Json* content);
void RemoveDownloadCapabilities(io::Json* value);
}  // namespace q4t::server::mcp_detail::graph_detail
