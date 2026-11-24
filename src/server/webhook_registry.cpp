#include "q4t/server/webhook.h"

#include <algorithm>
#include <array>
#include <ctime>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/request_json.h"
#include "q4t/server/native_crypto.h"
#include "webhook_keys.h"
#include "webhook_events.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Kind = WebhookRouteKind;

ObjectReply Invalid(std::string key, std::string message) {
  auto out = Error(400, "invalid_request", std::move(message));
  out.value = Object({{"param", String(std::move(key))}});
  return out;
}

ObjectReply Resource(const Json& data, const std::string& id) {
  const auto* row = data.Find("resource");
  ChatPlanError error;
  if (data.GetString("format") != "q4t.webhook.endpoint.v1" || !row ||
      row->GetString("id") != id || !ValidateWebhook(*row, "endpoint", &error))
    return Error(503, "webhook_record_invalid", "Invalid stored endpoint");
  ObjectReply out;
  out.value = *row;
  return out;
}

ObjectReply List(ObjectStore& objects, const StateAccess& access,
                 std::string_view raw) {
  auto query = ParseApplicationQuery(raw);
  if (!query.ok()) return query;
  for (const auto& [key, unused] : query.value.object)
    if (key != "limit" && key != "after") return Invalid(key, "Unknown query");
  ItemViewOptions page;
  ChatPlanError error;
  auto status = ParseItemViewOptions(query.value, &page, &error);
  if (!status) return Invalid(error.param, status.message());
  ObjectRequest req;
  req.operation = ObjectOperation::kList;
  req.scope = access.scope;
  req.kind = ObjectKind::kWebhookEndpoint;
  req.limit = page.limit;
  req.after = page.after;
  auto listed = objects.Execute(req, access.deadline, access.cancelled);
  if (!listed.ok()) return listed;
  auto& data = Field(listed.value, "data");
  if (!data.IsArray())
    return Error(503, "webhook_record_invalid", "Invalid endpoint page");
  for (auto& row : data.array) {
    auto projected = Resource(Field(row, "data"), row.GetString("id"));
    if (!projected.ok()) return projected;
    row = std::move(projected.value);
  }
  Set(&listed.value, "object", String("list"));
  return listed;
}

ObjectReply Apply(const Json& body, const FileFetchOptions* network,
                  Json* endpoint) {
  if (const auto* name = body.Find("name")) {
    const auto length = std::ranges::count_if(
        name->str, [](unsigned char c) { return (c & 0xc0) != 0x80; });
    if (!length || length > 256)
      return Invalid("name", "Expected 1 to 256 characters");
    Set(endpoint, "name", *name);
  }
  if (const auto* wire = body.Find("url")) {
    file_detail::Url url;
    if (wire->str.size() > 2048 || !wire->str.starts_with("https://") ||
        wire->str.find('#') != std::string::npos ||
        !file_detail::ParseUrl(wire->str, &url) || !url.tls)
      return Invalid("url", "Expected an HTTPS endpoint URL");
    if (network && !file_detail::AllowedUrl(url, *network))
      return Error(403, "webhook_target_denied",
                   "Subscriber target is not permitted");
    Set(endpoint, "url", *wire);
  }
  if (const auto* events = body.Find("event_types")) {
    if (events->array.empty())
      return Invalid("event_types", "Choose at least one event");
    Json normalized = Array();
    for (const auto& item : events->array) {
      if (!webhook_detail::EventBit(item.str))
        return Invalid("event_types", "Event is unavailable to this project");
      if (std::ranges::none_of(normalized.array, [&](const Json& prior) {
            return item.str == prior.str;
          }))
        normalized.array.push_back(item);
    }
    Set(endpoint, "event_types", std::move(normalized));
  }
  return {};
}

ObjectReply Test(ObjectStore& objects, const StateAccess& access,
                 const FileFetchOptions& network, const Json& record,
                 const Json& endpoint, const std::string& type, int64_t now) {
  if (!webhook_detail::EventBit(type))
    return Invalid("event_type", "Event is unavailable to this project");
  const auto id = NewObjectId(ObjectKind::kWebhookEvent);
  if (id.empty()) return Error(503, "webhook_id_unavailable", "No event ID");
  auto event = Object({{"object", String("event")},
                       {"id", String(id)},
                       {"type", String(type)},
                       {"created_at", Number(now)},
                       {"data", webhook_detail::SamplePayload(type, id.substr(4))}});
  Json keys;
  auto opened = webhook_detail::ReadKeys(
      objects, access, endpoint.GetString("id"), record, now, &keys);
  if (!opened.ok()) return opened;
  std::vector<std::string> signing;
  for (auto& entry : keys.array)
    signing.push_back(std::move(Field(entry, "secret").str));
  auto result = DeliverWebhook(access, network, endpoint.GetString("url"),
                               event, signing);
  for (auto& secret : signing) CryptoWipe(&secret);
  if (result.ok())
    result.value = Object({{"object", String("webhook_endpoint.test")},
                           {"webhook_endpoint_id", *endpoint.Find("id")},
                           {"event_type", String(type)},
                           {"status_code", result.value},
                           {"success", Boolean(true)}});
  return result;
}
}  // namespace

