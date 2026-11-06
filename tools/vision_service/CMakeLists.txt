cmake_minimum_required(VERSION 3.25)
project(q4t_vision_contract LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
get_filename_component(Q4T_ROOT "../.." ABSOLUTE)
add_executable(vision_contract probe.cpp
  "${Q4T_ROOT}/src/vision/media_plan.cpp"
  "${Q4T_ROOT}/src/vision/processor.cpp"
  "${Q4T_ROOT}/src/io/json.cpp")
target_include_directories(vision_contract PRIVATE "${Q4T_ROOT}/include"
  "${Q4T_ROOT}/third_party")
target_compile_options(vision_contract PRIVATE -Wall -Wextra -Werror)
