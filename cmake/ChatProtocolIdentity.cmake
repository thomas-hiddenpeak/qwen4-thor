# Embed source identity without Git state, runtime probes, or model access.
include_guard(GLOBAL)
include(${CMAKE_CURRENT_LIST_DIR}/StructuredOutput.cmake)
get_filename_component(Q4T_PROTOCOL_ROOT "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
set(protocol_inputs
  include/q4t/text/tokenizer.h
  src/text/tokenizer.cpp
  include/q4t/server/model_catalog.h
  src/server/model_catalog.cpp
  include/q4t/server/completion_plan.h
  src/server/completion_plan.cpp
  include/q4t/server/completion_response.h
  src/server/completion_response.cpp
  include/q4t/server/responses_output.h
  src/server/responses_output.cpp
  include/q4t/server/responses_input.h
  src/server/responses_input.cpp
  include/q4t/server/responses_plan.h
  src/server/responses_plan.cpp
  src/server/responses_summary.cpp
  cmake/NativeCrypto.cmake
  include/q4t/server/native_crypto.h
  src/server/native_crypto.cpp
  include/q4t/server/state_cipher.h
  src/server/state_cipher.cpp
  src/server/responses_json.h
  include/q4t/model/sequence.h
  include/q4t/runtime/model_health.h
  include/q4t/server/service_health.h
  include/q4t/ple/page_reader.h
  include/q4t/ple/ple_embedding.h
  src/ple/page_reader.cpp
  src/ple/ple_embedding.cpp
  src/server/chat_health.cpp
  include/q4t/model/vision_positions.h
  include/q4t/vision/configuration.h
  include/q4t/server/client_identity.h
  include/q4t/server/client_policy.h
  include/q4t/server/client_policy_admin.h
  include/q4t/server/client_rate_state.h
  include/q4t/server/client_request.h
  src/server/client_policy.cpp
  src/server/client_policy_admin.cpp
  src/server/client_rate_state.cpp
  src/server/client_request.cpp
  include/q4t/runtime/device_memory.h
  include/q4t/runtime/allocation_quota.h
  include/q4t/runtime/cuda_allocation_quota.h
  src/runtime/cuda_allocation_quota.cpp
  src/runtime/cuda_quota_backend.h
  include/q4t/server/cuda_quota_policy.h
  include/q4t/server/cuda_quota_metrics.h
  include/q4t/vision/vision.h
  include/q4t/vision/processor.h
  include/q4t/vision/timing.h
  include/q4t/vision/forward_timing.h
  include/q4t/vision/forward_timing_core.h
  include/q4t/vision/media_plan.h
  src/vision/media_plan.cpp
  include/q4t/server/media_admission.h
  src/server/media_admission.cpp
  src/vision/vision.cu
  src/vision/processor.cpp
  cmake/CudaCleanupBoundary.cmake
  include/q4t/runtime/cleanup_policy.h
  src/runtime/startup_reservation.cpp
  src/model/model_owner.cpp
  src/model/model.cu
  src/mtp/mtp.cu
  include/q4t/server/device_memory.h
  src/server/cuda_memory_backend.h
  src/server/chat_device_memory.cpp
  include/q4t/runtime/system_memory.h
  include/q4t/server/startup_admission.h
  src/runtime/system_memory.cpp
  include/q4t/server/memory_admission.h
  src/server/memory_admission.cpp
  src/server/chat_memory.cpp
  src/main.cpp
  include/q4t/server/grammar_process.h
  src/server/grammar_process.cpp
  include/q4t/server/cache_io.h
  include/q4t/server/cache_health.h
  src/server/cache_io.cpp
  src/server/cache_protocol.h
  src/server/cache_worker.cpp
  include/q4t/server/session_cache.h
  src/server/session_cache.cpp
  include/q4t/server/object_store.h
  src/server/object_store.cpp
  src/server/object_worker.cpp
  src/server/object_protocol.h
  include/q4t/server/responses_state.h
  src/server/responses_state.cpp
  include/q4t/server/responses_execution.h
  src/server/responses_execution.cpp
  include/q4t/server/responses_api.h
  src/server/responses_api.cpp
  include/q4t/server/responses_owner.h
  src/server/responses_owner.cpp
  include/q4t/service/guardian_process.h
  src/service/guardian_process.cpp
  include/q4t/server/application_query.h
  src/server/application_query.cpp
  include/q4t/server/conversation_state.h
  src/server/conversation_state.cpp
  include/q4t/server/conversation_api.h
  include/q4t/server/application_routes.h
  include/q4t/server/object_store_options.h
  src/server/object_store_options.cpp
  src/server/conversation_api.cpp
  include/q4t/server/chat_storage.h
  src/server/chat_storage.cpp
  src/server/chat_storage_options.cpp
  include/q4t/server/responses_item_shape.h
  src/server/responses_item_shape.cpp
  src/server/contracts/openai_item_types_v1.json
  cmake/responses_item_types.h.in
  src/server/grammar_worker.cpp
  src/server/grammar_worker_internal.h
  include/q4t/server/abort_reason.h
  include/q4t/server/structured_admission.h
  include/q4t/server/chat_capabilities.h
  include/q4t/server/input_contract.h
  include/q4t/server/chat_contract.h
  include/q4t/server/sampling.h
  include/q4t/server/prediction.h
  include/q4t/server/custom_tools.h
  include/q4t/server/custom_grammar.h
  include/q4t/server/legacy_tools.h
  include/q4t/server/token_logprobs.h
  include/q4t/server/chat_choices.h
  include/q4t/server/chat_plan.h
  include/q4t/server/chat_prompt.h
  include/q4t/server/server_options.h
  include/q4t/server/sequence_admission.h
  include/q4t/server/request_admission.h
  include/q4t/runtime/sequence_capacity.h
  include/q4t/server/thinking_budget.h
  include/q4t/server/text_stop.h
  include/q4t/server/reasoning_options.h
  include/q4t/server/chat_response_parser.h
  include/q4t/server/chat_template.h
  include/q4t/server/chat_tool_history.h
  include/q4t/server/http_response.h
  include/q4t/server/http_request.h
  include/q4t/server/http_framing.h
  include/q4t/server/http_connection.h
  include/q4t/server/http_listener.h
  include/q4t/server/http_observation.h
  include/q4t/server/request_control.h
  include/q4t/server/request_diagnostics.h
  include/q4t/server/request_progress.h
  include/q4t/server/progress_context.h
  include/q4t/runtime/execution_progress.h
  include/q4t/server/progress_transport.h
  src/server/progress_transport.cpp
  include/q4t/server/chat_server.h
  include/q4t/server/request_json.h
  include/q4t/server/output_schema.h
  include/q4t/server/output_constraints.h
  include/q4t/server/structured_decoder.h
  cmake/StructuredOutput.cmake
  cmake/RustRegex.cmake
  cmake/regex-syntax-0.8.8.sha256
  src/server/regex_syntax/lib.rs
  src/server/regex_syntax/schema_regex.rs
  src/server/chat_capabilities.cpp
  src/server/input_contract.cpp
  src/server/chat_contract.cpp
  src/server/sampling.cpp
  src/server/prediction.cpp
  src/server/custom_tools.cpp
  src/server/custom_grammar.cpp
  src/server/custom_regex.cpp
  src/server/custom_regex.h
  src/server/legacy_tools.cpp
  src/server/token_logprobs.cpp
  src/server/chat_choices.cpp
  src/server/chat_plan.cpp
  src/server/chat_prompt.cpp
  src/server/chat_preflight.cpp
  src/server/server_options.cpp
  src/server/chat_generation.cpp
  src/server/chat_responses.cpp
  src/server/chat_http.cpp
  src/server/chat_input.cpp
  src/server/chat_metrics.cpp
  src/server/thinking_budget.cpp
  src/server/text_stop.cpp
  src/server/reasoning_options.cpp
  src/server/chat_response_parser.cpp
  src/server/chat_template.cpp
  src/server/chat_tool_history.cpp
  src/server/http_response.cpp
  src/server/http_request.cpp
  src/server/chat_server.cpp
  src/server/chat_scheduler.cpp
  src/server/chat_admission.cpp
  src/server/output_schema.cpp
  include/q4t/io/json.h
  src/io/json.cpp
  include/q4t/server/schema_decimal.h
  include/q4t/server/schema_graph.h
  include/q4t/server/schema_prefix.h
  include/q4t/server/schema_receipt.h
  src/server/schema_decimal.cpp
  src/server/schema_graph.cpp
  src/server/schema_prefix.cpp
  src/server/schema_receipt.cpp
  src/server/schema_token_guard.h
  src/server/schema_token_guard.cpp
  src/server/schema_number_grammar.h
  src/server/schema_number_grammar.cpp
  src/server/schema_grammar.h
  src/server/schema_grammar.cpp
  src/server/schema_string.h
  src/server/schema_string.cpp
  src/server/schema_string_grammar.h
  src/server/schema_string_grammar.cpp
  src/server/schema_idna.h
  src/server/schema_idna.cpp
  src/server/output_constraints.cpp
  src/server/structured_decoder.cpp)
list(SORT protocol_inputs)
set(protocol_manifest "")
set(protocol_sources "{}")
foreach(path IN LISTS protocol_inputs)
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${Q4T_PROTOCOL_ROOT}/${path}")
  file(SHA256 "${Q4T_PROTOCOL_ROOT}/${path}" hash)
  string(APPEND protocol_manifest "${path}:${hash}\n")
  string(JSON protocol_sources SET "${protocol_sources}" "${path}" "\"${hash}\"")
endforeach()
string(SHA256 Q4T_PROTOCOL_SHA256 "${protocol_manifest}")
file(SHA256 "${Q4T_PROTOCOL_ROOT}/src/server/chat_template.cpp"
  Q4T_RENDERER_SHA256)
file(SHA256 "${Q4T_PROTOCOL_ROOT}/src/server/chat_tool_history.cpp"
  Q4T_TOOL_HISTORY_SHA256)
set(fixture "${Q4T_PROTOCOL_ROOT}/tools/evalscope/fixtures/chat_template_cases")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
  "${fixture}.jsonl" "${fixture}.metadata.json")
