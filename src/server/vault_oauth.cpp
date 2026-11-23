#include "mcp_transport_internal.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/native_crypto.h"
#include "remote_http_internal.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
constexpr size_t kExchangeBytes = 65536;

std::string FormValue(std::string_view value) {
  constexpr char hex[] = "0123456789ABCDEF";
  std::string out;
  for (unsigned char c : value) {
    if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
        (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '*')
      out += static_cast<char>(c);
    else if (c == ' ')
      out += '+';
    else {
      out += '%';
      out += hex[c >> 4];
      out += hex[c & 15];
    }
  }
  return out;
}

bool Text(const Json& obj, const char* key) {
  const auto* v = obj.Find(key);
  return v && v->IsString() && !v->str.empty() && v->str.size() <= 8192 &&
         std::ranges::all_of(
             v->str, [](unsigned char c) { return c >= 32 && c != 127; });
}
ObjectReply InvalidResponse() {
  return Error(502, "oauth_refresh_response_invalid",
               "Invalid token endpoint response");
}
}  // namespace

ObjectReply BuildGrant(const Json& grant,
                       std::map<std::string, std::string>* headers,
                       std::string* body) {
  const auto* auth = grant.Find("token_endpoint_auth");
  if (!Text(grant, "refresh_token") || !Text(grant, "client_id") || !auth)
    return Error(400, "oauth_refresh_invalid", "Invalid stored refresh grant");
  *headers = {{"Content-Type", "application/x-www-form-urlencoded"},
              {"Accept", "application/json"}};
  *body = "grant_type=refresh_token";
  std::string basic, encoded;
  ScopeExit wipe([&] {
    CryptoWipe(&basic);
    CryptoWipe(&encoded);
  });
  const auto append = [&](const char* key, const Json& source) {
    *body += '&';
    *body += key;
    *body += '=';
    *body += FormValue(source.Find(key)->str);
  };
  append("refresh_token", grant);
  const auto method = auth->GetString("type");
  if (method == "none" || method == "client_secret_post") {
    append("client_id", grant);
    if (method == "client_secret_post" && Text(*auth, "client_secret"))
      append("client_secret", *auth);
    else if (method == "client_secret_post")
      return Error(400, "oauth_refresh_invalid", "Missing client secret");
  } else if (method == "client_secret_basic" && Text(*auth, "client_secret")) {
    basic = FormValue(grant.Find("client_id")->str) + ':' +
            FormValue(auth->Find("client_secret")->str);
    if (!io::EncodeBase64(basic, 28000, &encoded))
      return Error(413, "oauth_refresh_capacity",
                   "Client credentials exceed limit");
    headers->emplace("Authorization", "Basic " + encoded);
  } else {
    return Error(400, "oauth_refresh_invalid",
                 "Invalid token endpoint authentication");
  }
  for (const auto* key : {"resource", "scope"}) {
    if (!Nonnull(grant, key)) continue;
    if (!Text(grant, key))
      return Error(400, "oauth_refresh_invalid",
                   "Invalid refresh grant parameter");
    append(key, grant);
  }
  return {};
}

ObjectReply DecodeGrant(const std::string& response, std::time_t now,
                        Json* destination) {
  Json result;
  if (!Parse(response, &result) || !Text(result, "access_token") ||
      !std::ranges::all_of(
          result.Find("access_token")->str,
          [](unsigned char c) { return c >= 0x21 && c <= 0x7e; }) ||
      http::Lower(result.GetString("token_type")) != "bearer" ||
      (result.Has("refresh_token") && !Text(result, "refresh_token")))
    return InvalidResponse();
  auto patch = Object({{"type", String("mcp_oauth")},
                       {"access_token", *result.Find("access_token")}});
  if (const auto* ttl = result.Find("expires_in")) {
    if (now < 0 || !IsInputInteger(*ttl) || ttl->number <= 0 ||
        ttl->number > 253402300799LL - now)
      return InvalidResponse();
    std::time_t expires = now + ttl->AsInt();
    std::tm utc{};
    char text[32]{};
    if (!gmtime_r(&expires, &utc) ||
        !std::strftime(text, sizeof(text), "%Y-%m-%dT%H:%M:%SZ", &utc))
      return InvalidResponse();
    Set(&patch, "expires_at", String(text));
  }
  auto replacement = Object();
  if (result.Has("refresh_token"))
    Set(&replacement, "refresh_token", *result.Find("refresh_token"));
  if (result.Has("scope")) {
    if (!Text(result, "scope")) return InvalidResponse();
    // The response describes this access token. Keep the owner's configured
    // refresh scope, including when the issuer downscopes an exchange.
  }
  if (!replacement.object.empty())
    Set(&patch, "refresh", std::move(replacement));
  *destination = std::move(patch);
  return {};
}

ObjectReply RefreshGrant(const McpEndpoint& endpoint,
                         const FileFetchOptions& deployment, const Json& grant,
                         std::chrono::steady_clock::time_point deadline,
                         const std::function<bool()>& permit,
                         McpTransportReceipt* receipt) {
  using namespace file_detail;
  Url url;
  if (!receipt || !permit || !endpoint.exact_target ||
      endpoint.url != grant.GetString("token_endpoint") ||
      endpoint.url.find('#') != endpoint.url.npos ||
      !ParseUrl(endpoint.url, &url) || !url.tls || !AllowedUrl(url, deployment))
    return Error(400, "oauth_refresh_invalid", "Invalid stored refresh grant");
  RemoteHeaders headers;
  std::string body, response;
  ScopeExit wipe([&] {
    CryptoWipe(&body);
    CryptoWipe(&response);
    for (auto& [_, value] : headers) CryptoWipe(&value);
  });
  auto prepared = BuildGrant(grant, &headers, &body);
  if (!prepared.ok()) return prepared;
  std::unique_ptr<RemoteHttp> http;
  auto opened =
      RemoteHttp::Open(url, deployment, "POST", headers, body, kExchangeBytes,
                       deadline, &http, kExchangeBytes, [&] {
                         receipt->call_permitted = permit();
                         receipt->outcome_unknown = receipt->call_permitted;
                         return receipt->call_permitted;
                       });
  if (!opened.ok()) return opened;
  if (http->Status() != 200 || http->Mime() != "application/json" ||
      !http->ReadAll(&response).ok())
    return InvalidResponse();
  auto parsed = DecodeGrant(response, std::time(nullptr), &receipt->result);
  if (!parsed.ok()) return parsed;
  receipt->outcome_unknown = false;
  return {};
}
}  // namespace q4t::server::mcp_detail
