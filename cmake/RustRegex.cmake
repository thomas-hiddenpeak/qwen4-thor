include_guard(GLOBAL)
# One fixed, dependency-free parser crate. The resulting archive is linked into
# the existing bounded grammar worker; cargo and Rust are not runtime services.
find_program(Q4T_RUSTC rustc REQUIRED)
include(FetchContent)
FetchContent_Declare(q4t_regex_syntax
  URL https://static.crates.io/crates/regex-syntax/regex-syntax-0.8.8.crate
  URL_HASH SHA256=7a2d987857b319362043e95f5353c0535c1f58eec5336fdfcf626430af7def58
  SOURCE_SUBDIR q4t-populate-only)
FetchContent_MakeAvailable(q4t_regex_syntax)
file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/regex-syntax-0.8.8.sha256"
  regex_checksums)
foreach(entry IN LISTS regex_checksums)
  string(SUBSTRING "${entry}" 0 64 expected)
  string(SUBSTRING "${entry}" 66 -1 relative)
  file(SHA256 "${q4t_regex_syntax_SOURCE_DIR}/${relative}" actual)
  if(NOT actual STREQUAL expected)
    message(FATAL_ERROR "Changed regex-syntax source: ${relative}")
  endif()
endforeach()
set(regex_output "${CMAKE_BINARY_DIR}/q4t-regex")
set(regex_rlib "${regex_output}/libregex_syntax.rlib")
set(regex_archive "${regex_output}/libq4t_regex.a")
file(GLOB_RECURSE regex_sources CONFIGURE_DEPENDS
  "${q4t_regex_syntax_SOURCE_DIR}/src/*.rs")
set(regex_features)
foreach(feature std unicode unicode-age unicode-bool unicode-case
    unicode-gencat unicode-perl unicode-script unicode-segment)
  list(APPEND regex_features --cfg "feature=\"${feature}\"")
endforeach()
add_custom_command(OUTPUT "${regex_rlib}"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${regex_output}"
  COMMAND "${Q4T_RUSTC}" --crate-name regex_syntax --crate-type rlib
    --edition=2021 -C opt-level=3 ${regex_features}
    "${q4t_regex_syntax_SOURCE_DIR}/src/lib.rs" -o "${regex_rlib}"
  DEPENDS ${regex_sources} VERBATIM)
add_custom_command(OUTPUT "${regex_archive}"
  COMMAND "${Q4T_RUSTC}" --crate-name q4t_regex --crate-type staticlib
    --edition=2021 -C opt-level=3 -D warnings
    --extern "regex_syntax=${regex_rlib}"
    "${CMAKE_CURRENT_LIST_DIR}/../src/server/regex_syntax/lib.rs"
    -o "${regex_archive}"
  DEPENDS "${regex_rlib}"
    "${CMAKE_CURRENT_LIST_DIR}/../src/server/regex_syntax/lib.rs" VERBATIM)
add_custom_target(q4t_regex_build DEPENDS "${regex_archive}")
add_library(q4t_regex STATIC IMPORTED GLOBAL)
set_target_properties(q4t_regex PROPERTIES IMPORTED_LOCATION "${regex_archive}")
add_dependencies(q4t_regex q4t_regex_build)
