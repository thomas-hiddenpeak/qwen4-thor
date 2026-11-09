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
option(Q4T_STARTUP_RESOURCE_PLAN
       "Build the experimental config-derived startup reservation" OFF)

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

# The service Guardian's process control remains independently CPU-buildable.
include(cmake/GuardianProcess.cmake)
include(cmake/NativeIngress.cmake)

# CUDA toolkit headers/libraries for C++ sources that include CUDA runtime.
find_package(CUDAToolkit REQUIRED)
include(cmake/RecoveryProbe.cmake)

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
target_link_libraries(q4t_ple PUBLIC CUDA::cudart ${CMAKE_DL_LIBS})

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
  src/quant/moe_decode.cu
  src/quant/moe_residency.cpp
  src/quant/lt_cache.cpp)
target_include_directories(q4t_quant PUBLIC include)
target_link_libraries(q4t_quant PUBLIC q4t_io CUDA::cudart CUDA::cublasLt)

# ---------------------------------------------------------------------------
# q4t_model — model layer (hyper-connection backbone, attention, MoE)
# ---------------------------------------------------------------------------
add_library(q4t_trace STATIC
  src/trace/router_trace.cpp
  src/trace/router_collector.cpp src/trace/sha256.cpp)
target_include_directories(q4t_trace PUBLIC include)
target_link_libraries(q4t_trace PUBLIC CUDA::cudart)
target_link_libraries(q4t_quant PUBLIC q4t_trace)
add_executable(q4t_router_trace_check tools/trace/router_trace_check.cpp
  src/trace/router_trace.cpp)
target_include_directories(q4t_router_trace_check PRIVATE include)

add_library(q4t_model STATIC
  src/model/hyperconnection.cu
  src/model/linear_attention.cu
  src/model/full_attention.cu
  src/model/qsa_decode.cu
  src/model/streaming_topk.cu
  src/model/decode_topk.cu
  src/model/short_topk.cu
  src/model/indexer_decode.cu
  src/model/moe.cu
  src/model/ple_layer.cu
  src/model/decoder_layer.cu
  src/model/model_head.cu
  src/model/model.cu
  src/model/model_owner.cpp
  src/model/decode_metadata_upload.cpp
  src/model/metadata_upload_batch.cpp
  src/model/gemv.cu
  src/model/gemv_hc_inject.cu
  src/model/small_m_gemm.cu
  src/mtp/moe_bf16.cu
  src/mtp/mtp.cu
  src/mtp/subset_head.cpp
  src/mtp/suffix_drafter.cpp
  src/vision/vision.cu
  src/vision/media_plan.cpp
  src/vision/processor.cpp)
target_include_directories(q4t_model PUBLIC include third_party)
target_link_libraries(q4t_model PUBLIC q4t_trace q4t_io q4t_quant q4t_ple CUDA::cudart
                    CUDA::cublasLt)

# Standalone A1 diagnostic: never linked by production q4t_model/serve.
add_library(q4t_moe_replay_gpu STATIC EXCLUDE_FROM_ALL
  tools/verify/moe_replay_gpu.cu)
target_link_libraries(q4t_moe_replay_gpu PUBLIC q4t_model)
add_library(q4t_moe_replay_io STATIC EXCLUDE_FROM_ALL
  tools/verify/moe_replay_io.cpp)
target_link_libraries(q4t_moe_replay_io PUBLIC q4t_trace)
add_executable(q4t_moe_replay_host_tests EXCLUDE_FROM_ALL
  tests/test_main.cpp tests/moe_replay_contract_test.cpp
  tests/moe_replay_io_test.cpp)
target_link_libraries(q4t_moe_replay_host_tests PRIVATE q4t_moe_replay_io)
add_executable(q4t_moe_replay EXCLUDE_FROM_ALL
  tools/verify/moe_replay/main.cpp)
target_link_libraries(q4t_moe_replay PRIVATE q4t_moe_replay_gpu
  q4t_moe_replay_io)
