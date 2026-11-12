# Host-only management dispatch, shared by the engine and CPU contract fixture.
include_guard(GLOBAL)
set(Q4T_SERVICE_PYTHON_EXECUTABLE "/usr/bin/python3" CACHE FILEPATH
    "Service interpreter with Linux memfd and pidfd support (Python >= 3.11)")
set(q4t_service_root "${CMAKE_CURRENT_LIST_DIR}/..")
set(q4t_service_builder "${q4t_service_root}/tools/serve_recovery/build_tools.py")
execute_process(COMMAND "${Q4T_SERVICE_PYTHON_EXECUTABLE}" -I -B
  "${q4t_service_builder}" --check-runtime COMMAND_ERROR_IS_FATAL ANY)
execute_process(COMMAND "${Q4T_SERVICE_PYTHON_EXECUTABLE}" -I -B "${q4t_service_builder}" --list
  OUTPUT_VARIABLE q4t_service_sources OUTPUT_STRIP_TRAILING_WHITESPACE
  COMMAND_ERROR_IS_FATAL ANY)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
  "${q4t_service_root}/tools/serve_recovery/bundle.py"
  "${q4t_service_root}/tools/serve_service/package.py")
file(GLOB_RECURSE q4t_service_notices CONFIGURE_DEPENDS
  "${q4t_service_root}/third_party/licenses/*")
add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/service-tools/snapshot.done"
  COMMAND "${Q4T_SERVICE_PYTHON_EXECUTABLE}" -I -B "${q4t_service_builder}"
    --output "${CMAKE_CURRENT_BINARY_DIR}/service-tools"
  DEPENDS "${q4t_service_builder}" ${q4t_service_sources} ${q4t_service_notices}
  VERBATIM)
add_custom_target(q4t_service_tools
  DEPENDS "${CMAKE_CURRENT_BINARY_DIR}/service-tools/snapshot.done")
add_library(q4t_service_cli STATIC
  "${q4t_service_root}/src/service/service_cli.cpp")
target_include_directories(q4t_service_cli PUBLIC "${q4t_service_root}/include")
target_compile_features(q4t_service_cli PUBLIC cxx_std_23)
target_compile_options(q4t_service_cli PRIVATE -Wall -Wextra -Werror)
add_dependencies(q4t_service_cli q4t_service_tools)
if(NOT DEFINED Q4T_BUILD_TESTS OR Q4T_BUILD_TESTS)
  add_executable(q4t_service_cli_fixture
    "${q4t_service_root}/tests/service_cli_fixture.cpp"
    "${q4t_service_root}/src/io/json.cpp"
    "${q4t_service_root}/src/trace/sha256.cpp")
  target_link_libraries(q4t_service_cli_fixture PRIVATE q4t_service_cli)
  target_compile_options(q4t_service_cli_fixture PRIVATE -Wall -Wextra -Werror)
endif()
