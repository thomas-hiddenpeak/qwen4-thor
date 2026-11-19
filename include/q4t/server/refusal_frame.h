// Model-output adapter, using ordinary vocabulary bytes, not special tokens.
#pragma once

#include <cstddef>
#include <string_view>

namespace q4t::server {

inline constexpr std::string_view kRefusalOpen = "<q4t_refusal>";
inline constexpr std::string_view kRefusalClose = "</q4t_refusal>";
inline constexpr size_t kRefusalWhitespace = 16;

inline constexpr std::string_view kRefusalInstruction =
    "# Response encoding\n\nReturn ordinary answers in the requested text, "
    "JSON, or tool format. Only when you choose to refuse the task, "
    "output <q4t_refusal> followed by your refusal and </q4t_refusal>, "
    "with no other final-answer content. This is a formatting rule, "
    "not a reason to refuse.";

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
