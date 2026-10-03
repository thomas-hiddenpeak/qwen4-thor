# CPU-only shared partition and runtime adapter contracts. No CUDA dependency.
# Execute tests only after the optimization's first real HTTP E2E gate.
cmake_minimum_required(VERSION 3.25)
project(q4t_partition_contracts LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(q4t_partition_contracts ../test_main.cpp partition_test.cpp)
add_executable(q4t_partition_algorithm_contracts
  ../../tools/trace/test_offload_partition.cpp)
foreach(target q4t_partition_contracts q4t_partition_algorithm_contracts)
  target_include_directories(${target} PRIVATE ../../include)
  target_compile_options(${target} PRIVATE -Wall -Wextra -Werror)
endforeach()

enable_testing()
add_test(NAME q4t_partition_contracts COMMAND q4t_partition_contracts)
add_test(NAME q4t_partition_algorithm_contracts
  COMMAND q4t_partition_algorithm_contracts)
set_tests_properties(q4t_partition_contracts
  q4t_partition_algorithm_contracts PROPERTIES TIMEOUT 30)
