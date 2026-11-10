cmake_minimum_required(VERSION 3.25)
project(q4t_message_profile_probe LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(ICU REQUIRED COMPONENTS uc i18n)
find_package(Threads REQUIRED)
add_executable(template_probe template_probe.cpp
  ../../src/server/reasoning_options.cpp
  ../../src/server/chat_template.cpp ../../src/server/chat_contract.cpp
  ../../src/server/chat_tool_history.cpp ../../src/server/input_contract.cpp
  ../../src/server/output_constraints.cpp ../../src/server/output_schema.cpp
  ../../src/server/http_response.cpp ../../src/server/http_request.cpp
  ../../src/server/finite_response.cpp
  ../../src/io/json.cpp ../../src/text/tokenizer.cpp)
target_include_directories(template_probe PRIVATE ../../include)
target_compile_options(template_probe PRIVATE -Wall -Wextra -Werror)
target_link_libraries(template_probe PRIVATE ICU::uc ICU::i18n Threads::Threads)
