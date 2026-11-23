#include "q4t/server/agent_vault.h"

#include <algorithm>
#include <chrono>

#include "object_protocol.h"
#include "vault_credential_internal.h"
#include "q4t/server/application_query.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace vault_detail {
using namespace object_detail;
constexpr std::string_view kRoot = "/v1/vaults";

ObjectReply Invalid(std::string message, std::string param) {
  auto out = Error(400, "invalid_request", std::move(message));
  out.value = Object({{"param", String(std::move(param))}});
  return out;
}

// Input is validated UTF-8. Match Unicode White_Space at the two edges.
std::string_view TrimName(std::string_view raw) {
  constexpr std::string_view spaces[] = {
      "\u0085", "\u00a0", "\u1680", "\u2000", "\u2001", "\u2002", "\u2003",
      "\u2004", "\u2005", "\u2006", "\u2007", "\u2008", "\u2009", "\u200a",
      "\u2028", "\u2029", "\u202f", "\u205f", "\u3000"};
  constexpr std::string_view ascii = " \t\r\n\v\f";
  while (!raw.empty()) {
    const auto start = raw.find_first_not_of(ascii);
    if (start == raw.npos) return {};
    raw.remove_prefix(start);
    const auto found = std::ranges::find_if(
        spaces, [&](auto space) { return raw.starts_with(space); });
    if (found == std::end(spaces)) break;
    raw.remove_prefix(found->size());
  }
  while (!raw.empty()) {
    const auto end = raw.find_last_not_of(ascii);
    if (end == raw.npos) return {};
    raw = raw.substr(0, end + 1);
    const auto found = std::ranges::find_if(
        spaces, [&](auto space) { return raw.ends_with(space); });
    if (found == std::end(spaces)) break;
    raw.remove_suffix(found->size());
  }
  return raw;
}

ObjectReply Public(const Json& stored) {
  const auto* name = stored.Find("name");
  const auto* meta = stored.Find("metadata");
  if (stored.GetString("object") != "vault" ||
      !ValidId(stored.GetString("id")) || stored.GetInt("created_at") <= 0 ||
      !name || !(name->IsNull() || name->IsString()) || !meta ||
      !meta->IsObject())
    return Error(503, "vault_record_invalid", "Invalid stored vault");
  ObjectReply out;
  out.value = Object({{"id", *stored.Find("id")},
                      {"object", String("vault")},
                      {"created_at", *stored.Find("created_at")},
                      {"name", *name},
                      {"metadata", *meta}});
  return out;
}

ObjectReply ParseListOptions(std::string_view raw, ListOptions* options) {
  auto query = ParseApplicationQuery(raw, true);
  if (!query.ok()) return query;
  auto paging = Object(), filters = Object();
  bool active = true, archived = true;
  for (const auto& [key, value] : query.value.object) {
    if (key == "after" || key == "limit" || key == "order") {
      Set(&paging, key, value);
    } else if (key == "status") {
      active = false;
      archived = false;
      for (const auto& status : value.array) {
        if (status.str == "active")
          active = true;
        else if (status.str == "archived")
          archived = true;
        else
          return Invalid("Unknown vault status", "status");
      }
    } else if (key.starts_with("metadata[") && key.ends_with(']') &&
               key.size() > 10) {
      Set(&filters, key.substr(9, key.size() - 10), value);
    } else {
      return Invalid("Unknown vault list option", key);
    }
  }
  auto metadata = NormalizeConversationMetadata(filters);
  if (!metadata.ok()) return metadata;
  ChatPlanError error;
  auto valid = ParseItemViewOptions(paging, &options->paging, &error, true);
  if (!valid) return Invalid(valid.message(), error.param);
  options->active = active;
  options->archived = archived;
  options->metadata = std::move(metadata.value);
  return {};
}

bool ListOptions::Matches(bool is_archived, const Json& meta) const {
  if (is_archived ? !archived : !active) return false;
  return std::ranges::all_of(metadata.object, [&](const auto& pair) {
    const auto* value = meta.Find(pair.first);
    return value && value->IsString() && value->str == pair.second.str;
  });
}

