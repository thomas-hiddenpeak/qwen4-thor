# Isolated component build. Never overwrites the published q4t executable.
cmake_minimum_required(VERSION 4.0)
project(q4t_native_speaker_qualification LANGUAGES CXX CUDA)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_STANDARD 23)
set(CMAKE_CUDA_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_ARCHITECTURES 110a)
find_package(CUDAToolkit REQUIRED)
get_filename_component(Q4T_ROOT "../.." ABSOLUTE)
add_executable(q4t_speaker_replay model_replay.cpp
  ${Q4T_ROOT}/src/audio/speaker_config.cpp
  ${Q4T_ROOT}/src/audio/speaker_layout.cpp
  ${Q4T_ROOT}/src/audio/speaker_features.cpp
  ${Q4T_ROOT}/src/audio/speaker_model.cu
  ${Q4T_ROOT}/src/quant/lt_cache.cpp
  ${Q4T_ROOT}/src/io/json.cpp
  ${Q4T_ROOT}/src/trace/sha256.cpp
  ${Q4T_ROOT}/src/runtime/system_memory.cpp)
target_include_directories(q4t_speaker_replay PRIVATE ${Q4T_ROOT}/include)
target_link_libraries(q4t_speaker_replay PRIVATE CUDA::cudart CUDA::cublasLt)
target_compile_options(q4t_speaker_replay PRIVATE
  $<$<COMPILE_LANGUAGE:CXX>:-Wall;-Wextra;-Werror>
  $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=-Wall,-Wextra,-Werror>)