if(Q4T_HAS_ICU)
  add_executable(q4t_moe_real_capture EXCLUDE_FROM_ALL
    tools/verify/moe_replay_real/capture.cpp)
  target_link_libraries(q4t_moe_real_capture PRIVATE q4t_moe_replay_gpu
    q4t_moe_replay_io q4t_text)
  target_link_options(q4t_moe_real_capture PRIVATE
    "-Wl,--wrap=_ZN3q4t5model10MoEForwardEPKtRKNS_5quant15MoEWeightLayoutERKNS0_15MoEExtraWeightsEPtiiPvmSB_mP11CUstream_stPNS_5trace15RouterCollectorEiPKNS3_12MoEResidencyEPNS0_21MoEForwardDiagnosticsE")
  add_executable(q4t_upstream_gdn EXCLUDE_FROM_ALL
    tools/verify/upstream_first_fork/gdn_replay.cu)
  target_link_libraries(q4t_upstream_gdn PRIVATE q4t_model)
  add_executable(q4t_upstream_projection EXCLUDE_FROM_ALL
    tools/verify/upstream_first_fork/projection.cpp)
  target_link_libraries(q4t_upstream_projection PRIVATE q4t_moe_replay_gpu
    q4t_moe_replay_io q4t_text)
  target_link_options(q4t_upstream_projection PRIVATE
    "-Wl,--wrap=_ZN3q4t5model10MoEForwardEPKtRKNS_5quant15MoEWeightLayoutERKNS0_15MoEExtraWeightsEPtiiPvmSB_mP11CUstream_stPNS_5trace15RouterCollectorEiPKNS3_12MoEResidencyEPNS0_21MoEForwardDiagnosticsE")
  add_executable(q4t_upstream_first_fork EXCLUDE_FROM_ALL
    tools/verify/upstream_first_fork/capture.cpp)
  target_link_libraries(q4t_upstream_first_fork PRIVATE q4t_moe_replay_gpu
    q4t_moe_replay_io q4t_text)
  target_link_options(q4t_upstream_first_fork PRIVATE
    "-Wl,--wrap=_ZN3q4t5model10MoEForwardEPKtRKNS_5quant15MoEWeightLayoutERKNS0_15MoEExtraWeightsEPtiiPvmSB_mP11CUstream_stPNS_5trace15RouterCollectorEiPKNS3_12MoEResidencyEPNS0_21MoEForwardDiagnosticsE")
  target_link_options(q4t_upstream_first_fork PRIVATE
    LINKER:--wrap=_ZN3q4t5model19DecoderLayerForwardERKNS0_12DecoderLayerEPKtS5_PtPKiS8_iPvmP11CUstream_stPfS6_iiS8_S8_iS6_PKNS0_11RaggedBatchEiPNS_5trace15RouterCollectorEiiSC_S6_S6_
    LINKER:--wrap=_ZN3q4t5model7Bf16GevEPKtS2_PtiifP11CUstream_st
    LINKER:--wrap=_ZN3q4t5model14Bf16SmallMGemmEPKtS2_PtiiifP11CUstream_st
    LINKER:--wrap=cublasLtMatmul)

endif()



# ---------------------------------------------------------------------------
# q4t_runtime — host-side runtime utilities (memory budget, OOM preflight).
# No CUDA dependency: pure host logic over /proc/meminfo + architecture math.
# ---------------------------------------------------------------------------
add_library(q4t_runtime STATIC src/runtime/memory_budget.cpp
  src/runtime/system_memory.cpp)
target_include_directories(q4t_runtime PUBLIC include)

# ---------------------------------------------------------------------------
# q4t_server — native HTTP/application server with isolated optional backends
# ---------------------------------------------------------------------------
include(cmake/ChatProtocolIdentity.cmake)
add_library(q4t_server STATIC src/server/chat_server.cpp
  src/server/object_store.cpp
  src/server/object_worker.cpp
  src/server/object_retention.cpp
  src/server/multipart.cpp
  src/server/binary_store.cpp
  src/server/files.cpp
  src/server/files_http.cpp
  src/server/responses_state.cpp
  src/server/responses_execution.cpp
  src/server/responses_execution_compaction.cpp
  src/server/responses_jobs.cpp
  src/server/responses_owner.cpp
  src/server/progress_transport.cpp
  src/server/responses_api.cpp
  src/server/application_query.cpp
  src/server/conversation_state.cpp
  src/server/conversation_api.cpp
  src/server/prompt_template.cpp
  src/server/prompt_cache_policy.cpp
  src/server/prompt_cache_diagnostics.cpp
  src/server/prompt_registry.cpp
  src/server/prompt_api.cpp
  src/server/chat_storage.cpp
  src/server/chat_storage_options.cpp
  src/server/responses_item_shape.cpp
  src/server/model_catalog.cpp
  src/server/client_policy.cpp
  src/server/client_policy_admin.cpp
  src/server/client_rate_state.cpp
  src/server/client_request.cpp
  src/server/chat_device_memory.cpp
  src/server/chat_health.cpp
  src/server/memory_admission.cpp
  src/server/chat_memory.cpp
  src/server/chat_capabilities.cpp
  src/server/server_options.cpp
  src/server/object_store_options.cpp
  src/server/chat_http.cpp
  src/server/http_response.cpp
  src/server/chat_response_parser.cpp
  src/server/thinking_budget.cpp
  src/server/text_stop.cpp
  src/server/reasoning_options.cpp
  src/server/output_schema.cpp
  src/server/schema_decimal.cpp
  src/server/schema_graph.cpp
  src/server/schema_prefix.cpp
  src/server/schema_receipt.cpp
  src/server/schema_token_guard.cpp
  src/server/schema_number_grammar.cpp
  src/server/schema_grammar.cpp
  src/server/schema_string.cpp
  src/server/schema_string_grammar.cpp
  src/server/schema_idna.cpp
  src/server/output_constraints.cpp
  src/server/grammar_process.cpp
  src/server/grammar_worker.cpp
  src/server/structured_decoder.cpp
  src/server/chat_input.cpp
  src/server/media_admission.cpp
  src/server/chat_prompt.cpp
  src/server/chat_plan.cpp
  src/server/chat_preflight.cpp
  src/server/chat_input_tokens.cpp
  src/server/chat_metrics.cpp
  src/server/chat_admission.cpp
  src/server/chat_scheduler.cpp
  src/server/chat_generation.cpp
  src/server/chat_responses.cpp
  src/server/session_cache.cpp
  src/server/cache_io.cpp
  src/server/cache_worker.cpp
  src/server/session_state.cpp
  src/server/http_request.cpp
  src/server/input_contract.cpp
  src/server/chat_contract.cpp
  src/server/sampling.cpp
  src/server/prediction.cpp
  src/server/custom_tools.cpp
  src/server/custom_grammar.cpp
  src/server/custom_regex.cpp
  src/server/legacy_tools.cpp
  src/server/token_logprobs.cpp
    src/server/completion_plan.cpp
    src/server/completion_response.cpp
    src/server/responses_output.cpp
    src/server/responses_input.cpp
    src/server/responses_plan.cpp
    src/server/responses_tokens.cpp
    src/server/responses_summary.cpp
    src/server/responses_reasoning.cpp
    src/server/access_program.cpp
    src/server/moderation.cpp
    src/server/responses_execution_moderation.cpp
  src/server/chat_choices.cpp
  src/server/chat_tool_history.cpp
                             src/server/chat_template.cpp)
