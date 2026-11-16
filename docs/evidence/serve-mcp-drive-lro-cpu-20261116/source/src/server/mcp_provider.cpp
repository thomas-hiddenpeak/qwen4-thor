#include "mcp_provider_internal.h"
#include "mcp_provider_common.h"

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "remote_http_internal.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
constexpr uint64_t kProviderResponseBytes = 4U << 20;
}  // namespace

bool NativeProvider(std::string_view id) {
  return id == "connector_googlecalendar" || id == "connector_googledrive" ||
         id == "connector_gmail" || id == "connector_dropbox" ||
         GraphProviderId(id);
}
bool ProviderOrigin(std::string_view id, std::string_view raw) {
  file_detail::Url url;
  if (!NativeProvider(id) || !file_detail::ParseUrl(raw, &url) ||
      url.scheme != "https" || url.port != 443)
    return false;
  if (GraphProviderId(id)) return url.host == "graph.microsoft.com";
  if (id == "connector_dropbox")
    return url.host == "api.dropboxapi.com" ||
           url.host == "content.dropboxapi.com";
  if (url.host == "oauth2.googleapis.com") return true;
  if (id == "connector_gmail") return url.host == "gmail.googleapis.com";
  return url.host == "www.googleapis.com" ||
         (id == "connector_googlecalendar" &&
          url.host == "openidconnect.googleapis.com");
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
bool GoogleDownloadOrigin(std::string_view raw) {
  file_detail::Url url;
  if (!file_detail::ParseUrl(raw, &url) || url.scheme != "https" ||
      url.port != 443 || raw.find('#') != raw.npos)
    return false;
  return url.host == "www.googleapis.com" ||
         url.host == "content.googleapis.com" ||
         url.host == "drive.usercontent.google.com" ||
         url.host == "drive.google.com" || url.host == "docs.google.com" ||
         url.host.ends_with(".googleusercontent.com") ||
         url.host.ends_with(".googlevideo.com");
}
ObjectReply InvokeProvider(std::string_view id, std::string_view method,
                           const Json& params, const ProviderFetch& fetch,
                           Json* result, Json* error) {
  if (!result || !error || !fetch || !NativeProvider(id))
    return Error(500, "mcp_provider_invalid", "Invalid native provider");
  *result = *error = Json();
  auto reply = id == "connector_googlecalendar"
                   ? GoogleCalendarProvider(method, params, fetch, result)
               : id == "connector_googledrive"
                   ? GoogleDriveProvider(method, params, fetch, result)
               : id == "connector_gmail"
                   ? GmailProvider(method, params, fetch, result)
               : id == "connector_dropbox"
                   ? DropboxProvider(method, params, fetch, result)
                   : GraphProvider(id, method, params, fetch, result);
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
  if (reply.code == "mcp_provider_access_denied") {
    *error =
        Object({{"code", Number(-32000)}, {"message", String(reply.message)}});
    return {};
  }
  return reply;
}
ObjectReply ReadProviderResponse(file_detail::RemoteHttp& response,
                                 uint64_t* total_bytes, Json* output,
                                 bool binary, bool dropbox_content) {
  if (!total_bytes || !output || *total_bytes > kProviderResponseBytes)
    return Error(500, "mcp_provider_invalid",
                 "Invalid provider response state");
  if (response.Status() != 200) {
    // Error-body stalls cannot hide an authenticated 401/403/429 rejection.
    auto reply =
        Error(502, "mcp_provider_http_error", "Provider request failed");
    reply.value = Object({{"upstream_status", Number(response.Status())}});
    // A Dropbox content read may explicitly require export (or identify a
    // folder). Inspect only this known 409's bounded reason, never expose its
    // private body. A stalled/malformed error remains the already-known 409.
    if (dropbox_content && response.Status() == 409) {
      std::string body;
      auto read = response.ReadAll(&body);
      if (body.size() > kProviderResponseBytes - *total_bytes) return reply;
      *total_bytes += body.size();
      Json value;
      if (read.ok() && body.size() <= 16384 &&
          io::ParseJson(body, &value, true,
                        {.max_depth = 16,
                         .max_values = 1024,
                         .max_string_bytes = 16384,
                         .reject_duplicate_keys = true})) {
        const auto* error = value.Find("error");
        const auto* path = error ? error->Find("path") : nullptr;
        if (error && error->GetString(".tag") == "unsupported_file")
          Set(&reply.value, "provider_reason", String("export_required"));
        else if (error && error->GetString(".tag") == "path" && path &&
                 path->GetString(".tag") == "not_file")
          Set(&reply.value, "provider_reason", String("folder"));
      }
    }
    return reply;
  }
  Json metadata;
  if (dropbox_content) {
    const auto raw = response.Header("dropbox-api-result");
    if (!binary || raw.empty() || raw.size() > 16384 ||
        raw.size() > kProviderResponseBytes - *total_bytes ||
        !io::ParseJson(std::string(raw), &metadata, true,
                       {.max_depth = 16,
                        .max_values = 4096,
                        .max_string_bytes = 16384,
                        .reject_duplicate_keys = true}) ||
        !metadata.IsObject())
      return provider_detail::Protocol();
    *total_bytes += raw.size();
  }
  std::string body;
  auto reply = response.ReadAll(&body);
  if (!reply.ok()) return reply;
  if (body.size() > kProviderResponseBytes - *total_bytes)
    return Error(413, "mcp_provider_capacity",
                 "Provider response budget exceeded");
  *total_bytes += body.size();
  if (binary) {
    *output = Object({{"mimeType", String(std::string(response.Mime()))},
                      {"data", String(provider_detail::Base64(body))},
                      {"size", Number(body.size())}});
    if (dropbox_content) Set(output, "metadata", std::move(metadata));
    return {};
  }
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
ObjectReply PrepareProviderRequest(const McpEndpoint& endpoint,
                                   const FileFetchOptions& deployment,
                                   const ProviderRequest& request,
                                   file_detail::Url* url,
                                   std::map<std::string, std::string>* headers,
                                   FileFetchOptions* options) {
  if (!url || !headers || !options)
    return Error(500, "mcp_provider_invalid", "Invalid provider request state");
  *options = deployment;
  headers->clear();
  if (request.google_download) {
    if (endpoint.provider_id != "connector_googledrive" ||
        request.method != "GET" || !request.binary || request.token_query ||
        request.graph_download || request.dropbox_content ||
        !request.body.empty() || !GoogleDownloadOrigin(request.url) ||
        request.headers.size() > 1 ||
        (!request.headers.empty() &&
         !request.headers.contains("X-Goog-Drive-Resource-Keys")))
      return Error(403, "mcp_provider_target_denied",
                   "Native provider download target not permitted");
    file_detail::ParseUrl(request.url, url);
    options->allowed_networks.clear();
    options->allow_http = false;
    options->max_redirects = 0;
    *headers = request.headers;
    (*headers)["Accept"] = "*/*";
    // Only fixed API hosts receive the account token. Google-owned content
    // URIs retain the per-file resource key, never arbitrary deployment
    // headers.
    if (url->host == "www.googleapis.com" ||
        url->host == "content.googleapis.com")
      (*headers)["Authorization"] = "Bearer " + endpoint.authorization;
    return {};
  }
  if (request.graph_download) {
    if (endpoint.provider_id != "connector_sharepoint" ||
        request.method != "GET" || !request.binary || request.token_query ||
        request.dropbox_content || !request.body.empty() ||
        !request.headers.empty() || !file_detail::ParseUrl(request.url, url) ||
        url->scheme != "https" || url->port != 443 ||
        request.url.find('#') != request.url.npos)
      return Error(403, "mcp_provider_target_denied",
                   "Native provider download target not permitted");
    // The authenticated API chooses the short-lived CDN URL. Keep the
    // deployment host/CA/public-network restrictions; never inherit private
    // network grants or credentials onto that URL, and never follow redirects.
    options->allowed_networks.clear();
    options->allow_http = false;
    options->max_redirects = 0;
    (*headers)["Accept"] = "*/*";
    return {};
  }
  if (!ProviderOrigin(endpoint.provider_id, request.url))
    return Error(403, "mcp_provider_target_denied",
                 "Native provider target not permitted");
  if (!file_detail::ParseUrl(request.url, url))
    return Error(500, "mcp_provider_invalid", "Invalid provider target");
  if (endpoint.provider_id == "connector_dropbox") {
    const bool content =
        request.dropbox_content && request.binary && request.method == "GET" &&
        request.body.empty() && request.headers.size() == 1 &&
        request.headers.contains("Dropbox-API-Arg") &&
        (request.url == "https://content.dropboxapi.com/2/files/download" ||
         request.url == "https://content.dropboxapi.com/2/files/export");
    const bool rpc =
        !request.dropbox_content && !request.binary &&
        request.method == "POST" && request.headers.empty() &&
        (request.url ==
             "https://api.dropboxapi.com/2/users/get_current_account" ||
         request.url == "https://api.dropboxapi.com/2/files/get_metadata" ||
         request.url == "https://api.dropboxapi.com/2/files/search_v2" ||
         request.url ==
             "https://api.dropboxapi.com/2/files/search/continue_v2");
    if ((!content && !rpc) || request.token_query)
      return Error(403, "mcp_provider_target_denied",
                   "Native provider operation not permitted");
    *headers = endpoint.headers;
    (*headers)["Accept"] = content ? "*/*" : "application/json";
    if (content)
      (*headers)["Dropbox-API-Arg"] = request.headers.at("Dropbox-API-Arg");
    else
      (*headers)["Content-Type"] = "application/json";
    (*headers)["Authorization"] = "Bearer " + endpoint.authorization;
    return {};
  }
  if (request.dropbox_content)
    return Error(403, "mcp_provider_target_denied",
                 "Native provider operation not permitted");
  *headers = endpoint.headers;
  (*headers)["Accept"] = request.binary ? "*/*" : "application/json";
  for (const auto& [key, value] : request.headers) {
    if (!((endpoint.provider_id == "connector_googledrive" &&
           key == "X-Goog-Drive-Resource-Keys") ||
          (GraphProviderId(endpoint.provider_id) && key == "Prefer")))
      return Error(403, "mcp_provider_target_denied",
                   "Native provider header not permitted");
    (*headers)[key] = value;
  }
  if (request.method == "POST") {
    const std::string_view prefix = "/drive/v3/files/";
    const std::string_view suffix = "/download?mimeType=video%2Fmp4";
    const bool drive =
        endpoint.provider_id == "connector_googledrive" &&
        url->host == "www.googleapis.com" && request.body.empty() &&
        url->target.size() > prefix.size() + suffix.size() &&
        url->target.starts_with(prefix) && url->target.ends_with(suffix) &&
        provider_detail::Identifier(
            std::string_view(url->target)
                .substr(prefix.size(),
                        url->target.size() - prefix.size() - suffix.size()));
    const bool graph =
        GraphProviderId(endpoint.provider_id) &&
        request.url == "https://graph.microsoft.com/v1.0/search/query";
    if ((!drive && !graph) || request.token_query || request.binary)
      return Error(403, "mcp_provider_target_denied",
                   "Native provider operation not permitted");
    if (graph) (*headers)["Content-Type"] = "application/json";
  } else if (request.method != "GET" || !request.body.empty()) {
    return Error(403, "mcp_provider_target_denied",
                 "Native provider operation not permitted");
  }
  if (request.token_query) {
    if (request.url != "https://oauth2.googleapis.com/tokeninfo")
      return Error(403, "mcp_provider_target_denied",
                   "Native provider target not permitted");
    // Token-info's documented API uses a query parameter. It stays inside
    // the private worker; neither this URL nor raw errors leave the worker.
    url->target += "?access_token=" + ProviderEscape(endpoint.authorization);
    headers->clear();
    (*headers)["Accept"] = "application/json";
  } else {
    (*headers)["Authorization"] = "Bearer " + endpoint.authorization;
  }
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
    file_detail::Url url;
    file_detail::RemoteHeaders headers;
    FileFetchOptions options;
    auto reply = PrepareProviderRequest(endpoint, deployment, request, &url,
                                        &headers, &options);
    if (!reply.ok()) return reply;
    std::unique_ptr<file_detail::RemoteHttp> response;
    reply = file_detail::RemoteHttp::Open(
        url, options, request.method, headers, request.body,
        kProviderResponseBytes - bytes, deadline, &response);
    if (!reply.ok()) return reply;
    return ReadProviderResponse(*response, &bytes, output, request.binary,
                                request.dropbox_content);
  };
  return InvokeProvider(endpoint.provider_id, method, params, fetch, result,
                        error);
}
}  // namespace q4t::server::mcp_detail
