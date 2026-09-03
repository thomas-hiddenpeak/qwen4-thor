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
# q4t core library (grows as modules land)
# ---------------------------------------------------------------------------
# Sources are added here as modules are implemented. The skeleton ships
# with the CLI entry point only.
set(Q4T_SOURCES
  src/main.cpp)

add_executable(q4t ${Q4T_SOURCES})
target_include_directories(q4t PRIVATE include)
target_link_libraries(q4t PRIVATE CUDA::cudart)

# liburing for the PLE SSD Stream io_uring reader (Phase 1).
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(LIBURING QUIET liburing)
endif()
if(LIBURING_FOUND)
  target_compile_definitions(q4t PRIVATE Q4T_HAS_LIBURING=1)
  target_include_directories(q4t PRIVATE ${LIBURING_INCLUDE_DIRS})
  target_link_libraries(q4t PRIVATE ${LIBURING_LINK_LIBRARIES})
  message(STATUS "liburing found: ${LIBURING_INCLUDE_DIRS}")
else()
  message(STATUS "liburing not found — PLE io_uring reader disabled")
endif()

# ---------------------------------------------------------------------------
# Tests (skeleton)
# ---------------------------------------------------------------------------
if(Q4T_BUILD_TESTS)
  enable_testing()
  # Test targets are registered here as they are implemented.
endif()
