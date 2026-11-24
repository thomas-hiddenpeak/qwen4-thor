#pragma once

#include <cstdint>
#include <string_view>
#include "q4t/io/json.h"

namespace q4t::server::webhook_detail {
// Kept in the worker's bounded metadata index, never decoded from a caller's
// ownership fields. A bit identifies a subscribed event type.
uint32_t EventBit(std::string_view name);
io::Json EventTypes();
uint32_t Subscriptions(const io::Json& data);
bool IsEventSource(std::string_view kind);
std::string_view Transition(std::string_view kind, const io::Json& data,
                            bool creating);
io::Json EventPayload(std::string_view type, const std::string& id,
                      const io::Json& source);
io::Json SamplePayload(std::string_view type, std::string_view suffix);
io::Json PendingEvent(const std::string& owner_digest, const std::string& id,
                      std::string_view type, io::Json payload,
                      const io::Json& targets, int64_t created, int64_t ttl);
}  // namespace q4t::server::webhook_detail
