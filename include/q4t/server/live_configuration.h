// Native Live configuration transactions; publication belongs to the session.
#pragma once

#include <cstdint>
#include <functional>

#include "q4t/server/live_shape.h"

namespace q4t::server {
enum class LiveTransport { kWebSocket, kWebRtc, kSip };

struct LiveConfigurationContext {
  LiveTransport transport = LiveTransport::kWebSocket;
  std::string default_instructions;
  io::Json default_voice;
  // Trusted native adapters. Count real tokenizer/rendered history tokens,
  // never bytes or an estimate. Validate model, voice ownership and every
  // selected backend before acknowledging the effective configuration.
  // These callbacks are bounded preparation, without generation/publication.
  std::function<Status(std::string_view, size_t*, ChatPlanError*)> count_text;
  std::function<Status(const io::Json&, size_t*, ChatPlanError*)> count_history;
  std::function<Status(const io::Json&, ChatPlanError*)> validate;
};

// Pure and transactional: neither input nor output changes on failure. Native
// defaults are exposed as their real identities, not another model's voices.
Status ResolveLiveConfiguration(const io::Json& input,
                                const LiveConfigurationContext& context,
                                io::Json* output, ChatPlanError* error);
Status ResolveLiveForkConfiguration(const io::Json& state,
                                    const io::Json& overrides,
                                    const LiveConfigurationContext& context,
                                    io::Json* output, ChatPlanError* error);
Status UpdateLiveConfiguration(const io::Json& current, const io::Json& update,
                               const LiveConfigurationContext& context,
                               io::Json* output, ChatPlanError* error);
// Session identity and expiry come from the actual owner, never client fields.
// The SDK keeps status=active even in a terminal session.closed snapshot.
Status LiveSessionResource(const io::Json& configuration, std::string_view id,
                           uint64_t expires_at, io::Json* output,
                           ChatPlanError* error);
}  // namespace q4t::server