target_include_directories(q4t_server PUBLIC include)
target_include_directories(q4t_server PRIVATE ${Q4T_CHAT_PROTOCOL_INCLUDE_DIR})
q4t_link_structured_output(q4t_server)
include(cmake/NativeCrypto.cmake)
target_sources(q4t_server PRIVATE src/server/native_crypto.cpp
  src/server/state_cipher.cpp src/server/responses_crypto.cpp
  src/server/responses_compaction.cpp
  src/server/state_key_admin.cpp src/server/state_key_cli.cpp
  src/server/state_retention_cli.cpp)
q4t_link_native_crypto(q4t_server)
option(Q4T_SERVE_FAULT_INJECTION "Validation-only serving fault hooks" OFF)
if(Q4T_SERVE_FAULT_INJECTION)
  target_compile_definitions(q4t_server PRIVATE Q4T_SERVE_FAULT_INJECTION=1)
endif()
target_link_libraries(q4t_server PUBLIC q4t_model q4t_text q4t_io q4t_runtime
                                        CUDA::cudart q4t_guardian_process)

# ---------------------------------------------------------------------------
# q4t — CLI executable
# ---------------------------------------------------------------------------
include(cmake/ServiceCLI.cmake)
add_executable(q4t src/main.cpp)
q4t_link_structured_output(q4t)
target_include_directories(q4t PRIVATE include)
target_link_libraries(q4t PRIVATE q4t_model q4t_text q4t_ple q4t_quant
                                  q4t_server q4t_service_cli CUDA::cudart)

add_library(q4t_startup_plan STATIC
  src/runtime/startup_plan.cpp
  src/runtime/startup_model_plan.cpp
  src/runtime/startup_mtp_plan.cpp)
target_include_directories(q4t_startup_plan PUBLIC include)
target_link_libraries(q4t_startup_plan PUBLIC q4t_model q4t_runtime)
target_link_libraries(q4t_server PUBLIC q4t_startup_plan)

if(Q4T_STARTUP_RESOURCE_PLAN)
  target_compile_definitions(q4t_server PRIVATE Q4T_STARTUP_RESOURCE_PLAN=1)
  target_sources(q4t PRIVATE tools/verify/startup_plan/reservation.cpp)
  target_link_libraries(q4t PRIVATE ${CMAKE_DL_LIBS})
  foreach(api cudaMalloc cudaFree cudaHostAlloc cudaMallocHost cudaFreeHost
              cudaMallocAsync cudaFreeAsync cudaStreamSynchronize
              cudaDeviceSynchronize cudaEventRecord cudaEventSynchronize)
    target_link_options(q4t PRIVATE "LINKER:--wrap=${api}")
  endforeach()
else()
  # PUBLIC keeps ChatServer's member layout identical in all translation units.
  target_compile_definitions(q4t_server PUBLIC Q4T_STARTUP_RESERVATION=1)
  target_sources(q4t PRIVATE src/runtime/startup_reservation.cpp
                            src/runtime/cuda_allocation_quota.cpp)
  # No per-operation tracing. Unknown cleanup/completion fails closed even
  # with --startup-plan off; existing handled serve releases retain errors.
  include(cmake/CudaCleanupBoundary.cmake)
  q4t_link_cuda_cleanup(q4t)

  add_executable(q4t_cleanup_probe EXCLUDE_FROM_ALL
    tools/serve_backend_cleanup/cleanup_probe.cpp
    src/runtime/startup_reservation.cpp src/runtime/cuda_allocation_quota.cpp)
  target_include_directories(q4t_cleanup_probe PRIVATE include)
  target_link_libraries(q4t_cleanup_probe PRIVATE q4t_startup_plan CUDA::cudart)
  q4t_link_cuda_cleanup(q4t_cleanup_probe)
endif()

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

# q4t_small_m_gemm_v2_bench — SM110a-tuned hand-written BF16 small-M GEMM
# (smem-staged x, f32x2 FMA, MAXM templated) vs cuBLASLt on the model's real
# dense projection shapes; L2-defeated weight pool (DRAM regime).
add_executable(q4t_small_m_gemm_v2_bench tools/small_m_gemm_v2_bench.cu)
target_link_libraries(q4t_small_m_gemm_v2_bench PRIVATE CUDA::cudart CUDA::cublasLt)

