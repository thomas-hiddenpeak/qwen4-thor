#include "vault_credential_internal.h"

#include <algorithm>
#include <chrono>
#include <ranges>
#include <stdexcept>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/request_json.h"

namespace q4t::server::vault_detail {
namespace {
using namespace object_detail;

ObjectReply Invalid(std::string param) {
  auto out = Error(400, "invalid_request", "Invalid credential request");
  out.value = Object({{"param", String(std::move(param))}});
  return out;
}
ObjectReply Missing() {
  return Error(404, "object_not_found", "Credential not found");
}
ObjectReply Corrupt() {
  return Error(503, "credential_state_invalid", "Invalid stored credential");
}
int64_t Now() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}
ObjectReply PublicCredential(const Json& row, std::string_view vault) {
  const auto* resource = row.Find("resource");
  ChatPlanError error;
  if (!resource || !row.Find("archived") || !row.Find("archived")->IsBool() ||
      resource->GetString("vault_id") != vault ||
      !ValidateCredential(*resource, CredentialShape::kResource, &error) ||
      (row.GetBool("archived") ? row.Has("sealed") : row.GetString("sealed").empty()))
    return Corrupt();
  ObjectReply result;
  result.value = *resource;
  return result;
}

ObjectReply ListCredentials(const Json& entries, std::string_view vault,
                            std::string_view query) {
  ListOptions options;
  auto parsed = ParseListOptions(query, &options);
  if (!parsed.ok()) return parsed;
  auto rows = Array();
  bool after = options.paging.after.empty();
  bool more = false;
  for (size_t n = 0; n < entries.array.size(); ++n) {
    const auto index =
        options.paging.descending ? entries.array.size() - n - 1 : n;
    const auto& row = entries.array[index];
    auto resource = PublicCredential(row, vault);
    if (!resource.ok()) return resource;
    if (!after) {
      after = resource.value.GetString("id") == options.paging.after;
      continue;
    }
    if (!options.Matches(row.GetBool("archived"),
                         *resource.value.Find("metadata")))
      continue;
    if (rows.array.size() == static_cast<size_t>(options.paging.limit)) {
      more = true;
      break;
    }
    rows.array.push_back(std::move(resource.value));
  }
  if (!after) return Missing();
  return ListPage(std::move(rows), more);
}

void Archive(Json* row) {
  std::erase_if(row->object,
                [](const auto& field) { return field.first == "sealed"; });
  Set(row, "archived", Boolean(true));
}

ObjectReply ReadInput(const std::string& body, bool create, Json* input) {
  if (!io::ParseJson(body, input, true, kSchemaJsonLimits))
    return Invalid("body");
  if (create) {
    ChatPlanError error;
    if (!ValidateCredential(*input, CredentialShape::kCreate, &error))
      return Invalid("body");
    const auto name = TrimName(input->Find("name")->str);
    if (name.empty() || name.size() > 256) return Invalid("name");
    Set(input, "name", String(std::string(name)));
  } else {
    std::string param;
    if (!InputFields(*input, {"metadata", "auth"}, &param))
      return Invalid("body");
  }
  if (const auto* meta = input->Find("metadata")) {
    if (!meta->IsObject() || meta->Has("")) return Invalid("metadata");
    auto valid = NormalizeConversationMetadata(*meta);
    if (!valid.ok()) return valid;
  }
  return {};
}

}  // namespace

ObjectReply ArchiveCredentials(Json* vault) {
  if (!vault->Has("credentials")) return {};
  auto& entries = Field(*vault, "credentials");
  if (!entries.IsArray()) return Corrupt();
  for (auto& row : entries.array) {
    auto valid = PublicCredential(row, vault->GetString("id"));
    if (!valid.ok()) return valid;
    Archive(&row);
  }
  return {};
}

