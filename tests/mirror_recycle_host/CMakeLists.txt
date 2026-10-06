# Execute these host contracts only after the optimization's first HTTP E2E.
cmake_minimum_required(VERSION 3.25)
project(q4t_mirror_recycle_contracts LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
add_executable(q4t_mirror_recycle_contracts
  ../test_main.cpp mirror_recycle_test.cpp)
target_include_directories(q4t_mirror_recycle_contracts PRIVATE ../../include)
target_compile_options(q4t_mirror_recycle_contracts PRIVATE
  -Wall -Wextra -Werror)
enable_testing()
add_test(NAME q4t_mirror_recycle_contracts COMMAND q4t_mirror_recycle_contracts)
set_tests_properties(q4t_mirror_recycle_contracts PROPERTIES TIMEOUT 30)
