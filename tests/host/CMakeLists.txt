# Public Linux checks: no CUDA, ICU, weights, or private fixtures required.
cmake_minimum_required(VERSION 3.25)
project(q4t_public_host_checks LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

include(../../cmake/HostTestSources.cmake)
find_package(Threads REQUIRED)
add_executable(q4t_host_tests ${Q4T_HOST_TEST_SOURCES} ../../src/io/json.cpp)
target_include_directories(q4t_host_tests PRIVATE ../../include)
target_compile_options(q4t_host_tests PRIVATE -Wall -Wextra -Werror)
target_link_libraries(q4t_host_tests PRIVATE Threads::Threads)

enable_testing()
# Run the complete public registry, strictly: no filters or allowed skips.
add_test(NAME q4t_public_host_contracts COMMAND q4t_host_tests)
set_tests_properties(q4t_public_host_contracts PROPERTIES TIMEOUT 180)

# Offline trace validation shares no runtime/CUDA dependencies.
add_executable(q4t_router_trace_check ../../tools/trace/router_trace_check.cpp
  ../../src/trace/router_trace.cpp)
target_include_directories(q4t_router_trace_check PRIVATE ../../include)
target_compile_options(q4t_router_trace_check PRIVATE -Wall -Wextra -Werror)
find_package(Python3 REQUIRED COMPONENTS Interpreter)
add_test(NAME q4t_router_trace_wire_contracts
  COMMAND ${Python3_EXECUTABLE} -B
    ${CMAKE_CURRENT_SOURCE_DIR}/../../tools/trace/test_wire.py
    --checker $<TARGET_FILE:q4t_router_trace_check>
    --output ${CMAKE_CURRENT_BINARY_DIR}/trace-wire-tests)
set_tests_properties(q4t_router_trace_wire_contracts PROPERTIES TIMEOUT 180)
