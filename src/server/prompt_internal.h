#pragma once

#include "object_protocol.h"
#include "q4t/server/prompt_registry.h"
#include "q4t/server/request_limits.h"

namespace q4t::server::prompt_detail {
inline constexpr size_t kDefinitionBytes = 256 * 1024;
inline constexpr size_t kExpandedBytes = kExpandedApplicationBytes;
inline constexpr size_t kVersions = 64;
ObjectReply Invalid(std::string message, std::string param);
ObjectReply ValidateDefinition(const io::Json& definition,
                               const ChatPlanConfig& config);
ObjectReply Render(const io::Json& definition, const io::Json& variables);
bool Version(std::string_view version);
io::Json Public(const io::Json& record);
}  // namespace q4t::server::prompt_detail
