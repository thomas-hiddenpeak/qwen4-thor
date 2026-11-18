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
  ${tts_test_root}/tests/tts_base_config_test.cpp
  ${tts_test_root}/tests/tts_speaker_features_test.cpp
  ${tts_test_root}/src/audio/tts_speaker_features.cpp
  ${tts_test_root}/tests/tts_speaker_layout_test.cpp
  ${tts_test_root}/src/audio/tts_speaker_layout.cpp
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
    ${tts_test_root}/tests/tts_base_generation_test.cpp
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

# Real encoding uses existing exec isolation; never links codecs into the core.
include(${CMAKE_CURRENT_LIST_DIR}/NativeVideo.cmake)
add_executable(q4t_speech_encode_tests EXCLUDE_FROM_ALL
  ${tts_test_root}/tests/speech_encode_test_main.cpp
  ${tts_test_root}/tools/native_tts/encode_replay.cpp
  ${tts_test_root}/tests/server_speech_encode_test.cpp
  ${tts_test_root}/tests/sha256_stream_test.cpp
  ${tts_test_root}/tests/router_trace_hash_test.cpp
  ${tts_test_root}/src/server/speech_encode.cpp
  ${tts_test_root}/src/server/speech_encode_worker.cpp
  ${tts_test_root}/src/server/speech_codec_stream.cpp
  ${tts_test_root}/src/server/speech_output.cpp
  ${tts_test_root}/tests/server_speech_output_test.cpp
  ${tts_test_root}/tests/pcm_output_test.cpp
  ${tts_test_root}/src/audio/speech_tempo.cpp
  ${tts_test_root}/src/server/speech_codec_stream_worker.cpp
  ${tts_test_root}/tests/server_speech_codec_stream_test.cpp
  ${tts_test_root}/src/server/speech_request.cpp
  ${tts_test_root}/src/audio/pcm_output.cpp
  ${tts_test_root}/src/server/file_audio.cpp
  ${tts_test_root}/src/server/file_audio_worker.cpp
  ${tts_test_root}/src/server/file_video_sandbox.cpp
  ${tts_test_root}/src/server/grammar_process.cpp
  ${tts_test_root}/src/server/worker_io_broker.cpp
  ${tts_test_root}/src/server/output_schema.cpp
  ${tts_test_root}/src/server/http_response.cpp
  ${tts_test_root}/src/io/json.cpp
  ${tts_test_root}/src/trace/sha256.cpp)
target_compile_definitions(q4t_speech_encode_tests PRIVATE Q4T_GRAMMAR_PROCESS_TESTS=1)
target_include_directories(q4t_speech_encode_tests PRIVATE ${tts_test_root}/include)
q4t_configure_native_video(q4t_speech_encode_tests)
target_link_libraries(q4t_speech_encode_tests PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
target_compile_options(q4t_speech_encode_tests PRIVATE
  -Wall -Wextra -Werror -ffunction-sections -fdata-sections)
target_link_options(q4t_speech_encode_tests PRIVATE -Wl,--gc-sections)
