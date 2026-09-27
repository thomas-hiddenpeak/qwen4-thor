# Shared by the Thor release gate and the standalone public host build.
set(Q4T_HOST_TEST_SOURCES
  ${CMAKE_CURRENT_LIST_DIR}/../tests/test_main.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/io_json_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/server_http_request_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../tests/server_chat_contract_test.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/server/http_request.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/server/chat_contract.cpp)
