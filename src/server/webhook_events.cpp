#include "webhook_events.h"

#include <array>
#include "object_protocol.h"

namespace q4t::server::webhook_detail {
namespace {
using namespace object_detail;
constexpr std::array<std::string_view, 17> kTypes = {
    "response.completed",        "response.failed",
    "response.cancelled",        "response.incomplete",
    "batch.completed",           "batch.failed",
    "batch.cancelled",           "batch.expired",
    "agent.session.created",     "agent.environment.ready",
    "agent.session.idle",        "agent.session.in_progress",
    "agent.session.failed",      "agent.session.action_required",
    "agent.environment.expired", "agent.environment.suspended",
    "agent.environment.failed"};
}  // namespace
uint32_t EventBit(std::string_view name) {
  for (size_t i = 0; i < kTypes.size(); ++i)
    if (kTypes[i] == name) return 1U << i;
  return 0;
}
Json EventTypes() {
  auto result = Array();
  for (auto type : kTypes) result.array.push_back(String(std::string(type)));
  return result;
}
uint32_t Subscriptions(const Json& data) {
  const auto* resource = data.Find("resource");
  const auto* types = resource ? resource->GetArray("event_types") : nullptr;
  if (data.GetString("format") != "q4t.webhook.endpoint.v1" || !types ||
      resource->GetString("object") != "webhook_endpoint")
    return 0;
  uint32_t mask = 0;
  for (const auto& type : types->array) mask |= EventBit(type.str);
  return mask;
}
bool IsEventSource(std::string_view kind) {
  return kind == "agent_session" || kind == "environment" || kind == "batch" ||
         kind == "response";
}

std::string_view Transition(std::string_view kind, const Json& data,
                            bool creating) {
  if (kind == "agent_session") {
    const auto* session = data.Find("session");
    return creating && session &&
                   session->GetString("object") == "agent.session"
               ? "agent.session.created"
               : "";
  }
  if (kind == "environment") {
    const auto* resource = data.Find("resource");
    if (!resource || resource->GetString("object") != "agent.environment" ||
        resource->GetString("type") != "openai_hosted")
      return {};
    const auto status = resource->GetString("status");
    if (creating && status == "ready") return "agent.environment.ready";
    if (status == "failed") return "agent.environment.failed";
    if (status == "expired") return "agent.environment.expired";
    if (status == "suspended") return "agent.environment.suspended";
    return {};
  }
  if (kind != "response" && kind != "batch") return {};
  const auto* item = data.Find(kind == "response" ? "response" : "batch");
  if (!item || (kind == "response" && !item->GetBool("background"))) return {};
  const auto status = item->GetString("status");
  for (const auto name : kTypes)
    if (name.starts_with(kind) && name.substr(kind.size() + 1) == status)
      return name;
  return {};
}
Json EventPayload(std::string_view type, const std::string& id,
                  const Json& source) {
  auto data = Object({{"id", String(id)}});
  if (type.starts_with("agent.session.")) {
    const auto* session = source.Find("session");
    const auto* env = session ? session->Find("environment") : nullptr;
    if (!env || session->GetString("id") != id ||
        env->GetString("type").empty())
      return {};
    if (type == "agent.session.action_required") {
      const auto* actions = session->GetArray("required_actions");
      if (!actions || actions->array.empty()) return {};
      const auto action = actions->array.front().GetString("type");
      if (action != "function_call" && action != "environment_connection" &&
          action != "computer_use_approval_request")
        return {};
      Set(&data, "required_action", Object({{"type", String(action)}}));
      return data;
    }
    Set(&data, "environment_type", String(env->GetString("type")));
    if (!env->GetString("id").empty())
      Set(&data, "environment_id", String(env->GetString("id")));
  }
  return data;
}

Json SamplePayload(std::string_view type, std::string_view suffix) {
  if (type == "agent.session.action_required")
    return Object(
        {{"id", String("agent_session_test_" + std::string(suffix))},
         {"required_action", Object({{"type", String("function_call")}})}});
  if (type.starts_with("agent.session."))
    return Object({{"id", String("agent_session_test_" + std::string(suffix))},
                   {"environment_type", String("none")}});
  std::string prefix = type.starts_with("batch.")               ? "batch_test_"
                       : type.starts_with("agent.environment.") ? "env_test_"
                                                                : "resp_test_";
  return Object({{"id", String(prefix + std::string(suffix))}});
}

Json PendingEvent(const std::string& owner_digest, const std::string& id,
                  std::string_view type, Json payload, const Json& targets,
                  int64_t created, int64_t ttl) {
  // Completion and initial creation each have one immutable event identity.
  // A retry after an uncertain commit must have the same event identity.
  const auto event_id = "evt_" + Digest("webhook.v1/" + owner_digest + "/" +
                                        id + "/" + std::string(type))
                                     .substr(0, 32);
  const auto event = Object({{"id", String(event_id)},
                             {"object", String("event")},
                             {"type", String(std::string(type))},
                             {"created_at", Number(created)},
                             {"data", std::move(payload)}});
  auto deliveries = Array();
  for (const auto& target : targets.array)
    deliveries.array.push_back(Object({{"endpoint", target},
                                       {"attempts", Number(0)},
                                       {"status", String("pending")},
                                       {"next_attempt_at", Number(created)}}));
  auto record = Object({{"format", String("q4t.webhook.event.v1")},
                        {"event", event},
                        {"deliveries", std::move(deliveries)},
                        {"deliver_before", Number(created + ttl)}});
  return Object({{"operation", String("put")},
                 {"kind", String("webhook_event")},
                 {"id", String(event_id)},
                 {"owner", String(owner_digest)},
                 {"data", std::move(record)},
                 {"revision", Number(0)},
                 {"ttl", Number(ttl)},
                 {"created_at", Number(created)},
                 {"expires_at", Number(created + ttl)}});
}
}  // namespace q4t::server::webhook_detail
