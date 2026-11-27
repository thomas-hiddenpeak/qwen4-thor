# Optional local qualification with real pinned metadata, never public CI.
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(MODEL_ASSET_ICU QUIET IMPORTED_TARGET icu-uc icu-i18n)
endif()
if(MODEL_ASSET_ICU_FOUND)
  set(model_asset_root "${CMAKE_CURRENT_LIST_DIR}/..")
  add_executable(q4t_model_asset_tests EXCLUDE_FROM_ALL
    ${model_asset_root}/tests/model_asset_test_main.cpp
    ${model_asset_root}/tests/server_model_assets_test.cpp
    ${model_asset_root}/tests/server_tts_assets_test.cpp
    ${model_asset_root}/tests/server_tts_service_test.cpp
    ${model_asset_root}/src/server/tts_assets.cpp
    ${model_asset_root}/src/server/tts_service.cpp
    ${model_asset_root}/src/server/client_policy.cpp
    ${model_asset_root}/src/server/client_rate_state.cpp
    ${model_asset_root}/src/server/output_schema.cpp
    ${model_asset_root}/src/server/chat_audio_output.cpp
    ${model_asset_root}/src/server/speech_output.cpp
    ${model_asset_root}/src/runtime/memory_budget.cpp
    ${model_asset_root}/src/audio/tts_config.cpp
    ${model_asset_root}/src/audio/tts_speaker_layout.cpp
    ${model_asset_root}/src/audio/tts_speaker_features.cpp
    ${model_asset_root}/src/audio/tts_layout.cpp
    ${model_asset_root}/src/audio/tts_codec_layout.cpp
    ${model_asset_root}/src/audio/tts_encoder_layout.cpp
    ${model_asset_root}/src/audio/tts_generation.cpp
    ${model_asset_root}/tests/server_aligner_assets_test.cpp
    ${model_asset_root}/tests/server_speaker_assets_test.cpp
    ${model_asset_root}/tests/server_separator_assets_test.cpp
    ${model_asset_root}/tests/server_separator_service_test.cpp
    ${model_asset_root}/tests/separator_layout_test.cpp
    ${model_asset_root}/src/server/separator_assets.cpp
    ${model_asset_root}/src/server/separator_service.cpp
    ${model_asset_root}/src/audio/separator_config.cpp
    ${model_asset_root}/src/audio/separator_layout.cpp
    ${model_asset_root}/tests/server_speaker_service_test.cpp
    ${model_asset_root}/src/server/speaker_assets.cpp
    ${model_asset_root}/src/server/speaker_service.cpp
    ${model_asset_root}/src/server/asr_service.cpp
    ${model_asset_root}/src/audio/speaker_config.cpp
    ${model_asset_root}/src/audio/speaker_layout.cpp
    ${model_asset_root}/tests/server_vad_assets_test.cpp
    ${model_asset_root}/src/server/vad_assets.cpp
    ${model_asset_root}/src/audio/vad.cpp
    ${model_asset_root}/tests/server_embedding_assets_test.cpp
    ${model_asset_root}/tests/router_trace_hash_test.cpp
    ${model_asset_root}/src/server/model_assets.cpp
    ${model_asset_root}/src/server/asr_assets.cpp
    ${model_asset_root}/src/server/aligner_assets.cpp
    ${model_asset_root}/src/server/embedding_assets.cpp
    ${model_asset_root}/src/server/grammar_process.cpp
    ${model_asset_root}/src/server/worker_io_broker.cpp
    ${model_asset_root}/src/audio/asr_config.cpp
    ${model_asset_root}/src/audio/asr_layout.cpp
    ${model_asset_root}/src/audio/aligner_config.cpp
    ${model_asset_root}/src/audio/aligner_layout.cpp
    ${model_asset_root}/src/model/embedding_config.cpp
    ${model_asset_root}/src/model/embedding_layout.cpp
    ${model_asset_root}/src/text/tokenizer.cpp
    ${model_asset_root}/src/io/json.cpp
    ${model_asset_root}/src/trace/sha256.cpp)
  target_compile_definitions(q4t_model_asset_tests PRIVATE
    Q4T_GRAMMAR_PROCESS_TESTS=1)
  target_include_directories(q4t_model_asset_tests PRIVATE
    ${model_asset_root}/include)
  target_link_libraries(q4t_model_asset_tests PRIVATE
    Threads::Threads PkgConfig::MODEL_ASSET_ICU)
  # Keep the native speech usage checks without linking their codec workers.
  target_compile_options(q4t_model_asset_tests PRIVATE
    -Wall -Wextra -Werror -ffunction-sections -fdata-sections)
  target_link_options(q4t_model_asset_tests PRIVATE -Wl,--gc-sections)
endif()
