include_guard(GLOBAL)
set(vad_test_root "${CMAKE_CURRENT_LIST_DIR}/..")
add_executable(q4t_vad_tests EXCLUDE_FROM_ALL
  ${vad_test_root}/tests/test_main.cpp ${vad_test_root}/tests/vad_test.cpp
  ${vad_test_root}/src/audio/vad.cpp ${vad_test_root}/src/io/json.cpp)
add_executable(q4t_vad_replay EXCLUDE_FROM_ALL
  ${vad_test_root}/tools/native_asr/vad_replay.cpp
  ${vad_test_root}/src/audio/vad.cpp ${vad_test_root}/src/io/json.cpp
  ${vad_test_root}/src/trace/sha256.cpp)
foreach(target q4t_vad_tests q4t_vad_replay)
  target_include_directories(${target} PRIVATE ${vad_test_root}/include)
  target_compile_options(${target} PRIVATE -Wall -Wextra -Werror)
endforeach()