file(READ "${fixture}.metadata.json" metadata)
string(JSON Q4T_TEMPLATE_REFERENCE_SHA256 GET "${metadata}" template_sha256)
file(SHA256 "${fixture}.jsonl" Q4T_TEMPLATE_FIXTURE_SHA256)
set(Q4T_CHAT_PROTOCOL_INCLUDE_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated")
configure_file("${CMAKE_CURRENT_LIST_DIR}/chat_protocol_identity.h.in"
  "${Q4T_CHAT_PROTOCOL_INCLUDE_DIR}/q4t/server/chat_protocol_identity.h" @ONLY)
file(CONFIGURE
  OUTPUT "${Q4T_CHAT_PROTOCOL_INCLUDE_DIR}/q4t/server/chat_protocol_sources.json"
  CONTENT "${protocol_sources}\n" @ONLY)
file(READ "${Q4T_PROTOCOL_ROOT}/src/server/contracts/openai_item_types_v1.json"
  Q4T_ITEM_TYPES)
string(FIND "${Q4T_ITEM_TYPES}" ")q4t_types\"" item_delimiter)
if(NOT item_delimiter EQUAL -1)
  message(FATAL_ERROR "item type graph contains its C++ literal delimiter")
endif()
configure_file("${CMAKE_CURRENT_LIST_DIR}/responses_item_types.h.in"
  "${Q4T_CHAT_PROTOCOL_INCLUDE_DIR}/q4t/server/responses_item_types_data.h" @ONLY)
