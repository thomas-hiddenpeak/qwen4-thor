# Isolated TTS qualification; never overwrites the published executable.
cmake_minimum_required(VERSION 4.0)
project(q4t_native_tts_qualification LANGUAGES CXX CUDA)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_STANDARD 23)
set(CMAKE_CUDA_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_ARCHITECTURES 110a)
find_package(CUDAToolkit REQUIRED)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(ICU REQUIRED IMPORTED_TARGET icu-uc icu-i18n)
get_filename_component(Q4T_ROOT "../.." ABSOLUTE)
add_library(q4t_tts_replay_core STATIC
  ${Q4T_ROOT}/src/audio/tts_config.cpp
  ${Q4T_ROOT}/src/audio/tts_layout.cpp
  ${Q4T_ROOT}/src/audio/tts_codec_layout.cpp
  ${Q4T_ROOT}/src/audio/tts_synthesis_plan.cpp
  ${Q4T_ROOT}/src/audio/tts_encoder_layout.cpp
  ${Q4T_ROOT}/src/audio/tts_encoder_conv.cu
  ${Q4T_ROOT}/src/audio/tts_encoder_dense.cu
  ${Q4T_ROOT}/src/audio/tts_encoder.cu
  ${Q4T_ROOT}/src/audio/tts_generation.cpp
  ${Q4T_ROOT}/src/audio/tts_dense.cu
  ${Q4T_ROOT}/src/audio/tts_model.cu
  ${Q4T_ROOT}/src/audio/tts_codec_conv.cu
  ${Q4T_ROOT}/src/audio/tts_codec_dense.cu
  ${Q4T_ROOT}/src/audio/tts_codec.cu
  ${Q4T_ROOT}/src/audio/tts_synthesis.cpp
  ${Q4T_ROOT}/src/quant/lt_cache.cpp
  ${Q4T_ROOT}/src/text/tokenizer.cpp
  ${Q4T_ROOT}/src/io/json.cpp
  ${Q4T_ROOT}/src/trace/sha256.cpp
  ${Q4T_ROOT}/src/runtime/system_memory.cpp)
target_include_directories(q4t_tts_replay_core PUBLIC
  ${Q4T_ROOT}/include ${Q4T_ROOT}/third_party)
target_link_libraries(q4t_tts_replay_core PUBLIC
  CUDA::cudart CUDA::cublasLt PkgConfig::ICU)
target_compile_options(q4t_tts_replay_core PRIVATE
  $<$<COMPILE_LANGUAGE:CXX>:-Wall;-Wextra;-Werror>
  $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=-Wall,-Wextra,-Werror>)
option(Q4T_TTS_TRACE "Capture bounded native TTS operands" OFF)
option(Q4T_TTS_DENSE_TRACE "Capture predictor operands for defect diagnosis" OFF)
if(Q4T_TTS_DENSE_TRACE)
  if(NOT Q4T_TTS_TRACE)
    message(FATAL_ERROR "Dense diagnosis requires the qualification trace hook")
  endif()
  target_compile_definitions(q4t_tts_replay_core PUBLIC Q4T_TTS_DENSE_TRACE=1)
endif()
if(Q4T_TTS_TRACE)
  target_compile_definitions(q4t_tts_replay_core PUBLIC Q4T_TTS_TRACE=1)
endif()
add_executable(q4t_tts_replay model_replay.cpp)
target_link_libraries(q4t_tts_replay PRIVATE q4t_tts_replay_core)
target_compile_options(q4t_tts_replay PRIVATE -Wall -Wextra -Werror)
add_executable(q4t_tts_codec_replay codec_replay.cpp)
target_link_libraries(q4t_tts_codec_replay PRIVATE q4t_tts_replay_core)
target_compile_options(q4t_tts_codec_replay PRIVATE -Wall -Wextra -Werror)
add_executable(q4t_tts_utterance utterance_replay.cpp)
target_link_libraries(q4t_tts_utterance PRIVATE q4t_tts_replay_core)
target_compile_options(q4t_tts_utterance PRIVATE -Wall -Wextra -Werror)
add_executable(q4t_speech_tempo tempo_replay.cpp
  ${Q4T_ROOT}/src/audio/pcm_output.cpp
  ${Q4T_ROOT}/src/audio/speech_tempo.cpp
  ${Q4T_ROOT}/src/io/json.cpp ${Q4T_ROOT}/src/trace/sha256.cpp)
target_include_directories(q4t_speech_tempo PRIVATE ${Q4T_ROOT}/include)
target_compile_options(q4t_speech_tempo PRIVATE -Wall -Wextra -Werror)

add_executable(q4t_tts_synthesis synthesis_replay.cpp
  ${Q4T_ROOT}/src/server/tts_assets.cpp
  ${Q4T_ROOT}/src/server/tts_upload.cpp
  ${Q4T_ROOT}/src/server/tts_service.cpp
  ${Q4T_ROOT}/src/server/model_assets.cpp
  ${Q4T_ROOT}/src/server/grammar_process.cpp
  ${Q4T_ROOT}/src/server/worker_io_broker.cpp)
target_link_libraries(q4t_tts_synthesis PRIVATE q4t_tts_replay_core Threads::Threads)
target_compile_options(q4t_tts_synthesis PRIVATE
  -Wall -Wextra -Werror -ffunction-sections -fdata-sections)
target_link_options(q4t_tts_synthesis PRIVATE -Wl,--gc-sections)

add_executable(q4t_tts_encoder_replay encoder_replay.cpp)
target_link_libraries(q4t_tts_encoder_replay PRIVATE q4t_tts_replay_core)
target_compile_options(q4t_tts_encoder_replay PRIVATE -Wall -Wextra -Werror)
