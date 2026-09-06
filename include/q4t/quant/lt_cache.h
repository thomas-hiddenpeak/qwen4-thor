// Shared cuBLASLt plan cache for the BF16 and NVFP4 GEMM primitives.
//
// Both q4t::model::Bf16Gemm and q4t::quant::Fp4Gemm previously created a
// fresh cublasLt handle, matmul descriptor, three matrix layouts, and ran
// cublasLtMatmulAlgoGetHeuristic on EVERY call, then destroyed them all.
// The heuristic query in particular costs 10-100+ us of CPU. In the decode
// steady state the GEMM shapes (M, N, K) are fixed per call site, so this
// work is pure repeated overhead and shows up in nsys as the 10-50us gaps
// between the ~4500 kernels launched per decode step.
//
// This header provides:
//   - a single process-global cublasLt handle (created once, never freed);
//   - per-flavor plan caches keyed by (M, N, K, workspace_bytes), each entry
//     holding the descriptor, the three layouts, and the chosen algorithm.
//
// The caches are guarded by a mutex. Decode is single-threaded, so the lock
// is uncontended in practice; it exists for correctness if a model instance
// is ever driven from multiple threads.
#pragma once

#include <cublasLt.h>

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <unordered_map>

namespace q4t {
namespace quant {

// Cached cuBLASLt objects for one GEMM shape. The descriptor and layouts are
// immutable after creation except that the NVFP4 path re-points its two scale
// pointers on each call (cheap SetAttribute); the algorithm is fixed.
struct LtPlan {
  cublasLtMatmulDesc_t op = nullptr;
  cublasLtMatrixLayout_t la = nullptr;
  cublasLtMatrixLayout_t lb = nullptr;
  cublasLtMatrixLayout_t lc = nullptr;
  cublasLtMatmulAlgo_t algo;
  bool has_algo = false;
};

// Key identifying a GEMM shape plus its workspace budget. The workspace size
// is part of the key because it feeds the heuristic (MAX_WORKSPACE_BYTES) and
// can change the chosen algorithm.
struct LtPlanKey {
  int M = 0;
  int N = 0;
  int K = 0;
  size_t ws = 0;
  bool operator==(const LtPlanKey& o) const {
    return M == o.M && N == o.N && K == o.K && ws == o.ws;
  }
};

struct LtPlanKeyHash {
  size_t operator()(const LtPlanKey& k) const {
    // FNV-1a over the four fields.
    uint64_t h = 1469598103934665603ull;
    auto mix = [&](uint64_t v) {
      h ^= v;
      h *= 1099511628211ull;
    };
    mix(static_cast<uint64_t>(static_cast<uint32_t>(k.M)));
    mix(static_cast<uint64_t>(static_cast<uint32_t>(k.N)));
    mix(static_cast<uint64_t>(static_cast<uint32_t>(k.K)));
    mix(static_cast<uint64_t>(k.ws));
    return static_cast<size_t>(h);
  }
};

using LtPlanMap = std::unordered_map<LtPlanKey, LtPlan, LtPlanKeyHash>;

// Process-global cuBLASLt handle. Created lazily on first use; intentionally
// never destroyed (process-lifetime, avoids repeated create/destroy cost).
cublasLtHandle_t GlobalLtHandle();

// One plan cache per GEMM flavor (descriptor/layout dtypes differ between
// BF16 and NVFP4).
LtPlanMap& Bf16PlanCache();
LtPlanMap& Fp4PlanCache();

// Shared lock guarding both caches and the global handle init.
std::mutex& LtCacheMutex();

}  // namespace quant
}  // namespace q4t
