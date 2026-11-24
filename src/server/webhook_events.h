#pragma once

#include <cstdint>
#include <string_view>
#include "q4t/io/json.h"

namespace q4t::server::webhook_detail {
// Kept in the worker's bounded metadata index, never decoded from a caller's
// ownership fields. A bit identifies one terminal business transition.
uint32_t EventBit(std::string_view name);
io::Json EventTypes();
uint32_t Subscriptions(const io::Json& data);
std::string_view TerminalEvent(std::string_view kind, const io::Json& data);
io::Json PendingEvent(const std::string& owner_digest, const std::string& id,
                      std::string_view type, const io::Json& targets,
                      int64_t created, int64_t ttl);
}  // namespace q4t::server::webhook_detail
