#pragma once

#include <set>

#include "mcp_provider_internal.h"

namespace q4t::server::mcp_detail::provider_detail {
inline constexpr uint64_t kBytes = 4U << 20;
ObjectReply Invalid();
ObjectReply Protocol();
ObjectReply Capacity();
io::Json TextSchema(size_t maximum, bool nullable = true);
io::Json LimitSchema(int maximum);
io::Json Tool(std::string name, std::string description, io::Json properties,
              io::Json required);
bool OptionalText(const io::Json& args, const char* name, size_t maximum);
std::string Text(const io::Json& args, const char* name,
                 std::string_view fallback = {});
bool Limit(const io::Json& args, int fallback, int maximum, int* output);
bool Identifier(std::string_view value);
void Query(std::string* url, std::string_view key, std::string_view value);
ObjectReply GoogleScopes(const ProviderFetch& fetch,
                         std::set<std::string>* scopes);
bool Scope(const std::set<std::string>& scopes, std::string_view name);
ObjectReply Page(const io::Json& response, const char* field, size_t maximum,
                 io::Json* result);
ObjectReply Result(io::Json value, io::Json content, io::Json* result);
std::string Base64(std::string_view bytes);
ObjectReply DecodeBase64Url(std::string_view value, std::string* bytes);
io::Json Resource(std::string uri, std::string mime, std::string base64);
}  // namespace q4t::server::mcp_detail::provider_detail
