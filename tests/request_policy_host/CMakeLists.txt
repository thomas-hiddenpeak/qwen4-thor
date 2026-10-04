# CPU-only request identity and policy contracts. Execute after the first HTTP.
cmake_minimum_required(VERSION 3.25)
project(q4t_request_policy_contracts LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(q4t_request_policy_contracts
  ../test_main.cpp request_policy_test.cpp)
target_include_directories(q4t_request_policy_contracts PRIVATE ../../include)
target_compile_options(q4t_request_policy_contracts PRIVATE
  -Wall -Wextra -Werror)

enable_testing()
add_test(NAME q4t_request_policy_contracts COMMAND q4t_request_policy_contracts)
set_tests_properties(q4t_request_policy_contracts PROPERTIES TIMEOUT 30)
