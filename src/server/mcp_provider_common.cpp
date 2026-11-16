#include "mcp_provider_common.h"

#include <algorithm>
#include <sstream>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/mcp_content.h"

namespace q4t::server::mcp_detail::provider_detail {
using namespace object_detail;
ObjectReply Invalid() {
  return Error(400, "mcp_provider_arguments_invalid",
               "Invalid native provider tool arguments");
}
ObjectReply Protocol() {
  return Error(502, "mcp_provider_response_invalid",
               "Invalid native provider response");
}
ObjectReply Capacity() {
  return Error(413, "mcp_provider_capacity",
               "Provider response budget exceeded");
}
Json TextSchema(size_t maximum, bool nullable) {
  return Object({{"type", nullable ? Array({String("string"), String("null")})
                                   : String("string")},
                 {"minLength", Number(1)},
                 {"maxLength", Number(maximum)}});
}
Json LimitSchema(int maximum) {
  return Object({{"type", Array({String("integer"), String("null")})},
                 {"minimum", Number(1)},
                 {"maximum", Number(maximum)}});
}
Json Tool(std::string name, std::string description, Json properties,
          Json required) {
  return Object(
      {{"name", String(std::move(name))},
       {"description", String(std::move(description))},
       {"inputSchema", Object({{"type", String("object")},
                               {"properties", std::move(properties)},
                               {"required", std::move(required)},
                               {"additionalProperties", Boolean(false)}})},
       {"annotations", Object({{"readOnlyHint", Boolean(true)},
                               {"destructiveHint", Boolean(false)}})}});
}
bool OptionalText(const Json& args, const char* name, size_t maximum) {
  const auto* value = args.Find(name);
  return !value || value->IsNull() ||
         (value->IsString() && !value->str.empty() &&
          value->str.size() <= maximum &&
          std::none_of(value->str.begin(), value->str.end(),
                       [](unsigned char c) { return c < 32 || c == 127; }));
}
std::string Text(const Json& args, const char* name,
                 std::string_view fallback) {
  const auto* value = args.Find(name);
  return value && value->IsString() ? value->str : std::string(fallback);
}
bool Limit(const Json& args, int fallback, int maximum, int* output) {
  *output = fallback;
  const auto* value = args.Find("max_results");
  if (!value || value->IsNull()) return true;
  if (!IsInputInteger(*value) || value->number < 1 || value->number > maximum)
    return false;
  *output = value->AsInt();
  return true;
}
bool Identifier(std::string_view value) {
  return !value.empty() && value.size() <= 1024 && value != "." &&
         value != ".." &&
         std::all_of(value.begin(), value.end(), [](unsigned char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '-' || c == '_';
         });
}
void Query(std::string* url, std::string_view key, std::string_view value) {
  *url += url->find('?') == url->npos ? '?' : '&';
  *url += std::string(key) + '=' + ProviderEscape(value);
}
ObjectReply GoogleScopes(const ProviderFetch& fetch,
                         std::set<std::string>* scopes) {
  Json token;
  auto reply = fetch({"https://oauth2.googleapis.com/tokeninfo", true}, &token);
  if (!reply.ok()) return reply;
  const auto* scope = token.Find("scope");
  if (!scope || !scope->IsString() || scope->str.size() > 16384)
    return Protocol();
  scopes->clear();
  std::istringstream stream(scope->str);
  for (std::string item; stream >> item;) scopes->insert(std::move(item));
  return {};
}
bool Scope(const std::set<std::string>& scopes, std::string_view name) {
  return scopes.contains("https://www.googleapis.com/auth/" +
                         std::string(name));
}
ObjectReply Page(const Json& response, const char* field, size_t maximum,
                 Json* result) {
  if (!response.IsObject()) return Protocol();
  const auto* items = response.Find(field);
  if (items) {
    if (!items->IsArray() || items->array.size() > maximum) return Protocol();
    for (const auto& item : items->array)
      if (!item.IsObject() || item.GetString("id").empty()) return Protocol();
  }
  const auto* cursor = response.Find("nextPageToken");
  if (cursor &&
      (!cursor->IsString() || cursor->str.empty() || cursor->str.size() > 8192))
    return Protocol();
  *result = response;
  Set(result, field, items ? *items : Array());
  Set(result, "next_page_token", cursor ? *cursor : Json());
  return {};
}
ObjectReply Result(Json value, Json content, Json* result) {
  if (!content.IsArray() || !value.IsObject()) return Protocol();
  // Metadata is preserved separately; raw bytes are only in native content.
  content.array.insert(
      content.array.begin(),
      Object({{"type", String("text")}, {"text", String(OutputJson(value))}}));
  auto next = Object({{"content", std::move(content)},
                      {"structuredContent", std::move(value)},
                      {"isError", Boolean(false)}});
  if (OutputJson(next).size() > kBytes) return Capacity();
  if (!ValidateMcpToolResult(next)) return Protocol();
  *result = std::move(next);
  return {};
}
std::string Base64(std::string_view bytes) {
  if (bytes.size() > kBytes) return {};
  // Same canonical encoding as the existing file-input/media path. Keep this
  // byte conversion independent of the optional dynamically loaded TLS code.
  constexpr char alphabet[] =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string encoded;
  encoded.reserve((bytes.size() + 2) / 3 * 4);
  for (size_t i = 0; i < bytes.size(); i += 3) {
    const auto a = static_cast<unsigned char>(bytes[i]);
    const auto b =
        i + 1 < bytes.size() ? static_cast<unsigned char>(bytes[i + 1]) : 0;
    const auto c =
        i + 2 < bytes.size() ? static_cast<unsigned char>(bytes[i + 2]) : 0;
    encoded += alphabet[a >> 2];
    encoded += alphabet[((a & 3) << 4) | (b >> 4)];
    encoded +=
        i + 1 < bytes.size() ? alphabet[((b & 15) << 2) | (c >> 6)] : '=';
    encoded += i + 2 < bytes.size() ? alphabet[c & 63] : '=';
  }
  return encoded;
}
ObjectReply DecodeBase64Url(std::string_view value, std::string* bytes) {
  if (!bytes) return Protocol();
  bytes->clear();
  if (value.size() > kBytes) return Capacity();
  if (value.empty()) return {};
  std::string encoded(value);
  const auto padding = encoded.find('=');
  const auto digits = padding == encoded.npos ? encoded.size() : padding;
  if (digits % 4 == 1 || (padding != encoded.npos &&
                          (encoded.size() % 4 || encoded.size() - padding > 2)))
    return Protocol();
  for (size_t i = 0; i < encoded.size(); ++i) {
    auto& c = encoded[i];
    if (i >= digits) {
      if (c != '=') return Protocol();
    } else if (c == '-')
      c = '+';
    else if (c == '_')
      c = '/';
    else if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
               (c >= '0' && c <= '9')))
      return Protocol();
  }
  while (encoded.size() % 4) encoded += '=';
  const auto digit = [](unsigned char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    return c == '+' ? 62 : c == '/' ? 63 : 0;
  };
  std::string decoded;
  decoded.reserve(encoded.size() / 4 * 3);
  for (size_t i = 0; i < encoded.size(); i += 4) {
    const int a = digit(encoded[i]), b = digit(encoded[i + 1]);
    const int c = digit(encoded[i + 2]), d = digit(encoded[i + 3]);
    decoded += static_cast<char>((a << 2) | (b >> 4));
    if (i + 2 < digits) decoded += static_cast<char>((b << 4) | (c >> 2));
    if (i + 3 < digits) decoded += static_cast<char>((c << 6) | d);
  }
  if (Base64(decoded) != encoded) return Protocol();
  *bytes = std::move(decoded);
  return {};
}
Json Resource(std::string uri, std::string mime, std::string base64) {
  return Object({{"type", String("resource")},
                 {"resource", Object({{"uri", String(std::move(uri))},
                                      {"mimeType", String(std::move(mime))},
                                      {"blob", String(std::move(base64))}})}});
}
}  // namespace q4t::server::mcp_detail::provider_detail
