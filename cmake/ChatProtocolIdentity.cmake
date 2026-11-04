# Embed source identity without Git state, runtime probes, or model access.
include_guard(GLOBAL)
include(${CMAKE_CURRENT_LIST_DIR}/StructuredOutput.cmake)
get_filename_component(Q4T_PROTOCOL_ROOT "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
set(protocol_inputs
  include/q4t/server/client_identity.h
  include/q4t/server/client_policy.h
  include/q4t/server/client_request.h
  src/server/client_policy.cpp
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
  src/server/grammar_worker.cpp
  src/server/grammar_worker_internal.h
  include/q4t/server/abort_reason.h
  include/q4t/server/structured_admission.h
  include/q4t/server/chat_capabilities.h
  include/q4t/server/input_contract.h
  include/q4t/server/chat_contract.h
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
  include/q4t/server/http_connection.h
  include/q4t/server/http_listener.h
  include/q4t/server/http_observation.h
  include/q4t/server/request_control.h
  include/q4t/server/request_diagnostics.h
  include/q4t/server/chat_server.h
  include/q4t/server/request_json.h
  include/q4t/server/output_schema.h
  include/q4t/server/output_constraints.h
  include/q4t/server/structured_decoder.h
  cmake/StructuredOutput.cmake
  src/server/chat_capabilities.cpp
  src/server/input_contract.cpp
  src/server/chat_contract.cpp
  src/server/chat_plan.cpp
  src/server/chat_prompt.cpp
  src/server/chat_preflight.cpp
  src/server/server_options.cpp
  src/server/chat_generation.cpp
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