ObjectReply List(ObjectStore& store, std::string_view raw,
                 const StateAccess& access) {
  ListOptions options;
  auto valid = ParseListOptions(raw, &options);
  if (!valid.ok()) return valid;
  ObjectRequest req;
  req.kind = ObjectKind::kVault;
  req.scope = access.scope;
  req.operation = ObjectOperation::kList;
  req.descending = options.paging.descending;
  req.after = options.paging.after;
  req.limit = std::min(16, options.paging.limit + 1);
  auto rows = Array();
  bool more = false;
  for (;;) {
    auto page = store.Execute(req, access.deadline, access.cancelled);
    if (!page.ok()) return page;
    const auto* data = page.value.GetArray("data");
    if (!data) return Error(503, "vault_record_invalid", "Invalid vault page");
    for (const auto& record : data->array) {
      const auto* vault = record.Find("data");
      if (!vault) return Error(503, "vault_record_invalid", "Missing vault");
      auto item = Public(*vault);
      if (!item.ok()) return item;
      if (!options.Matches(vault->GetBool("archived"),
                           *item.value.Find("metadata")))
        continue;
      if (rows.array.size() == static_cast<size_t>(options.paging.limit)) {
        more = true;
        break;
      }
      rows.array.push_back(std::move(item.value));
    }
    if (more || !page.value.GetBool("has_more")) break;
    const auto cursor = page.value.GetString("last_id");
    if (cursor.empty() || cursor == req.after)
      return Error(503, "vault_record_invalid", "Invalid vault page cursor");
    req.after = cursor;
  }
  return ListPage(std::move(rows), more);
}

ObjectReply ListPage(Json rows, bool more) {
  auto first = rows.array.empty() ? Json{} : *rows.array.front().Find("id");
  auto last = rows.array.empty() ? Json{} : *rows.array.back().Find("id");
  ObjectReply out;
  out.value = Object({{"object", String("list")},
                      {"data", std::move(rows)},
                      {"has_more", Boolean(more)},
                      {"first_id", first},
                      {"last_id", last}});
  return out;
}
}  // namespace vault_detail

ObjectReply CheckAgentVaults(ObjectStore& objects, const StateAccess& access,
                             const io::Json& vaults) {
  using namespace vault_detail;
  if (!vaults.IsArray() || vaults.array.size() > 10)
    return Invalid("Expected up to ten Vault IDs", "vault_ids");
  ObjectRequest lookup;
  lookup.kind = ObjectKind::kVault;
  lookup.scope = access.scope;
  for (const auto& id : vaults.array) {
    if (!id.str.starts_with("vault_") || !ValidId(id.str))
      return Error(404, "object_not_found", "Vault not found");
    lookup.id = id.str;
    auto found = objects.Execute(lookup, access.deadline, access.cancelled);
    if (!found.ok()) return found;
    const auto* data = found.value.Find("data");
    if (!data || data->GetString("id") != id.str ||
        data->GetString("object") != "vault")
      return Error(503, "vault_record_invalid", "Invalid vault record");
    if (data->GetBool("archived"))
      return Error(404, "object_not_found", "Vault not found");
  }
  return {};
}

