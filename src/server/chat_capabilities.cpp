#include "q4t/server/chat_capabilities.h"

#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
std::string Quote(const std::string& value) {
  return '"' + detail::JsonEscape(value) + '"';
}
}  // namespace

std::string ChatCapabilitiesJson(const std::string& model, bool allow_media,
                                 bool mtp_enabled) {
  return R"({"object":"q4t.capabilities","schema_version":1,)"
         R"("implementation_only":true,"release_attestation":false,"model":)" +
         Quote(model) + R"(,"protocol_source_sha256":)" +
         Quote(identity::kProtocol) +
         R"(,"chat":{"messages":true,"raw_prompt":true,)"
         R"("messages_parse_response_default":true,)"
         R"("parse_response_false":true,"raw_prompt_parsing":false,)"
         R"("content":true,"reasoning_content":true,"tool_calls":true,)"
         R"("stream":true,"stream_include_usage":true,)"
         R"("stream_tool_arguments":"complete_call",)"
         R"("stream_terminal_order":["finish","usage_if_requested","DONE"],)"
         R"("parse_error":{"http_status":502,"sse_error_then_done":true,)"
         R"("successful_finish":false},"sampling":"greedy","choices":1},)"
         R"("tools":{"type":"function","execution":"client",)"
         R"("tool_choice":["auto"],"parallel_tool_calls":[true],)"
         R"("options_require_parsed_messages":true,"strict":false,)"
         R"("constrained_decoding":false,"full_json_schema_validation":false,)"
         R"("max_calls":128,"max_id_bytes":256,)"
         R"("history":{"identified_results":"complete_unique_batch",)"
         R"("unordered_text_results":true,"unordered_media_results":false,)"
         R"("legacy_without_ids":true}},"template":{)"
         R"("id":"qwen4_exp-native-v1","reference_sha256":)" +
         Quote(identity::kTemplateReference) + R"(,"renderer_source_sha256":)" +
         Quote(identity::kRenderer) + R"(,"tool_history_source_sha256":)" +
         Quote(identity::kToolHistory) + R"(,"fixture_sha256":)" +
         Quote(identity::kTemplateFixture) +
         R"(,"options":["enable_thinking","preserve_thinking",)"
         R"("reasoning_effort","add_vision_id"],)"
         R"("reasoning_effort":["low","medium","xhigh"]},)"
         R"("runtime":{"media_enabled":)" +
         (allow_media ? "true" : "false") + R"(,"mtp_enabled":)" +
         (mtp_enabled ? "true" : "false") +
         R"(},"cancellation":{"endpoint":"/v1/requests/cancel",)"
         R"("accepted_status":202,"acceptance_is_drained":false,)"
         R"("request_replay_idempotent":false},)"
         R"("unsupported":["tool_choice_none","tool_choice_required",)"
         R"("tool_choice_named","parallel_tool_calls_false",)"
         R"("strict_tools","json_schema_response","sampling"]})";
}

}  // namespace q4t::server
