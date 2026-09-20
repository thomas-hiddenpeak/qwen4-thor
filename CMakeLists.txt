# Qwen4-Thor — native C++23 (host + device) inference engine for
# Qwen3.8-Flash-Next (qwen4_exp) on Jetson AGX Thor (SM110a).
#
# CMake >= 4.0 is REQUIRED: CMAKE_CUDA_STANDARD 23 maps to nvcc --std=c++23
# only in CMake 4.x's NVIDIA compiler module (3.28/3.31 stop at CUDA20 and
# fail with 'requires the language dialect "CUDA23"'). On this machine:
#   pip install --user --break-system-packages cmake==4.4.3
cmake_minimum_required(VERSION 4.0)

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
# Both host (.cpp) and device (.cu) code are C++23, per the NVIDIA engineer
# recommendation: CUDA 13.3 fully supports C++23 on both sides, and the
# project migrates incrementally (existing .cu code is C++17-compatible, a
# subset of C++23, so it compiles unchanged; new code adopts C++23 features
# as they land).
#
# REQUIREMENT: host compiler must be GCC >= 14. GCC 13 makes nvcc silently
# IGNORE --std=c++23 ("not supported with the configured host compiler"),
# so device C++23 never actually engages. Configure with:
#   cmake -DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_CUDA_HOST_COMPILER=g++-14 ...
# (g++-14 from Ubuntu 24.04 apt; noble has no gcc-15).
#
# GCC 14.2 C++23 availability (measured 2026-09-19): std::print,
# std::expected, std::format, ranges, jthread, in-order designated
# initializers all work on host AND device (with --expt-relaxed-constexpr).
# NOT in GCC 14 (need GCC 15): std::mdspan, constexpr std::string.
# Device edge case: OUT-OF-ORDER designated initializers (C++23 P1967R3)
# are rejected by cudafe — use declaration order.
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_CUDA_STANDARD 23)
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
  src/model/streaming_topk.cu
  src/model/moe.cu
  src/model/ple_layer.cu
  src/model/decoder_layer.cu
  src/model/model_head.cu
  src/model/model.cu
  src/model/gemv.cu
  src/model/gemv_hc_inject.cu
  src/mtp/moe_bf16.cu
  src/mtp/mtp.cu
  src/vision/vision.cu
  src/vision/processor.cpp)
target_include_directories(q4t_model PUBLIC include third_party)
target_link_libraries(q4t_model PUBLIC q4t_io q4t_quant q4t_ple CUDA::cudart
                    CUDA::cublasLt)

# ---------------------------------------------------------------------------
# q4t_runtime — host-side runtime utilities (memory budget, OOM preflight).
# No CUDA dependency: pure host logic over /proc/meminfo + architecture math.
# ---------------------------------------------------------------------------
add_library(q4t_runtime STATIC src/runtime/memory_budget.cpp)
target_include_directories(q4t_runtime PUBLIC include)

# ---------------------------------------------------------------------------
# q4t_server — OpenAI-compatible HTTP server (POSIX sockets, no deps)
# ---------------------------------------------------------------------------
add_library(q4t_server STATIC src/server/chat_server.cpp)
target_include_directories(q4t_server PUBLIC include)
target_link_libraries(q4t_server PUBLIC q4t_model q4t_text q4t_io q4t_runtime
                                        CUDA::cudart)

# ---------------------------------------------------------------------------
# q4t — CLI executable
# ---------------------------------------------------------------------------
add_executable(q4t src/main.cpp)
target_include_directories(q4t PRIVATE include)
target_link_libraries(q4t PRIVATE q4t_model q4t_text q4t_ple q4t_quant
                                  q4t_server CUDA::cudart)

# ---------------------------------------------------------------------------
# q4t_bench_full_attn — full-attention (QSA) block benchmark (prefill tuning)
# ---------------------------------------------------------------------------
add_executable(q4t_bench_full_attn tools/bench_full_attn.cu)
target_include_directories(q4t_bench_full_attn PRIVATE include)
target_link_libraries(q4t_bench_full_attn PRIVATE q4t_model q4t_io
                                                  CUDA::cudart)

# q4t_sparse_gather_microbench — scatter-vs-ordered 1KB gather bandwidth
# probe (isolates the DRAM-locality lever for SparseAttentionKernel)
add_executable(q4t_sparse_gather_microbench tools/sparse_gather_microbench.cu)
target_link_libraries(q4t_sparse_gather_microbench PRIVATE CUDA::cudart)

# q4t_chunk_prefill_diag — one-shot vs chunked prefill logits comparison
# (262K chunked-prefill correctness diagnostic)
add_executable(q4t_chunk_prefill_diag tools/chunk_prefill_diag.cu)
target_include_directories(q4t_chunk_prefill_diag PRIVATE include)
target_link_libraries(q4t_chunk_prefill_diag PRIVATE q4t_model q4t_io
                                                     CUDA::cudart)

# q4t_fp8_gemv_proto — W8A16 FP8 GEMV feasibility spike (decode projection
# lever; standalone, synthetic weights, no model load)
add_executable(q4t_fp8_gemv_proto tools/fp8_gemv_proto.cu)
target_link_libraries(q4t_fp8_gemv_proto PRIVATE CUDA::cudart)

# q4t_small_m_gemm_bench — hand-written W8A16 small-M GEMM vs cuBLASLt BF16
# (extends FP8 to batched decode M=2..16; measures the crossover).
add_executable(q4t_small_m_gemm_bench tools/small_m_gemm_bench.cu)
target_link_libraries(q4t_small_m_gemm_bench PRIVATE CUDA::cudart CUDA::cublasLt)

# q4t_moe_grouped_bench — grouped FP4 MoE GEMM benefit probe (per-expert
# cuBLASLt vs weight-streaming floor; no model load)
add_executable(q4t_moe_grouped_bench tools/moe_grouped_bench.cu)
target_include_directories(q4t_moe_grouped_bench PRIVATE include)
target_link_libraries(q4t_moe_grouped_bench PRIVATE q4t_quant CUDA::cudart)

# q4t_gemm_roofline_bench — cuBLASLt BF16 TFLOPS on the model's real dense
# GEMM shapes across T (Pole 2 compute-roof ruler; no model load).
add_executable(q4t_gemm_roofline_bench tools/gemm_roofline_bench.cu)
target_link_libraries(q4t_gemm_roofline_bench PRIVATE CUDA::cudart
                                                     CUDA::cublasLt)

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
    tests/ple_working_memory_test.cpp
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
    tests/streaming_topk_test.cpp
    tests/model_decoder_layer_test.cpp
    tests/model_ple_layer_test.cpp
    tests/model_head_test.cpp
    tests/gemv_fp8_test.cpp
    tests/model_forward_test.cpp
    tests/model_moe_test.cpp
    tests/model_verify_multi_test.cpp
    tests/model_prefill_batch_test.cpp
    tests/model_mixed_batch_test.cpp
    tests/mtp_draft_test.cpp
    tests/mtp_speculative_test.cpp
    tests/mtp_spec_multi_test.cpp
    tests/vision_forward_test.cpp
    tests/model_vision_inject_test.cpp
    tests/vision_processor_test.cpp
    tests/video_processor_test.cpp
    tests/vision_e2e_test.cpp
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
