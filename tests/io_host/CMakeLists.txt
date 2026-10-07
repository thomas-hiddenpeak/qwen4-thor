# CPU-only WeightLoader lifetime contracts with synthetic shards. CUDA
# headers supply types; cudaMemcpyAsync is a host test double, so this target
# neither links CUDA libraries nor checks actual device transfers.
cmake_minimum_required(VERSION 3.25)
project(q4t_io_contracts LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(Threads REQUIRED)
find_path(Q4T_CUDA_HEADERS cuda_runtime.h
  HINTS /usr/local/cuda/include /usr/local/cuda/targets/aarch64-linux/include
  REQUIRED)

add_executable(q4t_io_contracts ../test_main.cpp
  ../weight_loader_contract_test.cpp ../../src/io/json.cpp
  ../../src/io/safetensors.cpp ../../src/io/weight_loader.cpp)
target_include_directories(q4t_io_contracts PRIVATE ../../include
  ${Q4T_CUDA_HEADERS})
target_compile_definitions(q4t_io_contracts PRIVATE
  Q4T_IO_FIXTURE_ROOT="${CMAKE_CURRENT_BINARY_DIR}/fixtures")
target_compile_options(q4t_io_contracts PRIVATE -Wall -Wextra -Werror)
target_link_options(q4t_io_contracts PRIVATE
  -Wl,--wrap=pread -Wl,--wrap=close -Wl,--wrap=cudaMemcpyAsync)
target_link_libraries(q4t_io_contracts PRIVATE Threads::Threads)

enable_testing()
add_test(NAME q4t_io_contracts COMMAND q4t_io_contracts)
set_tests_properties(q4t_io_contracts PROPERTIES TIMEOUT 60)
