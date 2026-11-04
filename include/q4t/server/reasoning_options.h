// Parsed reasoning transport, historical aliases, and generation budget input.
#pragma once

#include <string>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

inline constexpr std::string_view kReasoningProfile = "q4t-reasoning-v1";
enum class ReasoningField { kReasoningContent, kReasoning };
std::string_view ReasoningFieldName(ReasoningField field);

struct ReasoningOptions {
  ReasoningField field = ReasoningField::kReasoningContent;
  bool include = true;
  int64_t token_budget = -1;  // Absent/null; otherwise a nonnegative integer.
};

Status ParseReasoningOptions(const io::Json& request, ReasoningOptions* options,
                             std::string* error_param = nullptr);

// Returns a view into message, or empty for absent/null. Validates both aliases
// even when the template will omit historical reasoning.
Status ResolveMessageReasoning(const io::Json& message, size_t index,
                               std::string_view* reasoning,
                               std::string* error_param = nullptr);

}  // namespace q4t::server
