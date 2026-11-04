cmake_minimum_required(VERSION 3.25)
project(q4t_error_param_probe LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(Q4T_ENABLE_XGRAMMAR OFF)
include(../../cmake/HostTestSources.cmake)
list(FILTER Q4T_HOST_TEST_SOURCES INCLUDE REGEX "/src/")
get_filename_component(repo "../.." ABSOLUTE)
if(Q4T_ERROR_BASELINE)
  foreach(name input_contract output_constraints output_schema chat_contract
               chat_plan)
    list(FILTER Q4T_HOST_TEST_SOURCES EXCLUDE REGEX "/${name}\\.cpp$")
    list(APPEND Q4T_HOST_TEST_SOURCES
      "${Q4T_ERROR_BASELINE}/src/server/${name}.cpp")
  endforeach()
endif()
add_executable(error_param_probe probe.cpp ${Q4T_HOST_TEST_SOURCES}
  ../../src/io/json.cpp)
if(Q4T_ERROR_BASELINE)
  target_include_directories(error_param_probe BEFORE PRIVATE
    "${Q4T_ERROR_BASELINE}/include")
endif()
target_include_directories(error_param_probe PRIVATE "${repo}/include"
  "${Q4T_CHAT_PROTOCOL_INCLUDE_DIR}")
target_compile_options(error_param_probe PRIVATE -Wall -Wextra -Werror)
find_package(Threads REQUIRED)
target_link_libraries(error_param_probe PRIVATE Threads::Threads)
q4t_link_structured_output(error_param_probe)
