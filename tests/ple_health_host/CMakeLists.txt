# Thor CPU-only PLE fault contracts: real io_uring + synthetic backing files.
# CUDA headers provide types; all CUDA entry points are local CPU doubles.
# No CUDA runtime/driver/NVML library is linked or loaded by this target.
cmake_minimum_required(VERSION 3.25)
project(q4t_ple_health_contracts LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(Threads REQUIRED)
find_path(Q4T_CUDA_HEADERS cuda_runtime.h
  HINTS /usr/local/cuda/include /usr/local/cuda/targets/aarch64-linux/include
  REQUIRED)
find_path(Q4T_URING_HEADERS liburing.h REQUIRED)
find_library(Q4T_URING_LIBRARY uring REQUIRED)
add_executable(q4t_ple_health_tests ../test_main.cpp ple_health_test.cpp
  ../../src/ple/page_reader.cpp ../../src/ple/ple_embedding.cpp
  ../../src/ple/ngram_hash.cpp)
target_compile_definitions(q4t_ple_health_tests PRIVATE Q4T_HAS_LIBURING=1
  Q4T_PLE_HEALTH_FIXTURES="${CMAKE_CURRENT_BINARY_DIR}/fixtures")
target_include_directories(q4t_ple_health_tests PRIVATE ../../include
  ${Q4T_CUDA_HEADERS} ${Q4T_URING_HEADERS})
target_compile_options(q4t_ple_health_tests PRIVATE -Wall -Wextra -Werror)
target_link_libraries(q4t_ple_health_tests PRIVATE
  ${Q4T_URING_LIBRARY} Threads::Threads ${CMAKE_DL_LIBS})
enable_testing()
add_test(NAME q4t_ple_health_contracts COMMAND q4t_ple_health_tests)
set_tests_properties(q4t_ple_health_contracts PROPERTIES TIMEOUT 60)