ObjectReply HandleAgentVault(ObjectStore& store, std::string_view method,
                             std::string_view target, const std::string& body,
                             const StateAccess& access) {
  const auto split = target.find('?');
  const auto path = target.substr(0, split);
  using namespace vault_detail;
  const auto route = ParseVaultRoute(target);
  const bool collection = route.kind == VaultRouteKind::kCredentials ||
                          route.kind == VaultRouteKind::kCollection;
  const bool create = path == kRoot;
  if (!IsVaultTarget(target))
    return Error(404, "route_not_found", "No vault route");
  if (!ManagedAgentMethodAllowed(method, target)) {
    auto error = Error(405, "method_not_allowed", "Method not allowed");
    error.value = Object(
        {{"allow", String(collection ? "GET, POST" : "GET, POST, DELETE")}});
    return error;
  }
  if (method != "POST" && !body.empty())
    return Invalid("Unexpected body", "body");
  if (route.kind == VaultRouteKind::kCredentials ||
      route.kind == VaultRouteKind::kCredential) {
    if (split != target.npos && !(collection && method == "GET"))
      return Invalid("Unexpected query", "query");
    return CredentialRequest(
        store, route, method,
        split == target.npos ? "" : target.substr(split + 1), body, access);
  }
  if (create && method == "GET")
    return List(store, split == target.npos ? "" : target.substr(split + 1),
                access);
  if (split != target.npos) return Invalid("Unexpected query", "query");
  Json input = Object();
  if (method == "POST") {
    if (!create || !body.empty()) {
      auto valid = io::ParseJson(body, &input, true, kSchemaJsonLimits);
      if (!valid) return Invalid(valid.message(), "body");
      if (create && input.IsNull()) input = Object();
    }
    std::string param;
    auto valid = InputFields(input, {"name", "metadata"}, &param, "");
    if (!valid) return Invalid(valid.message(), param);
  }
  ObjectRequest req;
  req.kind = ObjectKind::kVault;
  req.scope = access.scope;
  auto execute = [&] {
    return store.Execute(req, access.deadline, access.cancelled);
  };
  Json row;
  if (create) {
    req.id = NewObjectId(req.kind);
    if (req.id.empty())
      return Error(503, "vault_id_unavailable", "No vault ID");
    const auto now = std::chrono::duration_cast<std::chrono::seconds>(
                         std::chrono::system_clock::now().time_since_epoch())
                         .count();
    row = Object({{"id", String(req.id)},
                  {"object", String("vault")},
                  {"created_at", Number(now)},
                  {"name", {}},
                  {"metadata", Object()}});
  } else {
    req.id = path.substr(kRoot.size() + 1);
    auto saved = execute();
    if (!saved.ok()) return saved;
    row = Field(saved.value, "data");
    auto public_row = Public(row);
    if (method == "GET" || !public_row.ok()) return public_row;
    if (row.GetBool("archived") && method == "POST")
      return Error(409, "vault_archived", "Archived vault cannot be updated");
    req.expected_revision = saved.value.GetInt("revision");
  }
  if (const auto* name = input.Find("name")) {
    if (name->IsNull() && !create)
      Set(&row, "name", {});
    else {
      const auto trimmed = TrimName(name->str);
      if (!name->IsString() || trimmed.empty() || trimmed.size() > 256)
        return Invalid("Name must contain 1 to 256 UTF-8 bytes after trimming",
                       "name");
      Set(&row, "name", String(std::string(trimmed)));
    }
  }
  if (const auto* meta = input.Find("metadata")) {
    if ((!create && meta->IsNull()) || meta->Has(""))
      return Invalid("Expected metadata with nonempty keys", "metadata");
    auto valid = NormalizeConversationMetadata(*meta);
    if (!valid.ok()) return valid;
    Set(&row, "metadata", std::move(valid.value));
  }
  if (method == "DELETE") {
    auto archived = ArchiveCredentials(&row);
    if (!archived.ok()) return archived;
    Set(&row, "archived", Boolean(true));
  }
  req.operation = ObjectOperation::kPut;
  req.data = row;
  req.ttl_seconds = 0;
  auto saved = execute();
  if (saved.commit_unknown) saved.value = Object({{"id", String(req.id)}});
  if (!saved.ok()) return saved;
  if (method != "DELETE") {
    auto result = Public(row);
    if (result.ok() && create) result.http_status = 201;
    return result;
  }
  saved.value = Object({{"object", String("vault.deleted")},
                        {"id", String(req.id)},
                        {"deleted", Boolean(true)}});
  return saved;
}
}  // namespace q4t::server