ObjectReply HandleWebhook(ObjectStore& objects, const StateAccess& access,
                          const FileFetchOptions* network,
                          std::string_view method, std::string_view target,
                          const std::string& wire) {
  const auto route = ParseWebhookRoute(target);
  if (route.kind == Kind::kNone)
    return Error(404, "route_not_found", "Webhook route not found");
  if (!WebhookMethodAllowed(method, target)) {
    auto out = Error(405, "method_not_allowed", "Method not allowed");
    out.value =
        Object({{"allow", String(std::string(WebhookAllow(route.kind)))}});
    return out;
  }
  const auto split = target.find('?');
  const auto raw = split == target.npos ? "" : target.substr(split + 1);
  if (method != "POST" && !wire.empty())
    return Invalid("body", "Unexpected body");
  if (route.kind == Kind::kCollection && method == "GET")
    return List(objects, access, raw);
  if (!raw.empty()) return Invalid("query", "Unexpected query parameters");
  if (route.kind == Kind::kCatalog) {
    auto events = webhook_detail::EventTypes();
    ObjectReply out;
    out.value =
        Object({{"object", String("list")}, {"data", std::move(events)}});
    return out;
  }
  const bool create = route.kind == Kind::kCollection;
  auto body = Object();
  ChatPlanError error;
  if (method == "POST") {
    if (!wire.empty() &&
        (!io::ParseJson(wire, &body, true, kApplicationJsonLimits) ||
         !body.IsObject()))
      return Invalid("body", "Expected a JSON object");
    const char* shape = create                        ? "create"
                        : route.kind == Kind::kRotate ? "rotate"
                        : route.kind == Kind::kTest   ? "test"
                                                      : "update";
    auto valid = ValidateWebhook(body, shape, &error);
    if (!valid) return Invalid(error.param, valid.message());
  }
  ObjectRequest req;
  req.scope = access.scope;
  req.kind = ObjectKind::kWebhookEndpoint;
  req.id = create ? NewObjectId(req.kind) : std::string(route.id);
  req.ttl_seconds = 0;
  if (req.id.empty())
    return Error(503, "webhook_id_unavailable", "No endpoint ID");
  auto execute = [&] {
    return objects.Execute(req, access.deadline, access.cancelled);
  };
  const auto now = std::time(nullptr);
  Json endpoint;
  if (create) {
    endpoint = Object({{"object", String("webhook_endpoint")},
                       {"id", String(req.id)},
                       {"created_at", Number(now)},
                       {"updated_at", Number(now)}});
    req.data = Object({{"format", String("q4t.webhook.endpoint.v1")}});
  } else {
    auto prior = execute();
    if (!prior.ok()) return prior;
    req.expected_revision = prior.value.GetInt("revision");
    req.data = Field(prior.value, "data");
    auto found = Resource(req.data, req.id);
    if (!found.ok() || method == "GET") return found;
    endpoint = std::move(found.value);
    if (method == "DELETE") {
      req.operation = ObjectOperation::kErase;
      auto erased = execute();
      if (erased.ok())
        erased.value = Object({{"object", String("webhook_endpoint.deleted")},
                               {"id", String(req.id)},
                               {"deleted", Boolean(true)}});
      if (erased.commit_unknown)
        erased.value = Object({{"id", String(req.id)}});
      return erased;
    }
    if (route.kind == Kind::kTest) {
      if (!network)
        return Error(503, "webhook_policy_unavailable",
                     "Webhook delivery policy is unavailable");
      return Test(objects, access, *network, req.data, endpoint,
                  body.GetString("event_type"), now);
    }
  }
  if (create || route.kind == Kind::kEndpoint) {
    const auto before = OutputJson(endpoint);
    auto applied = Apply(body, network, &endpoint);
    if (!applied.ok()) return applied;
    if (!create && OutputJson(endpoint) == before) {
      ObjectReply unchanged;
      unchanged.value = std::move(endpoint);
      return unchanged;
    }
  }
  std::string secret;
  if (create || route.kind == Kind::kRotate) {
    auto keys = Array();
    if (!create) {
      auto opened = webhook_detail::ReadKeys(objects, access, req.id, req.data,
                                             now, &keys);
      if (!opened.ok()) return opened;
    }
    auto rotated = webhook_detail::Rotate(
        objects, access, req.id, now,
        body.GetBool("keep_old_secret_active_for_24_hours"), std::move(keys),
        &req.data, &secret);
    if (!rotated.ok()) return rotated;
    Set(&endpoint, "signing_secret_hint",
        String("…" + secret.substr(secret.size() - 5)));
  }
  Set(&endpoint, "updated_at",
      Number(std::max<int64_t>(now, endpoint.GetInt("updated_at"))));
  Set(&req.data, "resource", endpoint);
  req.operation = ObjectOperation::kPut;
  auto saved = execute();
  if (saved.ok()) {
    saved.value = std::move(endpoint);
    if (!secret.empty())
      Set(&saved.value, "signing_secret", String(std::move(secret)));
  } else {
    CryptoWipe(&secret);
    if (saved.commit_unknown) saved.value = Object({{"id", String(req.id)}});
  }
  return saved;
}
}  // namespace q4t::server
