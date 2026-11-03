cmake_minimum_required(VERSION 3.24)
project(q4t_media_plan_contract LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
get_filename_component(Q4T_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)

add_executable(media_plan_contract contract.cpp
  "${Q4T_ROOT}/src/vision/media_plan.cpp")
target_include_directories(media_plan_contract PRIVATE "${Q4T_ROOT}/include")
target_compile_options(media_plan_contract PRIVATE -Wall -Wextra -Werror
  -fsanitize=address,undefined -fno-sanitize-recover=all)
target_link_options(media_plan_contract PRIVATE -fsanitize=address,undefined
  -Wl,--wrap=malloc -Wl,--wrap=realloc -Wl,--wrap=calloc)

# Compile the unchanged real processor for a small geometry differential only.
# This target has no CUDA library, model, HTTP server or GPU initialization.
add_executable(media_plan_processor processor.cpp
  "${Q4T_ROOT}/src/vision/media_plan.cpp"
  "${Q4T_ROOT}/src/vision/processor.cpp")
target_include_directories(media_plan_processor PRIVATE "${Q4T_ROOT}/include"
  "${Q4T_ROOT}/third_party")
target_compile_options(media_plan_processor PRIVATE -Wall -Wextra -Werror
  -fsanitize=address,undefined -fno-sanitize-recover=all)
target_link_options(media_plan_processor PRIVATE -fsanitize=address,undefined)
