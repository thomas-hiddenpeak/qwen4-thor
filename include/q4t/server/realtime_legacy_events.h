#pragma once

#include <optional>

#include "q4t/server/object_store.h"

namespace q4t::server {
// Keep private sampling separate from the validated native wire object.
ObjectReply DecodeLegacyRealtimeEvent(io::Json* event,
                                      std::optional<double>* temperature);
// False denotes the native duplicate of an item already emitted to beta.
bool EncodeLegacyRealtimeEvent(io::Json* event, double temperature,
                               int max_output_tokens, std::string_view voice);
}  // namespace q4t::server
