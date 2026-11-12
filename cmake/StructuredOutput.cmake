include_guard(GLOBAL)
option(Q4T_ENABLE_XGRAMMAR "Build experimental constrained text decoding" OFF)

# Opt-in only until Thor acceptance. No network access in default/public builds.
if(Q4T_ENABLE_XGRAMMAR)
  include(FetchContent)
  set(XGRAMMAR_BUILD_PYTHON_BINDINGS OFF CACHE BOOL "" FORCE)
  set(XGRAMMAR_BUILD_CXX_TESTS OFF CACHE BOOL "" FORCE)
  set(XGRAMMAR_ENABLE_CPPTRACE OFF CACHE BOOL "" FORCE)
  FetchContent_Declare(q4t_xgrammar
    GIT_REPOSITORY https://github.com/mlc-ai/xgrammar.git
    GIT_TAG ca1acf28bf35b9ba9092800b69aebc7d4ce1f7a7
    GIT_SUBMODULES 3rdparty/dlpack
    GIT_SUBMODULES_RECURSE FALSE
    SOURCE_SUBDIR q4t-populate-only)
  # Upstream install rules assume a standalone source tree. This embedded
  # static dependency is not independently installed by the runner.
  function(q4t_populate_xgrammar)
    set(SKBUILD TRUE)
    FetchContent_MakeAvailable(q4t_xgrammar)
    foreach(spec IN ITEMS
        "${q4t_xgrammar_SOURCE_DIR}|ca1acf28bf35b9ba9092800b69aebc7d4ce1f7a7"
        "${q4t_xgrammar_SOURCE_DIR}/3rdparty/dlpack|bbd2f4d32427e548797929af08cfe2a9cbb3cf12")
      string(REPLACE "|" ";" pair "${spec}")
      list(GET pair 0 source)
      list(GET pair 1 expected)
      execute_process(COMMAND git -C "${source}" rev-parse HEAD
        RESULT_VARIABLE result OUTPUT_VARIABLE revision
        OUTPUT_STRIP_TRAILING_WHITESPACE)
      if(NOT result EQUAL 0 OR NOT revision STREQUAL expected)
        message(FATAL_ERROR "Unexpected structured-output dependency identity")
      endif()
      execute_process(COMMAND git -C "${source}" status --porcelain
        --untracked-files=all --ignore-submodules=none
        RESULT_VARIABLE result OUTPUT_VARIABLE dirty)
      if(NOT result EQUAL 0 OR NOT dirty STREQUAL "")
        message(FATAL_ERROR "Structured-output dependency must be clean")
      endif()
    endforeach()
    # The pinned upstream cmake/config.cmake sets a normal variable to ON,
    # shadowing -DXGRAMMAR_BUILD_PYTHON_BINDINGS=OFF. Its documented override
    # is a binary-root config.cmake. Refuse to overwrite any other owner's file.
    set(config "# Q4T managed XGrammar C++-only configuration\nset(XGRAMMAR_BUILD_PYTHON_BINDINGS OFF)\nset(XGRAMMAR_BUILD_CXX_TESTS OFF)\nset(XGRAMMAR_ENABLE_CPPTRACE OFF)\nset(XGRAMMAR_ENABLE_COVERAGE OFF)\nset(XGRAMMAR_ENABLE_INTERNAL_CHECK OFF)\n")
    set(config_path "${CMAKE_BINARY_DIR}/config.cmake")
    if(EXISTS "${config_path}")
      file(READ "${config_path}" existing)
      if(NOT existing STREQUAL config)
        message(FATAL_ERROR "Refusing to overwrite existing ${config_path}")
      endif()
    else()
      file(WRITE "${config_path}" "${config}")
    endif()
    add_subdirectory("${q4t_xgrammar_SOURCE_DIR}"
      "${q4t_xgrammar_BINARY_DIR}" EXCLUDE_FROM_ALL)
  endfunction()
  q4t_populate_xgrammar()
  include(${CMAKE_CURRENT_LIST_DIR}/RustRegex.cmake)
endif()

function(q4t_link_structured_output target)
  if(Q4T_ENABLE_XGRAMMAR)
    target_compile_definitions(${target} PRIVATE Q4T_ENABLE_XGRAMMAR=1)
    target_link_libraries(${target} PRIVATE xgrammar q4t_regex ${CMAKE_DL_LIBS})
  endif()
endfunction()
