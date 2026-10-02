# Pure host allocation-estimate and shared residency-configuration contracts.
cmake_minimum_required(VERSION 3.25)
project(q4t_budget_contracts LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
add_executable(q4t_budget_contracts ../test_main.cpp
  ../runtime_memory_budget_test.cpp ../../src/runtime/memory_budget.cpp
  ../../src/io/json.cpp)
target_include_directories(q4t_budget_contracts PRIVATE ../../include)
target_compile_options(q4t_budget_contracts PRIVATE -Wall -Wextra -Werror)
enable_testing()
add_test(NAME q4t_budget_contracts COMMAND q4t_budget_contracts)
set_tests_properties(q4t_budget_contracts PROPERTIES TIMEOUT 30
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/../..)
