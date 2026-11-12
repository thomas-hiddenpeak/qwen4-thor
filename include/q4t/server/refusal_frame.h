// Model-output adapter, using ordinary vocabulary bytes, not special tokens.
#pragma once

#include <cstddef>
#include <string_view>

namespace q4t::server {

inline constexpr std::string_view kRefusalOpen = "<q4t_refusal>";
inline constexpr std::string_view kRefusalClose = "</q4t_refusal>";
inline constexpr size_t kRefusalWhitespace = 16;

inline constexpr std::string_view kRefusalInstruction =
    "# Refusal output\n\nFor a final answer that refuses the user's request, "
    "output <q4t_refusal> followed by a nonempty refusal message and "
    "</q4t_refusal>, then stop. Start this frame at the beginning of the "
    "final answer, after </think> if reasoning is enabled. Do not add normal "
    "text, JSON or tool calls to a refusal. This is an output control frame, "
    "not a tool. A refusal takes precedence over the response schema or "
    "required tool choice. For all other answers, use the normal response "
    "format and tool protocol. Do not use this frame merely to report a "
    "tool error, an empty result or uncertainty.";

enum class RefusalPrefix { kOrdinary, kPending, kRefusal };

inline bool RefusalSpace(char c) {
  return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

// Only the initial final-answer prefix is reserved. After an ordinary byte,
// identical marker bytes are data. Lookahead is bounded even for whitespace.
inline RefusalPrefix ProbeRefusalPrefix(std::string_view text,
                                        size_t* whitespace) {
  *whitespace = 0;
  while (*whitespace < text.size() && RefusalSpace(text[*whitespace])) {
    if (++*whitespace > kRefusalWhitespace) return RefusalPrefix::kOrdinary;
  }
  text.remove_prefix(*whitespace);
  if (text.starts_with(kRefusalOpen)) return RefusalPrefix::kRefusal;
  return kRefusalOpen.starts_with(text) ? RefusalPrefix::kPending
                                        : RefusalPrefix::kOrdinary;
}

}  // namespace q4t::server
