# Shared host-only process primitives. Do not link the CUDA-dependent q4t_io
# library or serving libraries here, even when included by the native build.
include_guard(GLOBAL)
add_library(q4t_guardian_process STATIC
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/guardian_process.cpp)
target_include_directories(q4t_guardian_process PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}/../include)
target_compile_features(q4t_guardian_process PUBLIC cxx_std_23)
target_compile_options(q4t_guardian_process PRIVATE -Wall -Wextra -Werror)

add_library(q4t_device_filter STATIC
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/device_filter.cpp)
target_link_libraries(q4t_device_filter PUBLIC q4t_guardian_process)
target_compile_options(q4t_device_filter PRIVATE -Wall -Wextra -Werror)

add_library(q4t_guardian_journal STATIC
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/guardian_journal.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/guardian_wire.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/io/json.cpp)
target_link_libraries(q4t_guardian_journal PUBLIC q4t_guardian_process)
target_compile_options(q4t_guardian_journal PRIVATE -Wall -Wextra -Werror)

add_library(q4t_device_admission STATIC
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/device_admission.cpp)
target_link_libraries(q4t_device_admission PUBLIC
  q4t_device_filter q4t_guardian_journal)
target_compile_options(q4t_device_admission PRIVATE -Wall -Wextra -Werror)

add_executable(q4t_guardian
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/guardian_main.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/guardian_version.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/trace/sha256.cpp)
set_target_properties(q4t_guardian PROPERTIES OUTPUT_NAME q4t-guardian)
target_link_libraries(q4t_guardian PRIVATE q4t_guardian_journal)
target_compile_options(q4t_guardian PRIVATE -Wall -Wextra -Werror)

if(NOT DEFINED Q4T_BUILD_TESTS OR Q4T_BUILD_TESTS)
  add_executable(q4t_device_filter_tests
    ${CMAKE_CURRENT_LIST_DIR}/../tests/device_filter_test.cpp)
  target_link_libraries(q4t_device_filter_tests PRIVATE q4t_device_admission)
  target_compile_options(q4t_device_filter_tests PRIVATE
    -Wall -Wextra -Werror)
  add_executable(q4t_guardian_process_tests
    ${CMAKE_CURRENT_LIST_DIR}/../tests/guardian_process_test.cpp)
  target_link_libraries(q4t_guardian_process_tests PRIVATE q4t_guardian_process)
  target_compile_options(q4t_guardian_process_tests PRIVATE
    -Wall -Wextra -Werror)
  add_executable(q4t_guardian_journal_tests
    ${CMAKE_CURRENT_LIST_DIR}/../tests/guardian_journal_test.cpp)
  target_link_libraries(q4t_guardian_journal_tests PRIVATE q4t_guardian_journal)
  target_compile_options(q4t_guardian_journal_tests PRIVATE
    -Wall -Wextra -Werror)
endif()
