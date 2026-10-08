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

# Python contracts use synthetic inputs and keep artifacts in this build tree.
# Select an environment containing numpy with -DPython3_EXECUTABLE=... if needed.
function(q4t_add_python_contract name script)
  set(output_dir "${CMAKE_CURRENT_BINARY_DIR}/${name}")
  file(MAKE_DIRECTORY "${output_dir}")
  add_test(NAME ${name}
    COMMAND ${Python3_EXECUTABLE} -B
      ${CMAKE_CURRENT_SOURCE_DIR}/../../${script} ${ARGN})
  get_filename_component(script_dir "${script}" DIRECTORY)
  set_tests_properties(${name} PROPERTIES
    TIMEOUT 180
    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../../${script_dir}"
    ENVIRONMENT "Q4T_TEST_OUTPUT_DIR=${output_dir};TMPDIR=${output_dir};PYTHONDONTWRITEBYTECODE=1")
endfunction()

q4t_add_python_contract(q4t_router_trace_source_contracts
  tools/trace/test_analyze.py
  --checker $<TARGET_FILE:q4t_router_trace_check>
  --output ${CMAKE_CURRENT_BINARY_DIR}/trace-analysis-tests)
q4t_add_python_contract(q4t_router_cache_replay_contracts
  tools/trace/test_replay.py)
q4t_add_python_contract(q4t_router_shadow_contracts
  tools/trace/test_shadow.py
  --checker $<TARGET_FILE:q4t_router_trace_check>
  --output ${CMAKE_CURRENT_BINARY_DIR}/shadow-contracts)
q4t_add_python_contract(q4t_router_distribution_contracts
  tools/trace/test_distribution.py
  --checker $<TARGET_FILE:q4t_router_trace_check>)
q4t_add_python_contract(q4t_router_hybrid_contracts
  tools/trace/test_hybrid.py)

# Offline research algorithm copies are isolated from runner include/src trees.
add_executable(q4t_gpu_cache_schedule ../../tools/trace/gpu_cache_schedule.cpp)
add_executable(q4t_offload_sort ../../tools/trace/offload_sort.cpp)
foreach(target q4t_gpu_cache_schedule q4t_offload_sort)
  target_compile_options(${target} PRIVATE -Wall -Wextra -Werror)
endforeach()
q4t_add_python_contract(q4t_offload_trace_contracts
  tools/trace/test_offload_trace.py
  --checker $<TARGET_FILE:q4t_router_trace_check>
  --output ${CMAKE_CURRENT_BINARY_DIR}/offload-trace-contracts)
q4t_add_python_contract(q4t_offload_log_contracts
  tools/trace/test_offload_log.py)
q4t_add_python_contract(q4t_gpu_cache_schedule_contracts
  tools/trace/test_gpu_cache_schedule.py
  --helper $<TARGET_FILE:q4t_gpu_cache_schedule>
  --sort-helper $<TARGET_FILE:q4t_offload_sort>)
q4t_add_python_contract(q4t_gpu_cache_replay_contracts
  tools/trace/test_gpu_cache_replay.py)
q4t_add_python_contract(q4t_gpu_cache_study_contracts
  tools/trace/test_gpu_cache_study.py)
q4t_add_python_contract(q4t_decode_supply_bounds_contracts
  tools/trace/test_decode_supply_bounds.py)
q4t_add_python_contract(q4t_analyze_decode_supply_contracts
  tools/trace/test_analyze_decode_supply.py)
q4t_add_python_contract(q4t_mirror_retention_contracts
  tools/trace/test_mirror_retention.py)
q4t_add_python_contract(q4t_analyze_mirror_retention_contracts
  tools/trace/test_analyze_mirror_retention.py)
q4t_add_python_contract(q4t_physical_ram_contracts
  tools/evalscope/test_physical_ram.py)
q4t_add_python_contract(q4t_response_identity_contracts
  tools/evalscope/test_response_identity.py)
q4t_add_python_contract(q4t_acceptance_mode_contracts
  tools/evalscope/test_acceptance_mode.py)
q4t_add_python_contract(q4t_request_cancellation_contracts
  tools/evalscope/test_request_cancellation.py)
