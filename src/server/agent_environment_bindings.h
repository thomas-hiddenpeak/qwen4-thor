#pragma once

#include <algorithm>
#include <cstddef>
#include <string_view>

#include "q4t/io/json.h"

namespace q4t::server::environment_detail {
inline constexpr std::size_t kMaxAttachedSessions = 128;
inline constexpr int64_t kEnvironmentIdleSeconds = 20 * 60;
inline constexpr int64_t kCheckpointSeconds = 7 * 24 * 3600;

// Failed preparation retains only public status, its journal and live members.
inline bool ValidFailedPreparation(const io::Json& data) {
  const auto* resource = data.Find("resource");
  const auto* events = data.GetArray("environment_events");
  const size_t fields = 4 + (data.Has("q4t_webhook_deferred") ? 1 : 0) +
                        (events ? 2 : 0);
  if (!resource || data.object.size() != fields ||
      resource->object.size() != 7 ||
      resource->GetString("object") != "agent.environment" ||
      resource->GetString("type") != "openai_hosted" ||
      resource->GetString("status") != "failed" ||
      data.GetInt("failed_at") <= 0 ||
      (data.GetString("failure_code") != "workspace_source_invalid" &&
       data.GetString("failure_code") != "workspace_definition_invalid" &&
       data.GetString("failure_code") != "workspace_preparation_expired"))
    return false;
  const auto* sessions = data.GetArray("attached_sessions");
  if (!sessions || sessions->array.size() > kMaxAttachedSessions ||
      (!sessions->array.empty() && !events))
    return false;
  if (events && (events->array.empty() || data.GetInt("environment_sequence") <= 0 ||
                 events->array.back().GetString("status") != "failed"))
    return false;
  for (const auto* key : {"files", "plugins", "skills"}) {
    const auto* entries = resource->GetArray(key);
    if (!entries || !entries->array.empty()) return false;
  }
  return true;
}

inline bool HasAttachedSession(const io::Json& data, std::string_view id) {
  if (id.empty()) return false;
  const auto* sessions = data.GetArray("attached_sessions");
  if (!sessions) return data.GetString("bound_session") == id;
  return std::ranges::any_of(sessions->array, [&](const io::Json& value) {
    return value.IsString() && value.str == id;
  });
}
}  // namespace q4t::server::environment_detail
