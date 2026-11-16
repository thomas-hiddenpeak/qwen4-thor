#include "mcp_provider_internal.h"

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "remote_http_internal.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
constexpr uint64_t kProviderResponseBytes = 4U << 20;
}  // namespace

bool NativeProvider(std::string_view id) {
  return id == "connector_googlecalendar";
}
bool ProviderOrigin(std::string_view id, std::string_view raw) {
  file_detail::Url url;
  if (!NativeProvider(id) || !file_detail::ParseUrl(raw, &url) ||
      url.scheme != "https" || url.port != 443)
    return false;
  return url.host == "www.googleapis.com" ||
         url.host == "oauth2.googleapis.com" ||
         url.host == "openidconnect.googleapis.com";
}
std::string ProviderEscape(std::string_view value) {
  constexpr char hex[] = "0123456789ABCDEF";
  std::string result;
  for (unsigned char c : value) {
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
        (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
        c == '~') {
      result += c;
    } else {
      result += '%';
      result += hex[c >> 4];
      result += hex[c & 15];
    }
  }
  return result;
}
ObjectReply InvokeProvider(std::string_view id, std::string_view method,
                           const Json& params, const ProviderFetch& fetch,
                           Json* result, Json* error) {
  if (!result || !error || !fetch || !NativeProvider(id))
    return Error(500, "mcp_provider_invalid", "Invalid native provider");
  *result = *error = Json();
  auto reply = GoogleCalendarProvider(method, params, fetch, result);
  if (reply.ok()) return reply;
  const int status = reply.value.GetInt("upstream_status");
  if (status >= 300 && status <= 599) {
    // A complete provider failure is a known result. Never echo a provider's
    // diagnostic body, which can contain credentials or request URLs.
    *error = Object({{"code", Number(-32000)},
                     {"message", String("Provider request failed")},
                     {"data", Object({{"upstream_status", Number(status)}})}});
    return reply.http_status == 408 || reply.http_status == 499 ? reply
                                                                : ObjectReply{};
  }
  if (reply.code == "mcp_provider_arguments_invalid" ||
      reply.code == "mcp_provider_time_invalid") {
    *error =
        Object({{"code", Number(-32602)}, {"message", String(reply.message)}});
    return {};
  }
  return reply;
}
ObjectReply ReadProviderResponse(file_detail::RemoteHttp& response,
                                 uint64_t* total_bytes, Json* output) {
  if (!total_bytes || !output || *total_bytes > kProviderResponseBytes)
    return Error(500, "mcp_provider_invalid",
                 "Invalid provider response state");
  if (response.Status() != 200) {
    // Error-body stalls cannot hide an authenticated 401/403/429 rejection.
    auto reply =
        Error(502, "mcp_provider_http_error", "Provider request failed");
    reply.value = Object({{"upstream_status", Number(response.Status())}});
    return reply;
  }
  std::string body;
  auto reply = response.ReadAll(&body);
  if (!reply.ok()) return reply;
  if (body.size() > kProviderResponseBytes - *total_bytes)
    return Error(413, "mcp_provider_capacity",
                 "Provider response budget exceeded");
  *total_bytes += body.size();
  if (response.Mime() != "application/json" ||
      !io::ParseJson(body, output, true,
                     {.max_depth = 32,
                      .max_values = 65536,
                      .max_string_bytes = kProviderResponseBytes,
                      .reject_duplicate_keys = true}) ||
      !output->IsObject())
    return Error(502, "mcp_provider_response_invalid",
                 "Invalid provider response");
  return {};
}
ObjectReply ExchangeProvider(const McpEndpoint& endpoint,
                             const FileFetchOptions& deployment,
                             std::string_view method, const Json& params,
                             std::chrono::steady_clock::time_point deadline,
                             Json* result, Json* error) {
  uint64_t bytes = 0;
  unsigned requests = 0;
  const auto fetch = [&](const ProviderRequest& request, Json* output) {
    if (++requests > 32 || bytes >= kProviderResponseBytes)
      return Error(413, "mcp_provider_capacity",
                   "Provider response budget exceeded");
    if (!ProviderOrigin(endpoint.provider_id, request.url))
      return Error(403, "mcp_provider_target_denied",
                   "Native provider target not permitted");
    file_detail::Url url;
    if (!file_detail::ParseUrl(request.url, &url))
      return Error(500, "mcp_provider_invalid", "Invalid provider target");
    auto headers = endpoint.headers;
    headers["Accept"] = "application/json";
    if (request.token_query) {
      if (request.url != "https://oauth2.googleapis.com/tokeninfo")
        return Error(403, "mcp_provider_target_denied",
                     "Native provider target not permitted");
      // Token-info's documented API uses a query parameter. It stays inside
      // the private worker; neither this URL nor raw errors leave the worker.
      url.target += "?access_token=" + ProviderEscape(endpoint.authorization);
      headers.clear();
      headers["Accept"] = "application/json";
    } else {
      headers["Authorization"] = "Bearer " + endpoint.authorization;
    }
    std::unique_ptr<file_detail::RemoteHttp> response;
    auto reply = file_detail::RemoteHttp::Open(
        url, deployment, "GET", headers, "", kProviderResponseBytes - bytes,
        deadline, &response);
    if (!reply.ok()) return reply;
    return ReadProviderResponse(*response, &bytes, output);
  };
  return InvokeProvider(endpoint.provider_id, method, params, fetch, result,
                        error);
}
}  // namespace q4t::server::mcp_detail
