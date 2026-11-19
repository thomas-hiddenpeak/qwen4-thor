// Transactional prompt/default/explicit-setting precedence for one session.
#pragma once

#include "q4t/server/prompt_registry.h"

namespace q4t::server {
struct RealtimePromptSelection {
  io::Json settings;
  std::optional<PromptSnapshot> prompt;
};
using RealtimePromptResolver =
    std::function<ObjectReply(const io::Json&, size_t, PromptSnapshot*)>;
using RealtimePromptValidator =
    std::function<ObjectReply(const RealtimePromptSelection&)>;

// The session owner supplies a bounded lookup; workers receive value copies.
// Defaults are not explicit overrides. Ordinary updates never re-read a
// selected template. Validation precedes the only commit of any new state.
class RealtimePromptSettings {
 public:
  static constexpr size_t kMaximumBytes = 64ULL << 20;
  static constexpr size_t kMaximumExpansionBytes = 16ULL << 20;
  explicit RealtimePromptSettings(io::Json defaults);
  const RealtimePromptSelection& Current() const { return current_; }
  ObjectReply Update(const io::Json& update,
                     const RealtimePromptResolver& resolve,
                     const RealtimePromptValidator& validate);
  // An absent prompt inherits the pinned session template; null clears it
  // only for this response. Response-only fields remain in the caller's
  // response object and are not made into session settings.
  ObjectReply Response(const io::Json& response,
                       const RealtimePromptResolver& resolve,
                       const RealtimePromptValidator& validate,
                       RealtimePromptSelection* selected) const;

 private:
  ObjectReply Select(const io::Json& update,
                     const RealtimePromptResolver& resolve,
                     std::optional<PromptSnapshot>* prompt) const;
  ObjectReply Compose(const io::Json& overrides,
                      std::optional<PromptSnapshot> prompt, bool referenced,
                      RealtimePromptSelection* selected) const;
  io::Json defaults_, overrides_;
  bool referenced_ = false;
  RealtimePromptSelection current_;
};
}  // namespace q4t::server