# q4t_small_m_gemm_v3_bench — contiguous-vs-scattered column mapping variants
# (bit-identical to production scattered) on the real verify (T=4) shapes.
add_executable(q4t_small_m_gemm_v3_bench tools/small_m_gemm_v3_bench.cu)
target_link_libraries(q4t_small_m_gemm_v3_bench PRIVATE CUDA::cudart CUDA::cublasLt)

# q4t_hc_inject_bench — HC inject GEMV variants (T=4 multi-row / T=1 decode),
# L2-defeated per-call timing.
add_executable(q4t_hc_inject_bench tools/hc_inject_bench.cu)
target_link_libraries(q4t_hc_inject_bench PRIVATE CUDA::cudart)

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

# CPU-only request planner probe; linking the runner does not execute CUDA.
add_executable(q4t_preflight_probe EXCLUDE_FROM_ALL
  tools/serve_preflight/prompt_probe.cpp)
target_link_libraries(q4t_preflight_probe PRIVATE q4t_server)
target_compile_options(q4t_preflight_probe PRIVATE -Wall -Wextra -Werror)
q4t_link_structured_output(q4t_preflight_probe)

# CPU-only request shape/semantic probe, using the current server identity.
add_executable(q4t_contract_probe EXCLUDE_FROM_ALL
  tools/serve_protocol/contract_probe.cpp)
target_link_libraries(q4t_contract_probe PRIVATE q4t_server)
target_compile_options(q4t_contract_probe PRIVATE -Wall -Wextra -Werror)
q4t_link_structured_output(q4t_contract_probe)

# Actual preparation/HTTP handlers; no model load. Run under gpu_exclusive.
add_executable(q4t_prepare_lock_probe EXCLUDE_FROM_ALL
  tools/serve_prepare_lock/probe.cpp)
target_link_libraries(q4t_prepare_lock_probe PRIVATE q4t_server)
target_compile_options(q4t_prepare_lock_probe PRIVATE -Wall -Wextra -Werror)
q4t_link_structured_output(q4t_prepare_lock_probe)
if(NOT Q4T_STARTUP_RESOURCE_PLAN)
  target_sources(q4t_prepare_lock_probe PRIVATE
    src/runtime/startup_reservation.cpp src/runtime/cuda_allocation_quota.cpp)
  q4t_link_cuda_cleanup(q4t_prepare_lock_probe)
