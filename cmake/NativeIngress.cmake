# Standalone service transport; reuses host HTTP/JSON without model libraries.
include_guard(GLOBAL)
find_package(Threads REQUIRED)
add_executable(q4t_ingress
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_main.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_transport.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/server/http_request.cpp)
set_target_properties(q4t_ingress PROPERTIES OUTPUT_NAME q4t-ingress)
target_link_libraries(q4t_ingress PRIVATE q4t_guardian_journal Threads::Threads)
target_compile_features(q4t_ingress PRIVATE cxx_std_23)
target_compile_options(q4t_ingress PRIVATE -Wall -Wextra -Werror)
