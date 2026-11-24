#include "q4t/server/webhook_dispatcher.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/native_crypto.h"
#include "webhook_keys.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
struct Tenant {
  std::string scope, after;
};
enum class Attempt { kIdle, kAcknowledged, kRetry, kError };

ObjectReply Subscribe(ObjectStore& objects, const StateAccess& access,
                      const FileFetchOptions& policy, const Json& event,
                      const std::string& id, bool* removed) {
  ObjectRequest req;
  req.scope = access.scope;
  req.kind = ObjectKind::kWebhookEndpoint;
  req.id = id;
  auto read = objects.Execute(req, access.deadline, access.cancelled);
  if (!read.ok()) {
    if (read.http_status == 404) *removed = true;
    return read;
  }
  auto& record = Field(read.value, "data");
  const auto* endpoint = record.Find("resource");
  if (!endpoint || endpoint->GetString("id") != id)
    return Error(503, "webhook_record_invalid", "Invalid stored endpoint");
  const auto* types = endpoint->GetArray("event_types");
  if (!types)
    return Error(503, "webhook_record_invalid", "Invalid subscriptions");
  if (std::ranges::none_of(types->array, [&](const Json& type) {
        return type.str == event.GetString("type");
      })) {
    *removed = true;
    return {};
  }
  Json keys;
  auto opened = webhook_detail::ReadKeys(objects, access, id, record,
                                         std::time(nullptr), &keys);
  if (!opened.ok()) return opened;
  std::vector<std::string> signing;
  for (auto& key : keys.array)
    signing.push_back(std::move(Field(key, "secret").str));
  auto sent = DeliverWebhook(access, policy, endpoint->GetString("url"), event,
                             signing);
  for (auto& key : signing) CryptoWipe(&key);
  return sent;
}

Attempt Process(ObjectStore& objects, const StateAccess& access,
                const FileFetchOptions& network, const Json& row) {
  ObjectRequest req;
  req.kind = ObjectKind::kWebhookEvent;
  req.scope = access.scope;
  req.id = row.GetString("id");
  const auto* data = row.Find("data");
  if (!data || data->GetString("format") != "q4t.webhook.event.v1")
    return Attempt::kError;
  req.data = *data;
  req.expected_revision = row.GetInt("revision");
  req.ttl_seconds = 0;
  req.expires_at = row.GetInt("expires_at");
  auto& deliveries = Field(req.data, "deliveries");
  const auto* event = req.data.Find("event");
  if (!event || event->GetString("id") != req.id || !deliveries.IsArray())
    return Attempt::kError;
  const auto now = std::time(nullptr);
  auto execute = [&] {
    return objects.Execute(req, access.deadline, access.cancelled);
  };
  if (req.data.GetInt("deliver_before") <= now) {
    req.operation = ObjectOperation::kErase;
    return execute().ok() ? Attempt::kIdle : Attempt::kError;
  }
  for (auto& target : deliveries.array) {
    if (target.GetString("status") != "pending" ||
        target.GetInt("next_attempt_at") > now)
      continue;
    const auto endpoint = target.GetString("endpoint");
    const auto attempts = target.GetInt("attempts", -1);
    if (attempts < 0 || attempts > 20000 || !endpoint.starts_with("whe_") ||
        !ValidId(endpoint))
      return Attempt::kError;
    // Reserve before the external side effect. After process loss the same
    // event ID is eligible again, even if the subscriber already handled it.
    req.operation = ObjectOperation::kPut;
    Set(&target, "attempts", Number(attempts + 1));
    Set(&target, "next_attempt_at", Number(now + 60));
    auto claimed = execute();
    if (!claimed.ok()) return Attempt::kError;
    req.expected_revision = claimed.value.GetInt("revision");
    bool removed = false;
    auto sent = Subscribe(objects, access, network, *event, endpoint, &removed);
    const auto status = sent.ok() ? sent.value.AsInt() : 0;
    const bool ok = status >= 200 && status < 300;
    if (removed || ok) {
      Set(&target, "status", String(removed ? "removed" : "delivered"));
    } else {
      const auto delay =
          std::min<int64_t>(6 * 3600, 30LL << std::min<int64_t>(attempts, 10));
      Set(&target, "next_attempt_at", Number(std::time(nullptr) + delay));
    }
    Set(&target, "http_status", Number(status));
    if (!execute().ok()) return Attempt::kError;
    return removed ? Attempt::kIdle
           : ok    ? Attempt::kAcknowledged
                   : Attempt::kRetry;
  }
  if (std::ranges::none_of(deliveries.array, [](const Json& target) {
        return target.GetString("status") == "pending";
      })) {
    req.operation = ObjectOperation::kErase;
    if (!execute().ok()) return Attempt::kError;
  }
  return Attempt::kIdle;
}
}  // namespace

