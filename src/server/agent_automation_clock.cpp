#include "agent_automation_internal.h"

#include <chrono>
#include <exception>

#include "object_protocol.h"
#include "q4t/server/agent_cron.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::automation_detail {
namespace {
using namespace object_detail;

ObjectReply Invalid(std::string key, std::string message) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(key))}});
  return reply;
}
}  // namespace

ObjectReply ApplyClock(const Json& patch, int64_t now, Json* resource) {
  if (resource->GetString("type") == "once") {
    for (const auto* name : {"expression", "timezone"})
      if (patch.Has(name))
        return Invalid(name, "This field requires a periodic automation");
    if (const auto* at = patch.Find("run_at")) {
      if (!IsInputInteger(*at) || at->AsInt() <= now)
        return Invalid("run_at", "Expected a future Unix timestamp");
      if (resource->GetString("status") == "completed")
        return Invalid("run_at", "The one-time occurrence already finished");
      Set(resource, "run_at", *at);
    }
    return {};
  }
  if (patch.Has("run_at"))
    return Invalid("run_at", "This field requires a one-time automation");
  if (const auto* expression = patch.Find("expression")) {
    if (!expression->IsString() || !AgentCron::Parse(expression->str))
      return Invalid("expression", "Expected a five-field cron expression");
    Set(resource, "expression", *expression);
  }
  if (const auto* zone = patch.Find("timezone")) {
    if (!zone->IsString() || zone->str.empty() || zone->str.size() > 128)
      return Invalid("timezone", "Expected an IANA time zone");
    const std::chrono::tzdb* database;
    try {
      database = &std::chrono::get_tzdb();
    } catch (const std::exception&) {
      return Error(503, "automation_timezone_unavailable",
                   "System timezone data unavailable");
    }
    try {
      database->locate_zone(zone->str);
    } catch (const std::runtime_error&) {
      return Invalid("timezone", "Unknown IANA time zone");
    }
    Set(resource, "timezone", *zone);
  }
  return {};
}
}  // namespace q4t::server::automation_detail
