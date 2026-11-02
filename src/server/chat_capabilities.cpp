#include "q4t/server/chat_capabilities.h"

#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/structured_decoder.h"

namespace q4t::server {
namespace {
std::string Quote(const std::string& value) {
  return '"' + detail::JsonEscape(value) + '"';
}
}  // namespace

std::string ChatCapabilitiesJson(const std::string& model, bool allow_media,
                                 bool mtp_enabled) {
  const bool constrained = kStructuredDecodingAvailable;
  const std::string options =
      constrained ? R"("tool_choice":["auto","none","required","named"],)"
                    R"("parallel_tool_calls":[true,false],"strict":true,)"
                    R"("constrained_decoding":true,)"
                  : R"("tool_choice":["auto"],"parallel_tool_calls":[true],)"
                    R"("strict":false,"constrained_decoding":false,)";
  return R"({"object":"q4t.capabilities","schema_version":1,)"
         R"("implementation_only":true,"release_attestation":false,"model":)" +
         Quote(model) + R"(,"protocol_source_sha256":)" +
         Quote(identity::kProtocol) +
         R"(,"chat":{"messages":true,"raw_prompt":true,)"
         R"("messages_parse_response_default":true,)"
         R"("parse_response_false":true,"raw_prompt_parsing":false,)"
         R"("content":true,"reasoning_content":true,"tool_calls":true,)"
         R"("stream":true,"stream_include_usage":true,)"
         R"("stream_usage_null":true,"choice_logprobs_null":true,)"
         R"("stream_heartbeat_ms":5000,"heartbeat_write_timeout_ms":1000,)"
         R"("stream_start":"after_template_and_structured_admission",)"
         R"("post_header_failure":"sse_error_then_done",)"
         R"("token_limit_fields":["max_tokens","max_completion_tokens"],)"
         R"("token_limit_conflict":"reject_both_nonnull",)"
         R"("input_contract":"q4t-input-v2","exact_integer_spelling":true,)"
         R"("null_as_default":["temperature","top_p","n",)"
         R"("frequency_penalty","presence_penalty","stream",)"
         R"("stream_options","user"],)"
         R"("reasoning_field":"reasoning_content",)"
         R"("stream_tool_arguments":"complete_call",)"
         R"("stream_terminal_order":["finish","usage_if_requested","DONE"],)"
         R"("parse_error":{"http_status":502,"sse_error_then_done":true,)"
         R"("successful_finish":false},"sampling":"greedy","choices":1},)"
         R"("tools":{"type":"function","execution":"client",)" +
         options +
         R"("options_require_parsed_messages":true,)"
         R"("definition_unknown_fields":"reject_all_modes",)"
         R"("strict_null":"false",)"
         R"("full_json_schema_validation":false,)"
         R"("max_calls":128,"max_id_bytes":256,)"
         R"("history":{"identified_results":"complete_unique_batch",)"
         R"("unordered_text_results":true,"unordered_media_results":false,)"
         R"("legacy_without_ids":true,"legacy_shape_validation":true,)"
         R"("arguments":"required_object_or_json_object_text",)"
         R"("result_content":"required_string_or_array"}},)"
         R"("structured_output":{"available":)" +
         (constrained ? "true" : "false") +
         R"(,"backend":"xgrammar","backend_revision":)" +
         Quote(kXGrammarRevision) + R"(,"schema_profile":)" +
         Quote(kOutputSchemaProfile) + R"(,"response_format":)" +
         (constrained ? R"(["text","json_object","json_schema"])"
                      : R"(["text"])") +
         R"(,"validation":"complete_response_before_publication",)"
         R"("stream_payload":"buffered_until_validated",)"
         R"("decode":"plain_host_masked_greedy","mtp":false,)"
         R"("packed_decode":false,"session_cache":false,)"
         R"("prefix_cache":false,"media":false,"length_terminal":"error",)"
         R"("named_tool_max_calls":1,"max_tools":16,"max_response_bytes":1048576,)"
         R"("max_schema_bytes":16384,"max_schema_depth":8,)"
         R"("max_schema_nodes":128,"object_key_order":"schema",)"
         R"("json_with_tools":"requires_tool_choice_none",)"
         R"("resources":{"active_limit":1,"overload_status":503,)"
         R"("overload_code":"structured_capacity_exhausted",)"
         R"("lease":"preparation_through_response_cleanup",)"
         R"("compiler_threads":1,"compiler_cache":false,)"
         R"("decoded_vocab_storage_bytes":33554432,)"
         R"("compiled_grammar_bytes":67108864,"compile_peak_rss_limit":false,)"
         R"("isolation":"exec_worker_compile_and_match",)"
         R"("worker_address_space_bytes":2147483648,)"
         R"("worker_memory_enforcement":"RLIMIT_AS",)"
         R"("whole_server_ram_limit":false,"rss_measurement":"reported_exec_VmHWM",)"
         R"("lifetime_ru_maxrss_includes_pre_exec":true,)"
         R"("compile_timeout_ms":10000,"match_timeout_ms":2000,)"
         R"("compile_interruptible":true,"shutdown":"kill_and_reap_owned_worker",)"
         R"("compile_timeout_code":"constraint_compile_timeout",)"
         R"("memory_limit_code":"constraint_resource_exhausted",)"
         R"("worker_failure_code":"constraint_worker_failed"}},)"
         R"("observability":{"version":2,"full_response":"q4t_response_seconds",)"
         R"("slot_wait":"q4t_slot_wait_seconds",)"
         R"("admission":"q4t_admission_seconds",)"
         R"("generation_exit":"q4t_generation_complete_seconds",)"
         R"("legacy_queue_e2e":"deprecated_aliases",)"
         R"("cancellation":"fixed_reason_counters"},"template":{)"
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
         R"(},"http":{"request_id_header":"X-Request-Id",)"
         R"("trace_id_independent_of_completion":true,"random_id_bits":128,)"
         R"("trace_id_max_bytes":128,"json_content_type_required":true,)"
         R"("charset":"utf-8","wrong_method_status":405,)"
         R"("query_parameters":"reject","connection_reuse":false,)"
         R"("liveness":"/livez","readiness":"/readyz",)"
         R"("control_reserved_connections":4,"drain_control_window_ms":2000,)"
         R"("reservation_excludes_unclassified_header_flood":true},)"
         R"("cancellation":{"endpoint":"/v1/requests/cancel",)"
         R"("accepted_status":202,"acceptance_is_drained":false,)"
         R"("unknown_fields":"reject","malformed_status":400,)"
         R"("unknown_credentials_status":404,)"
         R"("request_replay_idempotent":false},)"
         R"("errors":{"envelope":["message","type","code","param"],)"
         R"("code_namespace":"q4t","automatic_retry_safe":false,)"
         R"("sse_error_then_done":true},)"
         R"("unsupported":)" +
         (constrained
              ? R"(["sampling","full_json_schema"])"
              : R"(["tool_choice_none","tool_choice_required","tool_choice_named",)"
                R"("parallel_tool_calls_false","strict_tools",)"
                R"("json_schema_response","sampling"])") +
         '}';
}

}  // namespace q4t::server
