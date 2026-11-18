# Metadata qualification has no CUDA or model-weight dependency.
set(tts_test_root "${CMAKE_CURRENT_LIST_DIR}/..")
add_executable(q4t_speech_contract_tests EXCLUDE_FROM_ALL
  ${tts_test_root}/tests/test_main.cpp
  ${tts_test_root}/tests/server_speech_request_test.cpp
  ${tts_test_root}/tests/pcm_output_test.cpp
  ${tts_test_root}/tests/speech_tempo_test.cpp
  ${tts_test_root}/src/server/speech_request.cpp
  ${tts_test_root}/src/audio/pcm_output.cpp
  ${tts_test_root}/src/audio/speech_tempo.cpp
  ${tts_test_root}/src/io/json.cpp)
target_include_directories(q4t_speech_contract_tests PRIVATE
  ${tts_test_root}/include)
target_compile_options(q4t_speech_contract_tests PRIVATE -Wall -Wextra -Werror)
add_executable(q4t_tts_contract_tests EXCLUDE_FROM_ALL
  ${tts_test_root}/tests/test_main.cpp
  ${tts_test_root}/tests/tts_config_test.cpp
  ${tts_test_root}/src/audio/tts_config.cpp
  ${tts_test_root}/src/io/json.cpp)
target_include_directories(q4t_tts_contract_tests PRIVATE
  ${tts_test_root}/include)
target_compile_options(q4t_tts_contract_tests PRIVATE -Wall -Wextra -Werror)

# Actual tokenizer assets are supplied explicitly for local qualification.
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(TTS_ICU QUIET icu-uc icu-i18n)
endif()
if(TTS_ICU_FOUND)
  add_executable(q4t_tts_execution_tests EXCLUDE_FROM_ALL
    ${tts_test_root}/tests/test_main.cpp
    ${tts_test_root}/tests/tts_generation_test.cpp
    ${tts_test_root}/tests/tts_tokenizer_test.cpp
    ${tts_test_root}/tests/asr_tokenizer_test.cpp
    ${tts_test_root}/src/audio/tts_config.cpp
    ${tts_test_root}/src/audio/tts_layout.cpp
    ${tts_test_root}/src/audio/tts_codec_layout.cpp
    ${tts_test_root}/tests/tts_codec_layout_test.cpp
    ${tts_test_root}/src/audio/tts_generation.cpp
    ${tts_test_root}/src/text/tokenizer.cpp
    ${tts_test_root}/src/io/json.cpp)
  target_include_directories(q4t_tts_execution_tests PRIVATE
    ${tts_test_root}/include ${TTS_ICU_INCLUDE_DIRS})
  target_link_libraries(q4t_tts_execution_tests PRIVATE ${TTS_ICU_LINK_LIBRARIES})
  target_compile_options(q4t_tts_execution_tests PRIVATE -Wall -Wextra -Werror)
endif()