endif()

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
    tests/runtime_memory_budget_test.cpp
    tests/server_memory_admission_test.cpp
    src/server/memory_admission.cpp
    tests/model_sequence_test.cpp
    tests/server_http_request_test.cpp
    tests/server_http_framing_test.cpp
    tests/server_http_session_test.cpp
    tests/server_http_observation_test.cpp
    tests/server_chat_contract_test.cpp
    src/server/chat_capabilities.cpp
    tests/server_chat_capabilities_test.cpp
    src/server/chat_tool_history.cpp
    src/server/chat_template.cpp
    tests/server_chat_tool_history_test.cpp
    src/server/chat_response_parser.cpp
    src/server/thinking_budget.cpp
    src/server/text_stop.cpp
    src/server/reasoning_options.cpp
    tests/server_chat_response_parser_test.cpp
    tests/server_thinking_budget_test.cpp
    tests/server_text_stop_test.cpp
    src/server/output_schema.cpp
    src/server/schema_decimal.cpp
    src/server/schema_graph.cpp
    src/server/schema_prefix.cpp
    src/server/schema_receipt.cpp
    src/server/schema_token_guard.cpp
    src/server/schema_number_grammar.cpp
    src/server/schema_grammar.cpp
    src/server/schema_string.cpp
    src/server/schema_string_grammar.cpp
    src/server/schema_idna.cpp
    tests/server_schema_graph_test.cpp
    tests/server_schema_prefix_test.cpp
    tests/server_schema_string_test.cpp
    src/server/output_constraints.cpp
    src/server/grammar_process.cpp
    src/server/grammar_worker.cpp
    src/server/structured_decoder.cpp
    tests/server_output_constraints_test.cpp
    src/server/input_contract.cpp
    src/server/chat_contract.cpp
    src/server/chat_storage_options.cpp
    src/server/sampling.cpp
    src/server/prediction.cpp
    src/server/custom_tools.cpp
    src/server/custom_grammar.cpp
    src/server/custom_regex.cpp
    src/server/legacy_tools.cpp
    src/server/token_logprobs.cpp
    src/server/completion_plan.cpp
    src/server/completion_response.cpp
    src/server/responses_output.cpp
    src/server/responses_input.cpp
    src/server/responses_plan.cpp
    src/server/responses_tokens.cpp
    src/server/responses_summary.cpp
    src/server/responses_reasoning.cpp
    src/server/access_program.cpp
    src/server/moderation.cpp
    src/server/responses_execution_moderation.cpp
    src/server/chat_choices.cpp
    src/server/model_catalog.cpp
    tests/server_sampling_test.cpp
    tests/server_completion_test.cpp
    tests/server_responses_output_test.cpp
    tests/server_responses_input_test.cpp
    tests/server_responses_plan_test.cpp
    tests/server_responses_tokens_test.cpp
    tests/server_responses_reasoning_test.cpp
    tests/server_access_program_test.cpp
    tests/server_moderation_test.cpp
    tests/server_responses_context_test.cpp
    tests/server_prediction_test.cpp
    tests/server_legacy_tools_test.cpp
    tests/server_custom_tools_test.cpp
    tests/server_chat_sampling_response_test.cpp
    src/server/chat_prompt.cpp
    src/server/media_admission.cpp
    src/server/chat_plan.cpp
    tests/server_media_admission_test.cpp
    tests/server_chat_plan_test.cpp
    tests/server_sequence_admission_test.cpp
    src/server/server_options.cpp
    src/server/object_store_options.cpp
    src/server/http_response.cpp
    tests/server_http_response_test.cpp
    src/server/http_request.cpp
    tests/io_safetensors_test.cpp
    tests/io_model_config_test.cpp
    tests/io_weight_loader_test.cpp
    tests/quant_fp4_test.cpp
    tests/quant_moe_load_test.cpp
    tests/quant_moe_gemm_test.cpp
    tests/quant_moe_residency_test.cpp
    tests/moe_decode_grouped_test.cpp
    tests/model_hyperconnection_test.cpp
    tests/model_linear_attention_test.cpp
    tests/model_full_attention_test.cpp
    tests/agent_attention_layout_test.cpp
    tests/streaming_topk_test.cpp
    tests/decode_topk_test.cpp
    tests/model_decoder_layer_test.cpp
    tests/model_ple_layer_test.cpp
    tests/model_head_test.cpp
    tests/gemv_fp8_test.cpp
    tests/gemm_t1_vs_tk1_test.cpp
    tests/model_forward_test.cpp
    tests/moe_replay_contract_test.cpp
    tests/model_moe_test.cpp
    tests/model_verify_multi_test.cpp
    tests/model_verify_ckpt_test.cpp
    tests/model_verify_ckpt_samepath_test.cpp
    tests/model_prefill_batch_test.cpp
    tests/model_mixed_batch_test.cpp
    tests/mtp_position_metadata_test.cpp
    tests/mtp_draft_test.cpp
    tests/mtp_speculative_test.cpp
    tests/mtp_spec_multi_test.cpp
    tests/mtp_spec_accept_states_test.cpp
    tests/mtp_suffix_test.cpp
    tests/mtp_writer_row0_test.cpp
    tests/mem_alloc_probe_test.cpp
    tests/model_decode_rope_test.cpp
    tests/mtp_checkpoint_layout_test.cpp
    tests/mtp_path_triangle_test.cpp
    tests/vision_forward_test.cpp
    tests/model_vision_inject_test.cpp
    tests/vision_processor_test.cpp
    tests/video_processor_test.cpp
    tests/vision_e2e_test.cpp
    tests/test_main.cpp)
  set(Q4T_TEST_LIBS q4t_ple q4t_io q4t_quant q4t_model q4t_runtime)
  if(Q4T_HAS_ICU)
    list(APPEND Q4T_TEST_SOURCES tests/text_tokenizer_test.cpp
                                 tests/mtp_verify_numerics_test.cpp
                                 tests/mtp_sixcell_test.cpp)
    list(APPEND Q4T_TEST_LIBS q4t_text)
  endif()
  add_executable(q4t_agent_capacity EXCLUDE_FROM_ALL
    tools/mtp_suffix_agent/capacity_probe.cpp)
  target_include_directories(q4t_agent_capacity PRIVATE include)
  target_link_libraries(q4t_agent_capacity PRIVATE ${Q4T_TEST_LIBS})

  add_executable(q4t_tests ${Q4T_TEST_SOURCES})
  add_executable(q4t_vision_tower_contract EXCLUDE_FROM_ALL
    tools/vision_service/tower_probe.cpp)
  target_include_directories(q4t_vision_tower_contract PRIVATE include)
  target_link_libraries(q4t_vision_tower_contract PRIVATE ${Q4T_TEST_LIBS})
  target_compile_options(q4t_vision_tower_contract PRIVATE -Wall -Wextra -Werror)
  q4t_link_structured_output(q4t_tests)
  target_sources(q4t_tests PRIVATE src/server/native_crypto.cpp
    tests/server_native_crypto_test.cpp)
  q4t_link_native_crypto(q4t_tests)
  if(Q4T_ENABLE_XGRAMMAR)
    target_sources(q4t_tests PRIVATE tests/server_structured_decoder_test.cpp
      tests/server_schema_language_test.cpp tests/server_schema_grammar_test.cpp
      tests/server_schema_string_grammar_test.cpp)
    list(APPEND Q4T_TEST_SOURCES tests/server_schema_token_guard_test.cpp)
  endif()
  target_include_directories(q4t_tests PRIVATE include)
  target_include_directories(q4t_tests PRIVATE ${Q4T_CHAT_PROTOCOL_INCLUDE_DIR})
  target_link_libraries(q4t_tests PRIVATE ${Q4T_TEST_LIBS} ${CMAKE_DL_LIBS})
  add_test(NAME q4t_tests COMMAND q4t_tests)

  # These research replays require retained private raw input or a caller-
  # supplied new evidence directory. Keep their original assertions and run
  # them only through an explicit target with the recorded fixture protocol.
  foreach(fixture IN ITEMS mtp_projection_path mtp_moe_path)
    add_executable(q4t_${fixture}_diagnostic EXCLUDE_FROM_ALL
      tests/test_main.cpp tests/${fixture}_test.cpp)
    target_include_directories(q4t_${fixture}_diagnostic PRIVATE include)
    target_link_libraries(q4t_${fixture}_diagnostic PRIVATE ${Q4T_TEST_LIBS})
  endforeach()

  # Three frozen HC replays explain the old first cross-shape fork. Captures
  # wrap real projection imports only in this explicit diagnostic executable.
  add_executable(q4t_mtp_hc_first_fork EXCLUDE_FROM_ALL
    tests/test_main.cpp tests/mtp_hc_first_fork_test.cpp)
  target_include_directories(q4t_mtp_hc_first_fork PRIVATE include)
  target_link_libraries(q4t_mtp_hc_first_fork PRIVATE ${Q4T_TEST_LIBS})
  target_link_options(q4t_mtp_hc_first_fork PRIVATE
    LINKER:--wrap=_ZN3q4t5model7Bf16GevEPKtS2_PtiifP11CUstream_st
    LINKER:--wrap=cublasLtMatmul)


  # Shared MTP ownership contract retained across verifier replacement.
  add_executable(q4t_mtp_lifecycle tests/mtp_lifecycle_test.cpp)
  target_include_directories(q4t_mtp_lifecycle PRIVATE include)
  target_link_libraries(q4t_mtp_lifecycle PRIVATE ${Q4T_TEST_LIBS})
  target_link_options(q4t_mtp_lifecycle PRIVATE
    LINKER:--wrap=cudaMalloc LINKER:--wrap=cudaFree
    LINKER:--wrap=cudaStreamSynchronize)

  add_executable(q4t_vision_contract EXCLUDE_FROM_ALL
    tools/serve_media_failure/vision_contract.cpp)
  target_include_directories(q4t_vision_contract PRIVATE include)
  target_link_libraries(q4t_vision_contract PRIVATE ${Q4T_TEST_LIBS})
  target_link_options(q4t_vision_contract PRIVATE
    LINKER:--wrap=cudaMalloc LINKER:--wrap=cudaFree
    LINKER:--wrap=cudaMemcpy LINKER:--wrap=cudaMemcpyAsync
    LINKER:--wrap=cudaStreamSynchronize LINKER:--wrap=_Znwm)
  add_executable(q4t_vision_upload EXCLUDE_FROM_ALL
    tools/verify/inference_metadata/vision_load_test.cpp)
  target_include_directories(q4t_vision_upload PRIVATE include)
  target_link_libraries(q4t_vision_upload PRIVATE ${Q4T_TEST_LIBS})
  target_link_options(q4t_vision_upload PRIVATE LINKER:--wrap=cudaMemcpyAsync)

  add_executable(q4t_host_memory_contract EXCLUDE_FROM_ALL
    tools/serve_media_failure/host_contract.cpp src/server/http_response.cpp
    src/vision/media_plan.cpp
    src/vision/processor.cpp)
  target_include_directories(q4t_host_memory_contract PRIVATE include third_party)
  target_link_libraries(q4t_host_memory_contract PRIVATE Threads::Threads)
  target_link_options(q4t_host_memory_contract PRIVATE LINKER:--wrap=malloc
    LINKER:--wrap=realloc LINKER:--wrap=free LINKER:--wrap=send)

  add_executable(q4t_progress_media_tests EXCLUDE_FROM_ALL
    tests/test_main.cpp tests/vision_progress_test.cpp
    src/vision/media_plan.cpp src/vision/processor.cpp)
  target_include_directories(q4t_progress_media_tests PRIVATE include third_party)
  target_compile_options(q4t_progress_media_tests PRIVATE -Wall -Wextra -Werror)
  target_link_libraries(q4t_progress_media_tests PRIVATE Threads::Threads)

  # Repeatable real-CUDA copy contract; no checkpoint or private input.
  add_executable(q4t_mtp_position_copy
    tests/mtp_position_copy_test.cpp)
  target_include_directories(q4t_mtp_position_copy PRIVATE include)
  target_link_libraries(q4t_mtp_position_copy PRIVATE CUDA::cudart)
  add_test(NAME q4t_mtp_position_copy COMMAND q4t_mtp_position_copy)
  set_tests_properties(q4t_mtp_position_copy PROPERTIES
    TIMEOUT 180 LABELS cuda RESOURCE_LOCK q4t_cuda)


