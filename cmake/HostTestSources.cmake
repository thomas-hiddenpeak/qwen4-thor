# Shared by the Thor release gate and the standalone public host build.
set(Q4T_HOST_TEST_SOURCES
  ${CMAKE_CURRENT_LIST_DIR}/../src/trace/sha256.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/router_trace_hash_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/router_trace_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/trace/router_trace.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/test_main.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/server_options_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/server_mtp_policy_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/server_scheduler_submission_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/server/server_options.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/io_json_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/model_sequence_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/server_http_request_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/server_chat_contract_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/server/http_request.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/server/chat_contract.cpp)
