# Embed source identity without Git state, runtime probes, or model access.
include_guard(GLOBAL)
include(${CMAKE_CURRENT_LIST_DIR}/StructuredOutput.cmake)
get_filename_component(Q4T_PROTOCOL_ROOT "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
set(protocol_inputs
  include/q4t/server/chat_capabilities.h
  include/q4t/server/chat_contract.h
  include/q4t/server/chat_response_parser.h
  include/q4t/server/chat_template.h
  include/q4t/server/chat_tool_history.h
  include/q4t/server/http_response.h
  include/q4t/server/request_json.h
  include/q4t/server/output_schema.h
  include/q4t/server/output_constraints.h
  include/q4t/server/structured_decoder.h
  cmake/StructuredOutput.cmake
  src/server/chat_capabilities.cpp
  src/server/chat_contract.cpp
  src/server/chat_generation.cpp
  src/server/chat_http.cpp
  src/server/chat_input.cpp
  src/server/chat_metrics.cpp
  src/server/chat_response_parser.cpp
  src/server/chat_template.cpp
  src/server/chat_tool_history.cpp
  src/server/http_response.cpp
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