endif()

# Host-only release-contract slice; the exact required list rejects absent
# tokenizer registration when ICU is unavailable. No CUDA/model GPU tests run.
if(Q4T_BUILD_TESTS)
  include(cmake/HostTestSources.cmake)
  find_package(Threads REQUIRED)
  add_executable(q4t_host_tests ${Q4T_HOST_TEST_SOURCES})
  target_compile_definitions(q4t_host_tests PRIVATE Q4T_GRAMMAR_PROCESS_TESTS=1)
  q4t_link_structured_output(q4t_host_tests)
  q4t_link_native_crypto(q4t_host_tests)
  target_include_directories(q4t_host_tests PRIVATE include)
  target_include_directories(q4t_host_tests PRIVATE ${Q4T_CHAT_PROTOCOL_INCLUDE_DIR})
  target_link_libraries(q4t_host_tests PRIVATE q4t_io Threads::Threads ${CMAKE_DL_LIBS})
  if(Q4T_HAS_ICU)
    target_sources(q4t_host_tests PRIVATE tests/text_tokenizer_test.cpp)
    target_link_libraries(q4t_host_tests PRIVATE q4t_text)
    add_executable(q4t_http_lifecycle_tests
      tools/serve_reliability/http_lifecycle_test.cpp tests/test_main.cpp
      tools/serve_reliability/admission_contract_test.cpp
      src/runtime/system_memory.cpp src/server/memory_admission.cpp
      src/server/chat_memory.cpp
      src/server/chat_device_memory.cpp
      src/server/chat_health.cpp
      src/server/chat_admission.cpp
      src/server/grammar_process.cpp
      src/server/chat_http.cpp src/server/http_request.cpp
      src/server/client_policy.cpp src/server/client_rate_state.cpp
      src/server/client_request.cpp
      src/trace/sha256.cpp
      src/server/input_contract.cpp src/server/output_schema.cpp
      src/server/schema_graph.cpp src/server/schema_decimal.cpp
      src/server/custom_tools.cpp
      src/server/http_response.cpp src/trace/router_trace.cpp)
    target_include_directories(q4t_http_lifecycle_tests PRIVATE
      include ${CUDAToolkit_INCLUDE_DIRS})
    target_compile_definitions(q4t_http_lifecycle_tests PRIVATE
      Q4T_SERVE_FAULT_INJECTION=1)
    target_link_libraries(q4t_http_lifecycle_tests PRIVATE
      q4t_io q4t_text Threads::Threads)
    add_test(NAME q4t_http_lifecycle COMMAND q4t_http_lifecycle_tests)
    set_tests_properties(q4t_http_lifecycle PROPERTIES TIMEOUT 30)
  endif()
  add_test(NAME q4t_host_contracts COMMAND q4t_host_tests --required-list
    ${CMAKE_CURRENT_SOURCE_DIR}/tools/release/required_host_tests.txt)
  add_test(NAME q4t_scheduler_submission_contracts
    COMMAND q4t_host_tests scheduler_submission_)
  set_tests_properties(q4t_scheduler_submission_contracts PROPERTIES
    TIMEOUT 180)
