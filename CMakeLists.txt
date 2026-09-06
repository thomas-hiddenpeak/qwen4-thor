# Qwen4-Thor — native C++17/CUDA inference engine for
# Qwen3.8-Flash-Next (qwen4_exp) on Jetson AGX Thor (SM110a).
cmake_minimum_required(VERSION 3.24)

project(
  qwen4-thor
  VERSION 0.1.0
  DESCRIPTION "Qwen3.8-Flash-Next inference engine for Jetson AGX Thor"
  LANGUAGES CXX CUDA)

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
option(Q4T_BUILD_TESTS "Build unit tests" ON)

# CUDA architecture. Thor is SM110a. Overridable:
#   -DQ4T_CUDA_ARCHITECTURES=110a
if(NOT DEFINED Q4T_CUDA_ARCHITECTURES)
  set(Q4T_CUDA_ARCHITECTURES "110a" CACHE STRING "CUDA architecture")
endif()

# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_CUDA_STANDARD 17)
set(CMAKE_CUDA_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

set(CMAKE_CUDA_ARCHITECTURES "${Q4T_CUDA_ARCHITECTURES}")
set(CMAKE_CUDA_FLAGS
    "${CMAKE_CUDA_FLAGS} --expt-relaxed-constexpr -O3")

add_compile_options(-Wall -Wextra)

# CUDA toolkit headers/libraries for C++ sources that include CUDA runtime.
find_package(CUDAToolkit REQUIRED)

# ---------------------------------------------------------------------------
# liburing (PLE SSD Stream io_uring reader dependency)
# ---------------------------------------------------------------------------
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(LIBURING QUIET liburing)
endif()
if(LIBURING_FOUND)
  set(Q4T_LIBURING_DEFS Q4T_HAS_LIBURING=1)
  set(Q4T_LIBURING_INC ${LIBURING_INCLUDE_DIRS})
  set(Q4T_LIBURING_LIBS ${LIBURING_LINK_LIBRARIES})
  message(STATUS "liburing found: ${LIBURING_INCLUDE_DIRS}")
else()
  set(Q4T_LIBURING_DEFS "")
  set(Q4T_LIBURING_INC "")
  set(Q4T_LIBURING_LIBS "")
  message(STATUS "liburing not found — PLE io_uring reader disabled")
endif()

# ---------------------------------------------------------------------------
# q4t_ple — PLE n-gram hash + SSD stream (grows as modules land)
# ---------------------------------------------------------------------------
add_library(q4t_ple STATIC
  src/ple/ngram_hash.cpp
  src/ple/ngram_hash_derive.cpp
  src/ple/page_reader.cpp
  src/ple/fp8_convert.cu
  src/ple/ple_embedding.cpp)
target_include_directories(q4t_ple PUBLIC include)
target_compile_definitions(q4t_ple PRIVATE ${Q4T_LIBURING_DEFS})
if(Q4T_LIBURING_INC)
  target_include_directories(q4t_ple PRIVATE ${Q4T_LIBURING_INC})
endif()
if(Q4T_LIBURING_LIBS)
  target_link_libraries(q4t_ple PUBLIC ${Q4T_LIBURING_LIBS})
endif()
target_link_libraries(q4t_ple PUBLIC CUDA::cudart)

# ---------------------------------------------------------------------------
# q4t_io — file/config/tokenizer IO (safetensors, JSON)
# ---------------------------------------------------------------------------
add_library(q4t_io STATIC
  src/io/json.cpp
  src/io/safetensors.cpp
  src/io/model_config.cpp
  src/io/weight_loader.cpp)
target_include_directories(q4t_io PUBLIC include)
target_link_libraries(q4t_io PUBLIC CUDA::cudart)

# ---------------------------------------------------------------------------
# ICU (tokenizer NFC normalization + Unicode pre-tokenize regex)
# ---------------------------------------------------------------------------
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(ICU QUIET icu-uc icu-i18n)
endif()
if(ICU_FOUND)
  set(Q4T_HAS_ICU 1)
  message(STATUS "ICU found: ${ICU_VERSION} — tokenizer enabled")
else()
  set(Q4T_HAS_ICU 0)
  message(STATUS "ICU not found — tokenizer disabled")
endif()

# ---------------------------------------------------------------------------
# q4t_text — GPT-2 Byte-Level BPE tokenizer (ICU 74 for NFC + \p{L} regex)
# ---------------------------------------------------------------------------
if(Q4T_HAS_ICU)
  add_library(q4t_text STATIC src/text/tokenizer.cpp)
  target_include_directories(q4t_text PUBLIC include ${ICU_INCLUDE_DIRS})
  target_link_libraries(q4t_text PUBLIC q4t_io ${ICU_LINK_LIBRARIES})
endif()

# ---------------------------------------------------------------------------
# q4t_quant — NVFP4 quantization (e2m1/e4m3, swizzle, dequant, act-quant,
#             cuBLASLt W4A4 GEMM)
# ---------------------------------------------------------------------------
add_library(q4t_quant STATIC
  src/quant/dequant.cu
  src/quant/act_quant.cu
  src/quant/moe_weights.cpp
  src/quant/moe_gemm.cu
  src/quant/lt_cache.cpp)
target_include_directories(q4t_quant PUBLIC include)
target_link_libraries(q4t_quant PUBLIC q4t_io CUDA::cudart CUDA::cublasLt)

# ---------------------------------------------------------------------------
# q4t_model — model layer (hyper-connection backbone, attention, MoE)
# ---------------------------------------------------------------------------
add_library(q4t_model STATIC
  src/model/hyperconnection.cu
  src/model/linear_attention.cu
  src/model/full_attention.cu
  src/model/moe.cu
  src/model/ple_layer.cu
  src/model/decoder_layer.cu
  src/model/model_head.cu
  src/model/model.cu)
target_include_directories(q4t_model PUBLIC include)
target_link_libraries(q4t_model PUBLIC q4t_io q4t_quant q4t_ple CUDA::cudart
                    CUDA::cublasLt)

# ---------------------------------------------------------------------------
# q4t_server — OpenAI-compatible HTTP server (POSIX sockets, no deps)
# ---------------------------------------------------------------------------
add_library(q4t_server STATIC src/server/chat_server.cpp)
target_include_directories(q4t_server PUBLIC include)
target_link_libraries(q4t_server PUBLIC q4t_model q4t_text q4t_io
                                        CUDA::cudart)

# ---------------------------------------------------------------------------
# q4t — CLI executable
# ---------------------------------------------------------------------------
add_executable(q4t src/main.cpp)
target_include_directories(q4t PRIVATE include)
target_link_libraries(q4t PRIVATE q4t_model q4t_text q4t_ple q4t_quant
                                  q4t_server CUDA::cudart)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
if(Q4T_BUILD_TESTS)
  enable_testing()
  set(Q4T_TEST_SOURCES
    tests/ple_ngram_hash_test.cpp
    tests/ple_page_reader_test.cpp
    tests/ple_fp8_convert_test.cpp
    tests/ple_e2e_gather_test.cpp
    tests/io_json_test.cpp
    tests/io_safetensors_test.cpp
    tests/io_model_config_test.cpp
    tests/io_weight_loader_test.cpp
    tests/quant_fp4_test.cpp
    tests/quant_moe_load_test.cpp
    tests/quant_moe_gemm_test.cpp
    tests/model_hyperconnection_test.cpp
    tests/model_linear_attention_test.cpp
    tests/model_full_attention_test.cpp
    tests/model_decoder_layer_test.cpp
    tests/model_ple_layer_test.cpp
    tests/model_head_test.cpp
    tests/model_forward_test.cpp
    tests/model_moe_test.cpp
    tests/test_main.cpp)
  set(Q4T_TEST_LIBS q4t_ple q4t_io q4t_quant q4t_model)
  if(Q4T_HAS_ICU)
    list(APPEND Q4T_TEST_SOURCES tests/text_tokenizer_test.cpp)
    list(APPEND Q4T_TEST_LIBS q4t_text)
  endif()
  add_executable(q4t_tests ${Q4T_TEST_SOURCES})
  target_include_directories(q4t_tests PRIVATE include)
  target_link_libraries(q4t_tests PRIVATE ${Q4T_TEST_LIBS})
  add_test(NAME q4t_tests COMMAND q4t_tests)
endif()