WebhookDispatcher::WebhookDispatcher(ObjectStore& objects,
                                     ClientAdmission& clients,
                                     FileFetchOptions network)
    : thread_([this, &objects, &clients,
               network = std::move(network)](std::stop_token stop) {
        Run(objects, clients, network, stop);
      }) {}
void WebhookDispatcher::RequestStop() { thread_.request_stop(); }
void WebhookDispatcher::Join() {
  if (thread_.joinable()) thread_.join();
}
Json WebhookDispatcher::Health() const {
  return Object({{"acknowledged", Number(acknowledged_.load())},
                 {"retries", Number(retries_.load())},
                 {"errors", Number(errors_.load())},
                 {"capacity_deferrals", Number(deferred_.load())},
                 {"capacity_losses", Number(dropped_.load())}});
}

void WebhookDispatcher::Run(ObjectStore& objects, ClientAdmission& clients,
                            const FileFetchOptions& network,
                            std::stop_token stop) {
  std::array<Tenant, kMaxClients> tenants;
  std::string cursor;
  const auto cancelled = [&] { return stop.stop_requested(); };
  while (!cancelled()) {
    auto delay = 2s;
    try {
      const auto diag =
          objects.ReconcileWebhooks(cursor, Clock::now() + 2s, cancelled);
      if (diag.ok()) {
        deferred_ = diag.value.GetInt("capacity_deferrals");
        dropped_ = diag.value.GetInt("capacity_losses");
        cursor = diag.value.GetString("after");
      } else if (!cancelled()) {
        ++errors_;
      }
      const auto count = std::max<size_t>(1, clients.Size());
      for (size_t i = 0; i < count && !cancelled(); ++i) {
        const auto identity = clients.RecoveryIdentity(i);
        auto& tenant = tenants.at(i);
        if (!identity.Allowed()) {
          tenant = {};
          continue;
        }
        const auto scope = identity.Scoped()
                               ? "client:" + std::string(identity.Id())
                               : "local:anonymous";
        if (scope != tenant.scope) tenant = {scope, {}};
        ObjectRequest req;
        req.kind = ObjectKind::kWebhookEvent;
        req.operation = ObjectOperation::kList;
        req.scope = scope;
        req.limit = 4;
        req.after = tenant.after;
        req.descending = false;
        auto page = objects.Execute(req, Clock::now() + 2s, cancelled);
        if (!page.ok()) {
          if (page.code != "invalid_object_cursor") ++errors_;
          tenant.after.clear();
          continue;
        }
        const auto* rows = page.value.GetArray("data");
        if (!rows) {
          ++errors_;
          continue;
        }
        tenant.after = page.value.GetBool("has_more")
                           ? page.value.GetString("last_id")
                           : "";
        if (!tenant.after.empty()) delay = 0s;
        for (const auto& row : rows->array) {
          if (cancelled()) break;
          StateAccess access{
              scope,
              Clock::now() + std::chrono::milliseconds(network.timeout_ms) + 8s,
              cancelled};
          const auto attempt = Process(objects, access, network, row);
          if (attempt == Attempt::kAcknowledged) ++acknowledged_;
          if (attempt == Attempt::kRetry) ++retries_;
          if (attempt == Attempt::kError && !cancelled()) ++errors_;
        }
      }
    } catch (...) {
      ++errors_;
      delay = 2s;
    }
    std::unique_lock lock(mutex_);
    changed_.wait_for(lock, stop, delay, [] { return false; });
  }
}
}  // namespace q4t::server
