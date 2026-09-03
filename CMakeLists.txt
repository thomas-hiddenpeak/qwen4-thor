# Qwen4-Thor — native C++17/CUDA inference engine for
# Qwen3.8-Flash-Next (qwen4_exp) on Jetson AGX Thor (SM110a).
cmake_minimum_required(VERSION 3.24)

project(
  qwen4-thor
  VERSION 0.1.0
  DESCRIPTION "Qwen3.8-Flash-Next inference engine for Jetson AGX Thor"
  LANGUAGES CXX CUDA)

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
option(Q4T_BUILD_TESTS "Build unit tests" ON)

# CUDA architecture. Thor is SM110a. Overridable:
#   -DQ4T_CUDA_ARCHITECTURES=110a
if(NOT DEFINED Q4T_CUDA_ARCHITECTURES)
  set(Q4T_CUDA_ARCHITECTURES "110a" CACHE STRING "CUDA architecture")
endif()

# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_CUDA_STANDARD 17)
set(CMAKE_CUDA_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

set(CMAKE_CUDA_ARCHITECTURES "${Q4T_CUDA_ARCHITECTURES}")
set(CMAKE_CUDA_FLAGS
    "${CMAKE_CUDA_FLAGS} --expt-relaxed-constexpr -O3")

add_compile_options(-Wall -Wextra)

# CUDA toolkit headers/libraries for C++ sources that include CUDA runtime.
find_package(CUDAToolkit REQUIRED)

# ---------------------------------------------------------------------------
# liburing (PLE SSD Stream io_uring reader dependency)
# ---------------------------------------------------------------------------
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(LIBURING QUIET liburing)
endif()
if(LIBURING_FOUND)
  set(Q4T_LIBURING_DEFS Q4T_HAS_LIBURING=1)
  set(Q4T_LIBURING_INC ${LIBURING_INCLUDE_DIRS})
  set(Q4T_LIBURING_LIBS ${LIBURING_LINK_LIBRARIES})
  message(STATUS "liburing found: ${LIBURING_INCLUDE_DIRS}")
else()
  set(Q4T_LIBURING_DEFS "")
  set(Q4T_LIBURING_INC "")
  set(Q4T_LIBURING_LIBS "")
  message(STATUS "liburing not found — PLE io_uring reader disabled")
endif()

# ---------------------------------------------------------------------------
# q4t_ple — PLE n-gram hash + SSD stream (grows as modules land)
# ---------------------------------------------------------------------------
add_library(q4t_ple STATIC
  src/ple/ngram_hash.cpp
  src/ple/ngram_hash_derive.cpp
  src/ple/page_reader.cpp
  src/ple/fp8_convert.cu
  src/ple/ple_embedding.cpp)
target_include_directories(q4t_ple PUBLIC include)
target_compile_definitions(q4t_ple PRIVATE ${Q4T_LIBURING_DEFS})
if(Q4T_LIBURING_INC)
  target_include_directories(q4t_ple PRIVATE ${Q4T_LIBURING_INC})
endif()
if(Q4T_LIBURING_LIBS)
  target_link_libraries(q4t_ple PUBLIC ${Q4T_LIBURING_LIBS})
endif()
target_link_libraries(q4t_ple PUBLIC CUDA::cudart)

# ---------------------------------------------------------------------------
# q4t — CLI executable
# ---------------------------------------------------------------------------
add_executable(q4t src/main.cpp)
target_include_directories(q4t PRIVATE include)
target_link_libraries(q4t PRIVATE q4t_ple CUDA::cudart)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
if(Q4T_BUILD_TESTS)
  enable_testing()
  add_executable(q4t_tests
    tests/ple_ngram_hash_test.cpp
    tests/ple_page_reader_test.cpp
    tests/ple_fp8_convert_test.cpp
    tests/ple_e2e_gather_test.cpp
    tests/test_main.cpp)
  target_include_directories(q4t_tests PRIVATE include)
  target_link_libraries(q4t_tests PRIVATE q4t_ple)
  add_test(NAME q4t_tests COMMAND q4t_tests)
endif()
