include_guard(GLOBAL)
set(speaker_test_root "${CMAKE_CURRENT_LIST_DIR}/..")
add_executable(q4t_speaker_contract_tests EXCLUDE_FROM_ALL
  ${speaker_test_root}/tests/test_main.cpp
  ${speaker_test_root}/tests/speaker_config_test.cpp
  ${speaker_test_root}/tests/speaker_asset_test.cpp
  ${speaker_test_root}/src/audio/speaker_config.cpp
  ${speaker_test_root}/src/io/json.cpp
  ${speaker_test_root}/src/trace/sha256.cpp)
target_include_directories(q4t_speaker_contract_tests PRIVATE
  ${speaker_test_root}/include)
target_compile_options(q4t_speaker_contract_tests PRIVATE
  -Wall -Wextra -Werror)