endif()

# q4t_moe_gemm_bench — per-row grouped NVFP4 GEMM (verify T=4 shapes) vs
# DRAM floor; characterizes the moe_decode.cu grouped path per call.
add_executable(q4t_moe_gemm_bench tools/moe_gemm_bench.cu)
target_include_directories(q4t_moe_gemm_bench PRIVATE include)
target_link_libraries(q4t_moe_gemm_bench PRIVATE q4t_quant CUDA::cudart CUDA::cublasLt)

# q4t_moe_gemm_grouped_bench — expert-grouped vs per-row grouped NVFP4 GEMM
# (verify T=4 shapes); characterizes the weight-traffic headroom of grouping
# (token,slot) rows by expert before the grouped GEMM.
add_executable(q4t_moe_gemm_grouped_bench tools/moe_gemm_grouped_bench.cu)
target_include_directories(q4t_moe_gemm_grouped_bench PRIVATE include)
target_link_libraries(q4t_moe_gemm_grouped_bench PRIVATE q4t_quant CUDA::cudart CUDA::cublasLt)

# Independent parent/candidate metadata lifecycle fixture; never a serve hook.
add_executable(q4t_load_source_test EXCLUDE_FROM_ALL
  tools/verify/inference_metadata/load_source_test.cpp)
target_link_libraries(q4t_load_source_test PRIVATE q4t_io)
target_link_options(q4t_load_source_test PRIVATE
  LINKER:--wrap=cudaMemcpyAsync LINKER:--wrap=cudaStreamSynchronize)

add_executable(q4t_metadata_replay EXCLUDE_FROM_ALL
  tools/verify/inference_metadata/model_replay.cpp)
target_include_directories(q4t_metadata_replay PRIVATE include)
target_link_libraries(q4t_metadata_replay PRIVATE q4t_model)

add_executable(q4t_metadata_bounds EXCLUDE_FROM_ALL
  tools/verify/inference_metadata/model_bounds.cpp)
target_include_directories(q4t_metadata_bounds PRIVATE include)
target_link_libraries(q4t_metadata_bounds PRIVATE q4t_model)
target_compile_options(q4t_metadata_bounds PRIVATE -Wall -Wextra -Werror)

# Host-only validation of real metadata entry guards (no model loading).
add_executable(q4t_metadata_batch_entry EXCLUDE_FROM_ALL
  tools/verify/inference_metadata/batch_entry_test.cpp)
target_include_directories(q4t_metadata_batch_entry PRIVATE include)
target_link_libraries(q4t_metadata_batch_entry PRIVATE q4t_model)
target_compile_options(q4t_metadata_batch_entry PRIVATE -Wall -Wextra -Werror)

add_executable(q4t_metadata_batch_replay EXCLUDE_FROM_ALL
  tools/verify/inference_metadata/batch_replay.cpp)
target_include_directories(q4t_metadata_batch_replay PRIVATE include)
target_link_libraries(q4t_metadata_batch_replay PRIVATE q4t_model)
target_compile_options(q4t_metadata_batch_replay PRIVATE -Wall -Wextra -Werror)

