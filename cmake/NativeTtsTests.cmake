# Metadata qualification has no CUDA or model-weight dependency.
set(tts_test_root "${CMAKE_CURRENT_LIST_DIR}/..")
add_executable(q4t_tts_contract_tests EXCLUDE_FROM_ALL
  ${tts_test_root}/tests/test_main.cpp
  ${tts_test_root}/tests/tts_config_test.cpp
  ${tts_test_root}/src/audio/tts_config.cpp
  ${tts_test_root}/src/io/json.cpp)
target_include_directories(q4t_tts_contract_tests PRIVATE
  ${tts_test_root}/include)
target_compile_options(q4t_tts_contract_tests PRIVATE -Wall -Wextra -Werror)
