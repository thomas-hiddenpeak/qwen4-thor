# Isolated numerical qualification build; never writes the published q4t ELF.
cmake_minimum_required(VERSION 4.0)
project(q4t_native_asr_qualification LANGUAGES CXX CUDA)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_STANDARD 23)
set(CMAKE_CUDA_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_ARCHITECTURES 110a)
find_package(CUDAToolkit REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(ICU REQUIRED IMPORTED_TARGET icu-uc icu-i18n)
get_filename_component(Q4T_ROOT "../.." ABSOLUTE)
add_executable(q4t_asr_replay
  model_replay.cpp
  ${Q4T_ROOT}/src/audio/asr_config.cpp
  ${Q4T_ROOT}/src/audio/asr_layout.cpp
  ${Q4T_ROOT}/src/audio/asr_model.cu
  ${Q4T_ROOT}/src/audio/features.cpp
  ${Q4T_ROOT}/src/model/embedding_config.cpp
  ${Q4T_ROOT}/src/model/embedding_layout.cpp
  ${Q4T_ROOT}/src/model/embedding_model.cu
  ${Q4T_ROOT}/src/model/gemv.cu
  ${Q4T_ROOT}/src/model/gemv_hc_inject.cu
  ${Q4T_ROOT}/src/model/small_m_gemm.cu
  ${Q4T_ROOT}/src/quant/lt_cache.cpp
  ${Q4T_ROOT}/src/text/tokenizer.cpp
  ${Q4T_ROOT}/src/io/json.cpp
  ${Q4T_ROOT}/src/trace/sha256.cpp
  ${Q4T_ROOT}/src/runtime/system_memory.cpp)
target_include_directories(q4t_asr_replay PRIVATE
  ${Q4T_ROOT}/include ${Q4T_ROOT}/third_party)
target_link_libraries(q4t_asr_replay PRIVATE
  CUDA::cudart CUDA::cublasLt PkgConfig::ICU)
target_compile_options(q4t_asr_replay PRIVATE
  $<$<COMPILE_LANGUAGE:CXX>:-Wall;-Wextra;-Werror>
  $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=-Wall,-Wextra,-Werror>)
option(Q4T_ASR_DECODER_TRACE "Capture one bounded decoder step" OFF)
if(Q4T_ASR_DECODER_TRACE)
  target_compile_definitions(q4t_asr_replay PRIVATE Q4T_EMBEDDING_TRACE=1)
endif()