# Explicit independent-head diagnostics, never run by a default build.
add_executable(q4t_subset_contract EXCLUDE_FROM_ALL tools/mtp_subset/contract.cpp)
target_link_libraries(q4t_subset_contract PRIVATE q4t_model)
add_executable(q4t_subset_replay EXCLUDE_FROM_ALL
  tests/test_main.cpp tools/mtp_subset/replay.cpp)
target_link_libraries(q4t_subset_replay PRIVATE q4t_model q4t_text q4t_runtime)

add_executable(q4t_subset_states EXCLUDE_FROM_ALL tests/test_main.cpp
  tests/mtp_spec_accept_states_test.cpp tests/mtp_spec_multi_test.cpp)
target_link_libraries(q4t_subset_states PRIVATE q4t_model q4t_runtime)
add_executable(q4t_subset_read EXCLUDE_FROM_ALL tools/mtp_subset/read.cpp)
target_link_libraries(q4t_subset_read PRIVATE q4t_model)

# CPU-only SHA contracts, invoked after the HTTP gate for startup optimization.
add_executable(q4t_sha256_contract EXCLUDE_FROM_ALL
  tools/verify/sha256_contract.cpp src/trace/sha256.cpp)
target_include_directories(q4t_sha256_contract PRIVATE include)
target_compile_definitions(q4t_sha256_contract PRIVATE Q4T_SHA256_TESTING=1)
target_compile_options(q4t_sha256_contract PRIVATE -Wall -Wextra)

find_package(Threads REQUIRED)
add_executable(q4t_session_storage_contract EXCLUDE_FROM_ALL
  tools/session_cache/storage_contract.cpp src/server/session_cache.cpp
  src/server/cache_io.cpp src/server/cache_worker.cpp
  src/server/grammar_process.cpp
  src/trace/sha256.cpp)
target_compile_definitions(q4t_session_storage_contract PRIVATE
  Q4T_GRAMMAR_PROCESS_TESTS=1)
target_link_libraries(q4t_session_storage_contract PRIVATE Threads::Threads)
target_include_directories(q4t_session_storage_contract PRIVATE include)
target_compile_options(q4t_session_storage_contract PRIVATE -Wall -Wextra)

# Pure host buffers and actual SSD storage; no CUDA/model execution.
add_executable(q4t_session_target_contract EXCLUDE_FROM_ALL
  tests/test_main.cpp tests/server_session_target_test.cpp)
target_link_libraries(q4t_session_target_contract PRIVATE q4t_server)
target_compile_definitions(q4t_session_target_contract PRIVATE
  Q4T_CACHE_WORKER_ENTRY=1)
target_compile_options(q4t_session_target_contract PRIVATE -Wall -Wextra)

add_executable(q4t_prefix_storage_contract EXCLUDE_FROM_ALL
  tools/prefix_cache/storage_contract.cpp src/server/session_cache.cpp
  src/server/cache_io.cpp src/server/cache_worker.cpp
  src/server/grammar_process.cpp
  src/server/server_options.cpp src/server/object_store_options.cpp
  src/trace/sha256.cpp)
target_compile_definitions(q4t_prefix_storage_contract PRIVATE
  Q4T_GRAMMAR_PROCESS_TESTS=1)
target_link_libraries(q4t_prefix_storage_contract PRIVATE Threads::Threads)
target_include_directories(q4t_prefix_storage_contract PRIVATE include)
target_compile_options(q4t_prefix_storage_contract PRIVATE -Wall -Wextra)

add_executable(q4t_incremental_storage_contract EXCLUDE_FROM_ALL
  tools/session_incremental/storage_contract.cpp src/server/session_cache.cpp
  src/server/cache_io.cpp src/server/cache_worker.cpp
  src/server/grammar_process.cpp src/trace/sha256.cpp)
target_include_directories(q4t_incremental_storage_contract PRIVATE include)
target_compile_definitions(q4t_incremental_storage_contract PRIVATE
  Q4T_GRAMMAR_PROCESS_TESTS=1)
target_link_libraries(q4t_incremental_storage_contract PRIVATE Threads::Threads)
target_compile_options(q4t_incremental_storage_contract PRIVATE -Wall -Wextra)
add_executable(q4t_prefix_state_contract EXCLUDE_FROM_ALL
  tools/prefix_cache/state_contract.cpp)
target_link_libraries(q4t_prefix_state_contract PRIVATE q4t_server)
target_compile_options(q4t_prefix_state_contract PRIVATE -Wall -Wextra)

add_executable(q4t_prefill_scoring_contract EXCLUDE_FROM_ALL
  tools/mtp_prefill_scoring/state_contract.cpp)
target_link_libraries(q4t_prefill_scoring_contract PRIVATE q4t_server)
target_compile_options(q4t_prefill_scoring_contract PRIVATE -Wall -Wextra -Werror)

add_executable(q4t_prefix_seed_contract EXCLUDE_FROM_ALL
  tests/test_main.cpp tests/server_chat_contract_test.cpp
  tools/prefix_seed/policy_contract.cpp)
target_link_libraries(q4t_prefix_seed_contract PRIVATE q4t_server)
target_include_directories(q4t_prefix_seed_contract PRIVATE include)
target_compile_options(q4t_prefix_seed_contract PRIVATE -Wall -Wextra)