ObjectReply CredentialRequest(ObjectStore& store, const VaultRoute& route,
                              std::string_view method, std::string_view query,
                              const std::string& body,
                              const StateAccess& access) {
  const bool collection = route.kind == VaultRouteKind::kCredentials;
  ObjectRequest req;
  req.kind = ObjectKind::kVault;
  req.id = route.vault;
  req.scope = access.scope;
  auto saved = store.Execute(req, access.deadline, access.cancelled);
  if (!saved.ok()) return saved;
  auto vault = Field(saved.value, "data");
  if (vault.GetString("id") != route.vault ||
      vault.GetString("object") != "vault")
    return Corrupt();
  if (!vault.Has("credentials")) Set(&vault, "credentials", Array());
  auto& entries = Field(vault, "credentials");
  if (!entries.IsArray()) return Corrupt();
  if (collection && method == "GET")
    return ListCredentials(entries, route.vault, query);
  if (vault.GetBool("archived") && method != "GET")
    return Error(409, "vault_archived", "Archived vault cannot be updated");

  Json row, resource;
  std::string id(route.credential);
  size_t index = entries.array.size();
  if (!collection) {
    auto found = std::ranges::find_if(entries.array, [&](const Json& entry) {
      const auto* item = entry.Find("resource");
      return item && item->GetString("id") == id;
    });
    if (found == entries.array.end()) return Missing();
    row = *found;
    index = static_cast<size_t>(found - entries.array.begin());
    auto public_row = PublicCredential(row, route.vault);
    if (!public_row.ok() || method == "GET") return public_row;
    if (row.GetBool("archived") && method == "POST")
      return Error(409, "credential_archived",
                   "Archived credential cannot be updated");
    resource = std::move(public_row.value);
  }

  if (method == "DELETE") {
    Archive(&row);
  } else {
    Json input;
    auto valid = ReadInput(body, collection, &input);
    if (!valid.ok()) return valid;
    if (collection) {
      try {
        id = detail::NewRequestId("cred_");
      } catch (const std::runtime_error&) {
        return Error(503, "credential_id_unavailable", "No credential ID");
      }
      resource = Object({{"id", String(id)},
                         {"object", String("vault.credential")},
                         {"vault_id", String(req.id)},
                         {"name", *input.Find("name")},
                         {"created_at", Number(Now())},
                         {"metadata", Object()}});
      row = Object({{"archived", Boolean(false)}});
    }
    if (const auto* auth = input.Find("auth")) {
      Json projection;
      std::string sealed;
      auto result = collection
                        ? SealCredentialAuth(store, access, req.id, id, *auth,
                                             &projection, &sealed)
                        : RotateCredentialAuth(store, access, req.id, id,
                                               row.GetString("sealed"), *auth,
                                               &projection, &sealed);
      if (!result.ok()) return result;
      Set(&resource, "auth", std::move(projection));
      Set(&row, "sealed", String(std::move(sealed)));
    }
    if (const auto* meta = input.Find("metadata"))
      Set(&resource, "metadata", *meta);
    Set(&resource, "updated_at",
        Number(std::max({Now(), resource.GetInt("created_at"),
                         resource.GetInt("updated_at")})));
    Set(&row, "resource", resource);
    valid = PublicCredential(row, route.vault);
    if (!valid.ok()) return valid;
  }
  if (collection)
    entries.array.push_back(std::move(row));
  else
    entries.array[index] = std::move(row);
  // One parent revision owns the entries and their encrypted secrets. A
  // concurrent archive, creation, or rotation cannot restore an old secret.
  req.operation = ObjectOperation::kPut;
  req.expected_revision = saved.value.GetInt("revision");
  req.ttl_seconds = 0;
  req.data = std::move(vault);
  auto result = store.Execute(req, access.deadline, access.cancelled);
  if (result.commit_unknown) result.value = Object({{"id", String(id)}});
  if (!result.ok()) return result;
  result.value = method == "DELETE"
                     ? Object({{"id", String(id)},
                               {"object", String("vault.credential.deleted")},
                               {"deleted", Boolean(true)}})
                     : std::move(resource);
  return result;
}
}  // namespace q4t::server::vault_detail
