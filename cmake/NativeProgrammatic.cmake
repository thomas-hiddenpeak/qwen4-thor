include_guard(GLOBAL)
# The embedded interpreter has no OS module or external executable. It runs
# only in an independently bounded exec worker when a program is requested.
enable_language(C)
set(q4t_quickjs "${CMAKE_CURRENT_LIST_DIR}/../third_party/quickjs")
file(READ "${q4t_quickjs}/SOURCE.json" q4t_quickjs_source)
string(JSON q4t_quickjs_count LENGTH "${q4t_quickjs_source}" files)
math(EXPR q4t_quickjs_last "${q4t_quickjs_count} - 1")
foreach(index RANGE 0 ${q4t_quickjs_last})
  string(JSON name MEMBER "${q4t_quickjs_source}" files ${index})
  string(JSON expected GET "${q4t_quickjs_source}" files "${name}")
  file(SHA256 "${q4t_quickjs}/${name}" actual)
  if(NOT actual STREQUAL expected)
    message(FATAL_ERROR "Embedded QuickJS source identity changed: ${name}")
  endif()
endforeach()
add_library(q4t_program_javascript STATIC
  "${q4t_quickjs}/quickjs.c" "${q4t_quickjs}/dtoa.c"
  "${q4t_quickjs}/libregexp.c" "${q4t_quickjs}/libunicode.c"
  "${q4t_quickjs}/cutils.c")
set_target_properties(q4t_program_javascript PROPERTIES C_STANDARD 11
  C_STANDARD_REQUIRED YES POSITION_INDEPENDENT_CODE YES)
target_compile_definitions(q4t_program_javascript PRIVATE
  _GNU_SOURCE CONFIG_VERSION="2026-06-04")
# Upstream arithmetic relies on signed wrapping. Warning exceptions are
# confined to unchanged third-party code, never q4t's worker or contracts.
target_compile_options(q4t_program_javascript PRIVATE -Wall -Wextra -fwrapv
  -UNDEBUG -Wno-sign-compare -Wno-unused-parameter
  -Wno-missing-field-initializers -Wno-implicit-fallthrough)
target_include_directories(q4t_program_javascript SYSTEM PUBLIC
  "${q4t_quickjs}")
target_link_libraries(q4t_program_javascript PUBLIC m)
function(q4t_link_native_programmatic target)
  target_link_libraries(${target} PRIVATE q4t_program_javascript)
endfunction()
