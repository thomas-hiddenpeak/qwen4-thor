# CPU-only IO fault and WeightLoader lifetime contracts with synthetic
# shards. CUDA headers supply types; cudaMemcpyAsync is a host test double,
# so this target neither links CUDA libraries nor checks actual device
# transfers. Function sections discard the unused H2D entry points.
cmake_minimum_required(VERSION 3.25)
project(q4t_io_contracts LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(Threads REQUIRED)
find_path(Q4T_CUDA_HEADERS cuda_runtime.h
  HINTS /usr/local/cuda/include /usr/local/cuda/targets/aarch64-linux/include
  REQUIRED)
enable_testing()
function(q4t_io_contract name)
  add_executable(${name} ../test_main.cpp ${ARGN}
    ../../src/io/json.cpp ../../src/io/safetensors.cpp
    ../../src/io/weight_loader.cpp)
  target_include_directories(${name} PRIVATE ../../include ${Q4T_CUDA_HEADERS})
  target_compile_definitions(${name} PRIVATE
    Q4T_IO_FIXTURE_ROOT="${CMAKE_CURRENT_BINARY_DIR}/fixtures/${name}")
  target_compile_options(${name} PRIVATE -Wall -Wextra -Werror
    -ffunction-sections -fdata-sections)
  target_link_options(${name} PRIVATE -Wl,--gc-sections)
  target_link_libraries(${name} PRIVATE Threads::Threads)
  add_test(NAME ${name} COMMAND ${name})
  set_tests_properties(${name} PROPERTIES TIMEOUT 60
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/../..)
endfunction()

# Each syscall double owns its own state and global wrapper symbols.
q4t_io_contract(q4t_io_contracts ../io_read_contract_test.cpp)
target_link_options(q4t_io_contracts PRIVATE
  -Wl,--wrap=pread,--wrap=preadv,--wrap=close)
q4t_io_contract(q4t_weight_loader_contracts ../weight_loader_contract_test.cpp)
target_link_options(q4t_weight_loader_contracts PRIVATE
  -Wl,--wrap=pread,--wrap=close,--wrap=cudaMemcpyAsync)
q4t_io_contract(q4t_safetensors_validation ../safetensors_validation_test.cpp)
target_link_options(q4t_safetensors_validation PRIVATE
  -Wl,--wrap=mmap,--wrap=_Znwm)
q4t_io_contract(q4t_residency_host_contracts
  ../residency_host_contract_test.cpp ../../src/quant/moe_residency.cpp
  ../../src/quant/moe_weights.cpp)
function(q4t_residency_fault_wrappers name)
  # Static libstdc++ makes its pthread references visible to --wrap, so a
  # failure after an already running std::thread is tested deterministically.
  target_link_options(${name} PRIVATE -static-libstdc++
    -Wl,--wrap=pthread_create,--wrap=pthread_join
    -Wl,--wrap=_ZnwmRKSt9nothrow_t,--wrap=_Znwm,--wrap=pread
    -Wl,--wrap=cudaMemcpyAsync
    -Wl,--wrap=cudaMalloc,--wrap=cudaFree
    -Wl,--wrap=cudaHostAlloc,--wrap=cudaFreeHost
    -Wl,--wrap=cudaEventCreateWithFlags,--wrap=cudaEventDestroy
    -Wl,--wrap=cudaStreamSynchronize)
endfunction()
q4t_residency_fault_wrappers(q4t_residency_host_contracts)

option(Q4T_BUILD_RESIDENCY_CUDA_CONTRACTS
  "Build the real-CUDA variant (run manually under gpu_exclusive)" OFF)
if(Q4T_BUILD_RESIDENCY_CUDA_CONTRACTS)
  find_library(Q4T_CUDART cudart HINTS /usr/local/cuda/lib64 REQUIRED)
  add_executable(q4t_residency_cuda_contracts ../test_main.cpp
    ../residency_host_contract_test.cpp ../../src/quant/moe_residency.cpp
    ../../src/quant/moe_weights.cpp
    ../../src/io/json.cpp ../../src/io/safetensors.cpp
    ../../src/io/weight_loader.cpp)
  target_include_directories(q4t_residency_cuda_contracts PRIVATE
    ../../include ${Q4T_CUDA_HEADERS})
  target_compile_definitions(q4t_residency_cuda_contracts PRIVATE
    Q4T_RESIDENCY_REAL_CUDA=1
    Q4T_IO_FIXTURE_ROOT="${CMAKE_CURRENT_BINARY_DIR}/fixtures/cuda")
  target_compile_options(q4t_residency_cuda_contracts PRIVATE
    -Wall -Wextra -Werror)
  target_link_libraries(q4t_residency_cuda_contracts PRIVATE
    Threads::Threads ${Q4T_CUDART})
  q4t_residency_fault_wrappers(q4t_residency_cuda_contracts)
  # Deliberately not registered with CTest: CPU checks must not acquire GPU
  # resources, and GPU acceptance owns one lease for the complete stage.
endif()
