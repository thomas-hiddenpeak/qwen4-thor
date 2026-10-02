# CPU-only selector contracts and replay adapter. No CUDA headers/libraries.
# Runtime optimization protocol: execute tests only after the first HTTP gate.
cmake_minimum_required(VERSION 3.25)
project(q4t_chunk_order_contracts LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(q4t_chunk_order_contracts
  ../test_main.cpp ../model_moe_chunk_order_test.cpp)
add_executable(q4t_chunk_order_replay_fixture
  ../moe_chunk_order_replay_fixture.cpp)
foreach(target q4t_chunk_order_contracts q4t_chunk_order_replay_fixture)
  target_include_directories(${target} PRIVATE ../../include)
  target_compile_options(${target} PRIVATE -Wall -Wextra -Werror)
endforeach()

enable_testing()
add_test(NAME q4t_chunk_order_contracts COMMAND q4t_chunk_order_contracts)
set_tests_properties(q4t_chunk_order_contracts PROPERTIES TIMEOUT 30)
