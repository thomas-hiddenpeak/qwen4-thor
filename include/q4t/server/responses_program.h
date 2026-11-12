#pragma once

#include <vector>

#include "q4t/server/state_cipher.h"

namespace q4t::server {
inline constexpr char kResponsesProgramModelName[] = "q4t_native_program";

// Omitted/null is direct-only. Validation and authorization are distinct:
// invalid declarations never grant access, including in historical catalogs.
Status ValidateResponsesAllowedCallers(const io::Json& definition,
                                       ChatPlanError* error,
                                       const std::string& path);
bool ResponsesToolAllows(const io::Json& definition, std::string_view caller);

// Private request-owned state. Construct only through authenticated opening;
// never accept these fields from a public request or stored arbitrary metadata.
struct ResponsesProgramState {
  io::Json program;
  io::Json tools;
  io::Json worker_input;
  io::Json terminal;
  std::vector<io::Json> calls;
  size_t input_index = 0;
};

// active_tools is the validated, ordered catalog at invocation, after deferred
// discovery. The sealed snapshot remains authoritative if later tools change.
ObjectReply CreateResponsesProgram(StateCipher& cipher, std::string code,
                                   const io::Json& active_tools,
                                   const ChatPlanConfig& config,
                                   const StateAccess& access,
                                   io::Json* program);
// Opens code/authority and collects exactly linked child calls and ordered
// result events. This does not execute JavaScript or perform external tools.
ObjectReply OpenResponsesPrograms(StateCipher& cipher, const io::Json& history,
                                  const ChatPlanConfig& config,
                                  const StateAccess& access,
                                  std::vector<ResponsesProgramState>* programs);
// Replays only local computation. Emits only NEW client dispatches or the
// terminal result; previously returned calls never become external work again.
ObjectReply AdvanceResponsesProgram(const ResponsesProgramState& program,
                                    const StateAccess& access,
                                    io::Json* emitted);
}  // namespace q4t::server
