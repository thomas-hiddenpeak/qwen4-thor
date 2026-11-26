#pragma once

#include <vector>

#include "q4t/server/state_cipher.h"

namespace q4t::server {
struct ResponsesMcpBinding;
class ResponsesMcpState;
inline constexpr char kResponsesProgramModelName[] = "q4t_builtin_program";
// Internal dispatch intent only; never an SDK item or a public output type.
inline constexpr char kResponsesProgramMcpIntent[] = "q4t_program_mcp_intent";

// Omitted/null is direct-only. Validation and authorization are distinct:
// invalid declarations never grant access, including in historical catalogs.
Status ValidateResponsesAllowedCallers(const io::Json& definition,
                                       ChatPlanError* error,
                                       const std::string& path);
bool ResponsesToolAllows(const io::Json& definition, std::string_view caller);
std::string ResponsesMcpProgramNamespace(std::string_view server_label);
std::string ResponsesMcpProgramCallId(std::string_view program_call_id,
                                      size_t index,
                                      std::string_view result_context = {});
io::Json NativeResponsesProgramDefinition(
    const io::Json& active_tools, bool parallel = true,
    const std::vector<ResponsesMcpBinding>* mcp = nullptr);
Status MapResponsesProgramChoice(const io::Json& choice, bool enabled,
                                 io::Json* mapped, ChatPlanError* error,
                                 bool python = false);
// The SDK result remains a string. Only the explicit versioned representation
// carries typed media; ordinary text, including JSON-looking text, stays text.
Status DecodeResponsesProgramOutput(std::string_view result, io::Json* content);
Status EncodeResponsesProgramOutput(const io::Json& content,
                                    std::string* result);

// Private request-owned state. Construct only through authenticated opening;
// never accept these fields from a public request or stored arbitrary metadata.
struct ResponsesProgramState {
  io::Json program;
  io::Json tools;
  io::Json worker_input;
  io::Json terminal;
  std::vector<io::Json> calls;
  // Entry n binds the first n+1 results actually delivered to the VM.
  std::vector<std::string> result_contexts;
  bool legacy_child_ids = false;
  size_t input_index = 0;
};

// active_tools is the validated, ordered catalog at invocation, after deferred
// discovery. The sealed snapshot remains authoritative if later tools change.
ObjectReply CreateResponsesProgram(
    StateCipher& cipher, std::string code, const io::Json& active_tools,
    const ChatPlanConfig& config, const StateAccess& access, io::Json* program,
    bool parallel = true,
    const std::vector<ResponsesMcpBinding>* mcp = nullptr);
// Opens code/authority and collects exactly linked child calls and ordered
// result events. This does not execute JavaScript or perform external tools.
ObjectReply OpenResponsesPrograms(StateCipher& cipher, const io::Json& history,
                                  const ChatPlanConfig& config,
                                  const StateAccess& access,
                                  std::vector<ResponsesProgramState>* programs,
                                  ResponsesMcpState* mcp = nullptr);
// Replays only local computation. Emits only NEW client dispatches or the
// terminal result; previously returned calls never become external work again.
ObjectReply AdvanceResponsesProgram(const ResponsesProgramState& program,
                                    const StateAccess& access,
                                    io::Json* emitted);
ObjectReply BindResponsesProgramContracts(const ResponsesProgramState& program,
                                          const io::Json& items,
                                          io::Json* contracts);
}  // namespace q4t::server
