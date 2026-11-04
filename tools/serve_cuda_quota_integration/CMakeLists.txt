cmake_minimum_required(VERSION 3.25)
project(q4t_cuda_quota_integration LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
find_package(Threads REQUIRED)
find_package(CUDAToolkit 13.3 REQUIRED)

add_executable(quota_wrapper_contracts wrapper_contracts.cpp
  ../../src/runtime/startup_reservation.cpp
  ../../src/runtime/cuda_allocation_quota.cpp
  ../../src/runtime/startup_plan.cpp ../../src/trace/sha256.cpp)
add_executable(quota_wrapper_probe probe.cpp
  ../../src/runtime/startup_reservation.cpp
  ../../src/runtime/cuda_allocation_quota.cpp
  ../../src/runtime/startup_plan.cpp ../../src/trace/sha256.cpp)
foreach(target quota_wrapper_contracts quota_wrapper_probe)
  target_include_directories(${target} PRIVATE ../../include
    ${CUDAToolkit_INCLUDE_DIRS})
  target_compile_options(${target} PRIVATE -Wall -Wextra -Werror
    -ffunction-sections -fdata-sections)
  # Retain production StartupPlan::AddBytes; unused model planner overloads
  # are discarded. Neither fixture loads a checkpoint or tests its planner.
  target_link_options(${target} PRIVATE LINKER:--gc-sections)
  target_link_libraries(${target} PRIVATE Threads::Threads)
endforeach()
target_compile_definitions(quota_wrapper_contracts PRIVATE
  Q4T_STARTUP_RESERVATION=1)
# CPU fake symbols are never linked to CUDA. The native probe uses the exact
# same GNU link boundary as production q4t.
include(../../cmake/CudaCleanupBoundary.cmake)
q4t_link_cuda_cleanup(quota_wrapper_probe)
target_link_libraries(quota_wrapper_probe PRIVATE CUDA::cudart)
