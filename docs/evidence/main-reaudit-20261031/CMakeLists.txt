cmake_minimum_required(VERSION 3.25)
project(MainAuditProbes LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Threads REQUIRED)
set(AUDIT_SOURCE_ROOT "/home/rm01/models/dev/qwen4-thor" CACHE PATH "Production source snapshot")
add_executable(residency_probe residency_probe.cpp
  "/home/rm01/models/dev/qwen4-thor/tests/test_main.cpp"
  ${AUDIT_SOURCE_ROOT}/src/quant/moe_residency.cpp
  ${AUDIT_SOURCE_ROOT}/src/io/json.cpp
  ${AUDIT_SOURCE_ROOT}/src/io/safetensors.cpp
  ${AUDIT_SOURCE_ROOT}/src/io/weight_loader.cpp)
target_include_directories(residency_probe PRIVATE ${AUDIT_SOURCE_ROOT}/include
  /usr/local/cuda/include)
target_compile_definitions(residency_probe PRIVATE
  Q4T_IO_FIXTURE_ROOT="${CMAKE_CURRENT_BINARY_DIR}/fixtures")
target_compile_options(residency_probe PRIVATE -Wall -Wextra -Werror)
target_link_libraries(residency_probe PRIVATE Threads::Threads)
target_link_options(residency_probe PRIVATE -static-libstdc++
  -Wl,--wrap=pthread_create,--wrap=pthread_join,--wrap=_Znwm
  -Wl,--wrap=_ZnwmRKSt9nothrow_t
  -Wl,--wrap=cudaMalloc,--wrap=cudaFree
  -Wl,--wrap=cudaHostAlloc,--wrap=cudaFreeHost
  -Wl,--wrap=cudaEventCreateWithFlags,--wrap=cudaEventDestroy
  -Wl,--wrap=cudaStreamSynchronize)
