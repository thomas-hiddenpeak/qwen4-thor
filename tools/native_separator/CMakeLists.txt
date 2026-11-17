# Isolated component build; never overwrites the published q4t executable.
cmake_minimum_required(VERSION 4.0)
project(q4t_native_separator_qualification LANGUAGES CXX CUDA)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_STANDARD 23)
set(CMAKE_CUDA_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_ARCHITECTURES 110a)
find_package(CUDAToolkit REQUIRED)
get_filename_component(Q4T_ROOT "../.." ABSOLUTE)
add_library(q4t_separator_component STATIC
  ${Q4T_ROOT}/src/audio/separator_config.cpp
  ${Q4T_ROOT}/src/audio/separator_layout.cpp
  ${Q4T_ROOT}/src/audio/separator_model.cu
  ${Q4T_ROOT}/src/quant/lt_cache.cpp
  ${Q4T_ROOT}/src/io/json.cpp)
target_include_directories(q4t_separator_component PUBLIC ${Q4T_ROOT}/include)
target_link_libraries(q4t_separator_component PUBLIC
  CUDA::cudart CUDA::cublasLt)
target_compile_options(q4t_separator_component PRIVATE
  $<$<COMPILE_LANGUAGE:CXX>:-Wall;-Wextra;-Werror>
  $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=-Wall,-Wextra,-Werror>)
