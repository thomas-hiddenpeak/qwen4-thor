include_guard(GLOBAL)
set(separator_test_root "${CMAKE_CURRENT_LIST_DIR}/..")
add_executable(q4t_separator_contract_tests EXCLUDE_FROM_ALL
  ${separator_test_root}/tests/test_main.cpp
  ${separator_test_root}/tests/separator_config_test.cpp
  ${separator_test_root}/tests/separator_asset_test.cpp
  ${separator_test_root}/src/audio/separator_config.cpp
  ${separator_test_root}/src/io/json.cpp
  ${separator_test_root}/src/trace/sha256.cpp)
target_include_directories(q4t_separator_contract_tests PRIVATE
  ${separator_test_root}/include)
target_compile_options(q4t_separator_contract_tests PRIVATE
  -Wall -Wextra -Werror)
