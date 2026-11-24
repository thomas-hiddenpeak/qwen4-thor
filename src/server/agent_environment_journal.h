#pragma once

#include "object_protocol.h"

namespace q4t::server::environment_detail {
// A live subscriber may be between polls while a workspace suspends and
// reconnects. Keep both transitions in the transaction that changes status.
// Only compact public identities are retained here, never file contents.
inline constexpr size_t kEnvironmentEventLimit = 64;

inline void AppendEnvironmentStatus(io::Json* data, const char* status,
                                    const std::string& session = {},
                                    const std::string& turn = {}) {
  using namespace object_detail;
  const auto sequence = data->GetInt("environment_sequence") + 1;
  Set(data, "environment_sequence", Number(sequence));
  if (!data->GetArray("environment_events"))
    Set(data, "environment_events", Array());
  auto& rows = Field(*data, "environment_events").array;
  if (rows.size() >= kEnvironmentEventLimit) rows.erase(rows.begin());
  auto event =
      Object({{"sequence", Number(sequence)}, {"status", String(status)}});
  if (!turn.empty()) {
    Set(&event, "session_id", String(session));
    Set(&event, "turn_id", String(turn));
  }
  rows.push_back(std::move(event));
}
}  // namespace q4t::server::environment_detail
