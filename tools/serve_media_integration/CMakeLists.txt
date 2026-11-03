cmake_minimum_required(VERSION 3.24)
project(q4t_media_integration_cpu LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
get_filename_component(Q4T_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
find_package(Threads REQUIRED)
add_executable(processor_contract processor_contract.cpp
  "${Q4T_ROOT}/src/vision/media_plan.cpp"
  "${Q4T_ROOT}/src/vision/processor.cpp")
target_include_directories(processor_contract PRIVATE "${Q4T_ROOT}/include"
  "${Q4T_ROOT}/third_party")
target_link_libraries(processor_contract PRIVATE Threads::Threads)
target_compile_options(processor_contract PRIVATE -Wall -Wextra -Werror
  -fsanitize=address,undefined -fno-sanitize-recover=all)
target_link_options(processor_contract PRIVATE -fsanitize=address,undefined
  -Wl,--wrap=malloc -Wl,--wrap=realloc -Wl,--wrap=free)
