#include "q4t/quant/lt_cache.h"

namespace q4t {
namespace quant {

namespace {

LtPlanMap g_bf16_plans;
LtPlanMap g_fp4_plans;
std::mutex g_mutex;

}  // namespace

cublasLtHandle_t GlobalLtHandle() {
  // Process-lifetime handle via magic statics: thread-safe one-time init,
  // never destroyed (cublasLtCreate is non-trivial; the handle is safe to
  // keep for the process lifetime).
  //
  // IMPORTANT: this must NOT take g_mutex. The GEMM entry points hold
  // g_mutex (guarding the plan caches) while calling GlobalLtHandle(); a
  // non-recursive mutex would self-deadlock on the first GEMM call.
  static cublasLtHandle_t handle = [] -> cublasLtHandle_t {
    cublasLtHandle_t h = nullptr;
    if (cublasLtCreate(&h) != CUBLAS_STATUS_SUCCESS) {
      return nullptr;
    }
    return h;
  }();
  return handle;
}

LtPlanMap& Bf16PlanCache() {
  return g_bf16_plans;
}

LtPlanMap& Fp4PlanCache() {
  return g_fp4_plans;
}

std::mutex& LtCacheMutex() {
  return g_mutex;
}

}  // namespace quant
}  // namespace q4t
