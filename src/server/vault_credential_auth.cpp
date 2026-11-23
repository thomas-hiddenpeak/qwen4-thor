#include "vault_credential_internal.h"

#include <algorithm>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/request_json.h"
#include "q4t/server/state_cipher.h"
#include "schema_string.h"

namespace q4t::server::vault_detail {
namespace {
using namespace object_detail;

ObjectReply Invalid(std::string_view field) {
  auto error =
      Error(400, "invalid_request", "Invalid credential configuration");
  error.value = Object({{"param", String(std::string(field))}});
  return error;
}

void Wipe(Json* value) {
  CryptoWipe(&value->str);
  for (auto& item : value->array) Wipe(&item);
  for (auto& [_, item] : value->object) Wipe(&item);
}

struct ProtectedAuth {
  Json value;
  std::string wire;
  ~ProtectedAuth() {
    Wipe(&value);
    CryptoWipe(&wire);
  }
};

bool SafeValue(const Json& auth, const char* key) {
  const auto* secret = auth.Find(key);
  return secret && secret->IsString() && !secret->str.empty() &&
         secret->str.find_first_of("\r\n\0", 0, 3) == std::string::npos;
}

bool Https(const std::string& value) {
  file_detail::Url url;
  return value.find('#') == value.npos && file_detail::ParseUrl(value, &url) &&
         url.tls;
}

bool EnvironmentName(std::string_view name) {
  const auto alpha = [](char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_';
  };
  if (name.empty() || !alpha(name.front()) || name.starts_with("CODEX_") ||
      !std::ranges::all_of(
          name, [&](char c) { return alpha(c) || (c >= '0' && c <= '9'); }))
    return false;
  const std::string lower = http::Lower(name);
  constexpr std::string_view reserved[] = {
      "all_proxy",          "http_proxy",     "https_proxy",
      "no_proxy",           "ssl_cert_file",  "ssl_cert_dir",
      "requests_ca_bundle", "curl_ca_bundle", "node_extra_ca_certs"};
  return std::ranges::find(reserved, lower) == std::end(reserved);
}

ObjectReply Networking(Json& config) {
  if (config.GetString("type") == "unrestricted") return {};
  auto& hosts = Field(config, "allowed_hosts").array;
  if (hosts.empty() || hosts.size() > 16)
    return Invalid("auth.networking.allowed_hosts");
  std::set<std::string> seen;
  for (auto& host : hosts) {
    const auto value = http::Lower(host.str);
    if (!SchemaFormatValid("hostname", value) ||
        value.find_first_of(":/@*?#") != value.npos ||
        !seen.insert(value).second)
      return Invalid("auth.networking.allowed_hosts");
    host = String(value);
  }
  return {};
}

Json NullableField(const Json& value, const char* name) {
  const auto* field = value.Find(name);
  return field ? *field : Json{};
}

ObjectReply Project(Json& config, Json* out) {
  const auto type = config.GetString("type");
  Json result = Object({{"type", String(type)}});
  if (type == "environment_variable") {
    if (!EnvironmentName(config.GetString("secret_name")) ||
        !SafeValue(config, "secret_value"))
      return Invalid("auth");
    auto& network = Field(config, "networking");
    auto valid = Networking(network);
    if (!valid.ok()) return valid;
    Set(&result, "secret_name", *config.Find("secret_name"));
    Set(&result, "networking", network);
  } else {
    if (!Https(config.GetString("mcp_server_url")))
      return Invalid("auth.mcp_server_url");
    Set(&result, "mcp_server_url", *config.Find("mcp_server_url"));
    if (type == "static_bearer") {
      if (!SafeValue(config, "token")) return Invalid("auth.token");
    } else {
      if (!SafeValue(config, "access_token"))
        return Invalid("auth.access_token");
      auto expiry = NullableField(config, "expires_at");
      if (!expiry.IsNull() && !SchemaFormatValid("date-time", expiry.str))
        return Invalid("auth.expires_at");
      Set(&result, "expires_at", std::move(expiry));
      Json refresh;
      if (const auto* src = config.Find("refresh"); src && !src->IsNull()) {
        const auto& auth = *src->Find("token_endpoint_auth");
        if (!Https(src->GetString("token_endpoint")) ||
            !SafeValue(*src, "client_id") ||
            !SafeValue(*src, "refresh_token") ||
            (auth.GetString("type") != "none" &&
             !SafeValue(auth, "client_secret")))
          return Invalid("auth.refresh");
        refresh = Object(
            {{"client_id", *src->Find("client_id")},
             {"token_endpoint", *src->Find("token_endpoint")},
             {"scope", NullableField(*src, "scope")},
             {"resource", NullableField(*src, "resource")},
             {"token_endpoint_auth", Object({{"type", *auth.Find("type")}})}});
      }
      Set(&result, "refresh", std::move(refresh));
    }
  }
  *out = std::move(result);
  return {};
}

StateCipherBinding Binding(const std::string& vault, const std::string& id) {
  return {"q4t-vault", vault, "credential-auth", id};
}

ObjectReply Patch(Json& current, const Json& changes) {
  const auto type = current.GetString("type");
  if (type != changes.GetString("type")) return Invalid("auth.type");
  if (type != "mcp_oauth") {
    const auto key = type == "static_bearer" ? "token" : "secret_value";
    Set(&current, key, *changes.Find(key));
    return {};
  }
  if (Nonnull(changes, "access_token")) {
    Set(&current, "access_token", *changes.Find("access_token"));
    Set(&current, "expires_at", {});
  }
  if (changes.Has("expires_at"))
    Set(&current, "expires_at", *changes.Find("expires_at"));
  const auto* refresh = changes.Find("refresh");
  if (!refresh || refresh->IsNull()) return {};
  if (!Nonnull(current, "refresh")) return Invalid("auth.refresh");
  auto& stored = Field(current, "refresh");
  if (Nonnull(*refresh, "refresh_token"))
    Set(&stored, "refresh_token", *refresh->Find("refresh_token"));
  if (refresh->Has("scope")) Set(&stored, "scope", *refresh->Find("scope"));
  if (Nonnull(*refresh, "token_endpoint_auth")) {
    const auto& incoming = *refresh->Find("token_endpoint_auth");
    auto& method = Field(stored, "token_endpoint_auth");
    if (incoming.GetString("type") != method.GetString("type"))
      return Invalid("auth.refresh.token_endpoint_auth.type");
    if (Nonnull(incoming, "client_secret"))
      Set(&method, "client_secret", *incoming.Find("client_secret"));
  }
  return {};
}
}  // namespace

ObjectReply SealCredentialAuth(ObjectStore& store, const StateAccess& access,
                               const std::string& vault, const std::string& id,
                               const Json& auth, Json* public_auth,
                               std::string* sealed) {
  ChatPlanError error;
  if (!ValidateCredential(auth, CredentialShape::kAuthCreate, &error))
    return Invalid("auth");
  ProtectedAuth config;
  config.value = auth;
  Json projection;
  auto valid = Project(config.value, &projection);
  if (!valid.ok()) return valid;
  config.wire = OutputJson(config.value);
  auto result =
      StateCipher(store).Seal(config.wire, Binding(vault, id), access, sealed);
  if (result.ok()) *public_auth = std::move(projection);
  return result;
}

ObjectReply ResolveMcpToken(ObjectStore& store, const StateAccess& access,
                            const Json& vaults, std::string_view id,
                            const std::string& destination,
                            std::string* token) {
  CryptoWipe(token);
  const auto corrupt = [] {
    return Error(503, "credential_state_invalid", "Invalid stored credential");
  };
  const auto missing = [] {
    return Error(404, "object_not_found", "Attached MCP credential not found");
  };
  file_detail::Url target;
  if (!token || !vaults.IsArray()) return corrupt();
  if (destination.find('#') != destination.npos ||
      !file_detail::ParseUrl(destination, &target))
    return Invalid("transport.server_url");
  const auto matches = [&](const Json& auth) {
    file_detail::Url address;
    const auto url = auth.GetString("mcp_server_url");
    return target.tls && url.find('#') == url.npos &&
           file_detail::ParseUrl(url, &address) && address.tls &&
           target.host == address.host && target.port == address.port &&
           target.target == address.target;
  };

  ObjectRequest req;
  req.kind = ObjectKind::kVault;
  req.scope = access.scope;
  std::set<std::string> visited;
  std::string selected_vault, selected_id, sealed;
  bool revoked = false;
  for (const auto& attached : vaults.array) {
    if (!attached.IsString() || !attached.str.starts_with("vault_") ||
        !ValidId(attached.str))
      return missing();
    if (!visited.insert(attached.str).second) continue;
    req.id = attached.str;
    auto record = store.Execute(req, access.deadline, access.cancelled);
    if (!record.ok()) return record;
    const auto* vault = record.value.Find("data");
    if (!vault || vault->GetString("object") != "vault" ||
        vault->GetString("id") != req.id)
      return corrupt();
    const auto* rows = vault->Find("credentials");
    if (!rows) continue;
    if (!rows->IsArray()) return corrupt();
    for (const auto& row : rows->array) {
      const auto* resource = row.Find("resource");
      if (!resource || resource->GetString("vault_id") != req.id)
        return corrupt();
      const auto key = resource->GetString("id");
      if (!id.empty() && key != id) continue;
      const auto* auth = resource->Find("auth");
      if (!auth || !row.Find("archived") || !row.Find("archived")->IsBool())
        return corrupt();
      if (!matches(*auth)) continue;
      if (vault->GetBool("archived") || row.GetBool("archived")) {
        revoked = true;
        continue;
      }
      if (!selected_id.empty())
        return Error(409, "mcp_credential_ambiguous",
                     "Select one matching attached credential");
      if (!ValidId(key) || row.GetString("sealed").empty()) return corrupt();
      selected_vault = req.id;
      selected_id = key;
      sealed = row.GetString("sealed");
    }
  }
  if (selected_id.empty())
    return !id.empty() || revoked ? missing() : ObjectReply{};
  ProtectedAuth config;
  auto opened = StateCipher(store).Open(
      sealed, Binding(selected_vault, selected_id), access, &config.wire);
  if (!opened.ok()) return opened;
  ChatPlanError error;
  if (!io::ParseJson(config.wire, &config.value, true, kSchemaJsonLimits) ||
      !ValidateCredential(config.value, CredentialShape::kAuthCreate, &error) ||
      !matches(config.value))
    return corrupt();
  const auto type = config.value.GetString("type");
  if (type != "static_bearer" && type != "mcp_oauth") return corrupt();
  const auto* expiry = config.value.Find("expires_at");
  if (expiry && !expiry->IsNull() &&
      !SchemaDateTimeAfter(expiry->str, std::chrono::system_clock::now()))
    return Error(409, "mcp_credential_expired", "MCP access token has expired");
  const auto* field = type == "static_bearer" ? "token" : "access_token";
  if (!SafeValue(config.value, field)) return corrupt();
  *token = config.value.Find(field)->str;
  return {};
}

ObjectReply RotateCredentialAuth(ObjectStore& store, const StateAccess& access,
                                 const std::string& vault,
                                 const std::string& id,
                                 const std::string& previous, const Json& patch,
                                 Json* public_auth, std::string* sealed) {
  ChatPlanError error;
  if (!ValidateCredential(patch, CredentialShape::kAuthRotate, &error))
    return Invalid("auth");
  ProtectedAuth config;
  auto opened = StateCipher(store).Open(previous, Binding(vault, id), access,
                                        &config.wire);
  if (!opened.ok()) return opened;
  if (!io::ParseJson(config.wire, &config.value, true, kSchemaJsonLimits) ||
      !ValidateCredential(config.value, CredentialShape::kAuthCreate, &error))
    return Error(503, "credential_state_invalid",
                 "Stored credential state is invalid");
  auto changed = Patch(config.value, patch);
  if (!changed.ok()) return changed;
  return SealCredentialAuth(store, access, vault, id, config.value, public_auth,
                            sealed);
}
}  // namespace q4t::server::vault_detail
