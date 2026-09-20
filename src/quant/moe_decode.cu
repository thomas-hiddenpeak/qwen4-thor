// Device-selected expert matrices, two grouped GEMMs, fixed-slot combine.
#include "q4t/quant/moe_decode.h"

#include <cublasLt.h>
#include <cuda_bf16.h>

#include <cstdio>
#include <memory>
#include <mutex>
#include <unordered_map>

#include "q4t/quant/format.h"
#include "q4t/quant/lt_cache.h"

namespace q4t::quant {
namespace {

constexpr int kSlots = 10;
constexpr int kArraySize = 12;  // Every pointer/shape array is 16-byte aligned.
constexpr int kHidden = 2560;
constexpr int kIntermediate = 640;
constexpr int kPackedStride = kHidden / 2;
constexpr int kScaleStride = 40 * 512;  // SfBufferSize(1, 2560).

struct alignas(16) DeviceBatch {
  const void* weights[kArraySize];
  const void* activations[kArraySize];
  void* outputs[kArraySize];
  const void* weight_scales[kArraySize];
  const void* activation_scales[kArraySize];
  const float* alpha_ptrs[kArraySize];
  const float* beta_ptrs[kArraySize];
  float alpha[kArraySize];
  float beta[kArraySize];
  int dim_k[kArraySize];
  int dim_n[kArraySize];
  int dim_one[kArraySize];
};
constexpr size_t kBatchBytes = (sizeof(DeviceBatch) + 255) & ~size_t{255};

struct StageWeights {
  const uint8_t* packed;
  const uint8_t* scales;
  const float* global_scale;
  const float* input_scale;
  size_t packed_stride;
  size_t scale_stride;
};

__device__ float Bf16ToFloat(uint16_t bits) {
  return __bfloat162float(*reinterpret_cast<const __nv_bfloat16*>(&bits));
}

// Expressions and BF16 boundary match GatherQuantKernel/SwiGLUQuantKernel.
// Only the placement of each independent expert row changes.
template <bool Down>
__global__ void PrepareMoEDecodeKernel(const uint16_t* input,
                                       const int32_t* expert_ids,
                                       StageWeights weights, DeviceBatch* batch,
                                       uint8_t* packed, uint8_t* scales,
                                       uint16_t* output) {
  constexpr int k = Down ? kIntermediate : kHidden;
  constexpr int n = Down ? kHidden : 2 * kIntermediate;
  constexpr int groups = k / 16;
  constexpr int g_tiles = (groups + 3) / 4;
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx < kSlots) {
    const int e = expert_ids[idx];
    batch->weights[idx] = weights.packed + e * weights.packed_stride;
    batch->weight_scales[idx] = weights.scales + e * weights.scale_stride;
    batch->activations[idx] = packed + idx * kPackedStride;
    batch->activation_scales[idx] = scales + idx * kScaleStride;
    batch->outputs[idx] = output + idx * n;
    batch->alpha[idx] = weights.global_scale[e] * weights.input_scale[e];
    batch->beta[idx] = 0.0f;
    batch->alpha_ptrs[idx] = &batch->alpha[idx];
    batch->beta_ptrs[idx] = &batch->beta[idx];
    batch->dim_k[idx] = k;
    batch->dim_n[idx] = n;
    batch->dim_one[idx] = 1;
  }
  if (idx >= kSlots * groups) return;
  const int slot = idx / groups;
  const int g = idx % groups;
  const int e = expert_ids[slot];
  const float inv_scale = weights.input_scale[e];
  float a[16];
  float gmax = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    if constexpr (Down) {
      const uint16_t* gu = input + slot * 2 * kIntermediate;
      const float gv = Bf16ToFloat(gu[g * 16 + j]);
      const float uv = Bf16ToFloat(gu[kIntermediate + g * 16 + j]);
      const float s = 1.0f / (1.0f + __expf(-gv));
      a[j] = gv * s * uv;
    } else {
      a[j] = Bf16ToFloat(input[g * 16 + j]);
    }
    gmax = fmaxf(gmax, fabsf(a[j]));
  }
  const float block_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
  const uint8_t sf_code = FloatToE4m3(block_scale / inv_scale);
  const float eff = E4m3ToFloat(sf_code) * inv_scale;
  const float inv = eff > 0.0f ? 1.0f / eff : 0.0f;
  uint8_t* dst = packed + slot * kPackedStride + g * 8;
#pragma unroll
  for (int j = 0; j < 8; ++j) {
    const int c0 = FloatToE2m1Code(a[2 * j] * inv);
    const int c1 = FloatToE2m1Code(a[2 * j + 1] * inv);
    dst[j] = static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
  }
  // Row zero of a separate full swizzle atom for each slot.
  static_assert(g_tiles * 512 <= kScaleStride);
  scales[slot * kScaleStride + (g / 4) * 512 + g % 4] = sf_code;
}

__global__ void CombineMoEDecodeKernel(const uint16_t* down,
                                       const float* router_w, float* y) {
  const int c = blockIdx.x * blockDim.x + threadIdx.x;
  if (c >= kHidden) return;
  float acc = 0.0f;
  for (int slot = 0; slot < kSlots; ++slot) {
    acc += router_w[slot] * Bf16ToFloat(down[slot * kHidden + c]);
  }
  y[c] += acc;
}

struct GroupedPlan {
  cublasLtMatmulDesc_t op = nullptr;
  cublasLtMatrixLayout_t w = nullptr;
  cublasLtMatrixLayout_t a = nullptr;
  cublasLtMatrixLayout_t out = nullptr;
  cublasLtMatmulAlgo_t algo{};
  ~GroupedPlan() {
    if (out) cublasLtMatrixLayoutDestroy(out);
    if (a) cublasLtMatrixLayoutDestroy(a);
    if (w) cublasLtMatrixLayoutDestroy(w);
    if (op) cublasLtMatmulDescDestroy(op);
  }
};

Status LtError(cublasStatus_t result, const char* operation) {
  return Status::Fail(std::string("MoE device decode ") + operation +
                      ": cuBLAS status " + std::to_string(result));
}

// Descriptors are host-side cached plans, not owners of the device batch.
// Every invocation rebinds all dynamic pointers before enqueueing matmul.
Status GroupedProjection(DeviceBatch* batch, int n, int k, void* workspace,
                         size_t workspace_bytes, cudaStream_t stream) {
  std::lock_guard<std::mutex> lock(LtCacheMutex());
  using Cache = std::unordered_map<LtPlanKey, std::unique_ptr<GroupedPlan>,
                                   LtPlanKeyHash>;
  static Cache cache;
  const LtPlanKey key{kSlots, n, k, workspace_bytes, 16};
  auto it = cache.find(key);
  std::unique_ptr<GroupedPlan> created;
  GroupedPlan* plan;
  const bool is_new = it == cache.end();
  if (is_new) {
    created = std::make_unique<GroupedPlan>();
    plan = created.get();
  } else {
    plan = it->second.get();
  }
#define LT_TRY(expr)                                                    \
  do {                                                                  \
    const cublasStatus_t result = (expr);                               \
    if (result != CUBLAS_STATUS_SUCCESS) return LtError(result, #expr); \
  } while (false)
  if (is_new) {
    LT_TRY(cublasLtMatmulDescCreate(&plan->op, CUBLAS_COMPUTE_32F, CUDA_R_32F));
    const cublasOperation_t trans_w = CUBLAS_OP_T;
    LT_TRY(cublasLtMatmulDescSetAttribute(plan->op, CUBLASLT_MATMUL_DESC_TRANSA,
                                          &trans_w, sizeof(trans_w)));
    const cublasLtPointerMode_t mode = CUBLASLT_POINTER_MODE_DEVICE;
    LT_TRY(cublasLtMatmulDescSetAttribute(
        plan->op, CUBLASLT_MATMUL_DESC_POINTER_MODE, &mode, sizeof(mode)));
    const int64_t stride = 1;  // Device arrays of per-group scalar pointers.
    LT_TRY(cublasLtMatmulDescSetAttribute(
        plan->op, CUBLASLT_MATMUL_DESC_ALPHA_BATCH_STRIDE, &stride,
        sizeof(stride)));
    LT_TRY(cublasLtMatmulDescSetAttribute(
        plan->op, CUBLASLT_MATMUL_DESC_BETA_BATCH_STRIDE, &stride,
        sizeof(stride)));
    const auto sf_mode = CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3;
    LT_TRY(cublasLtMatmulDescSetAttribute(plan->op,
                                          CUBLASLT_MATMUL_DESC_A_SCALE_MODE,
                                          &sf_mode, sizeof(sf_mode)));
    LT_TRY(cublasLtMatmulDescSetAttribute(plan->op,
                                          CUBLASLT_MATMUL_DESC_B_SCALE_MODE,
                                          &sf_mode, sizeof(sf_mode)));
    LT_TRY(cublasLtGroupedMatrixLayoutCreate(&plan->w, CUDA_R_4F_E2M1, kSlots,
                                             batch->dim_k, batch->dim_n,
                                             batch->dim_k));
    LT_TRY(cublasLtGroupedMatrixLayoutCreate(&plan->a, CUDA_R_4F_E2M1, kSlots,
                                             batch->dim_k, batch->dim_one,
                                             batch->dim_k));
    LT_TRY(cublasLtGroupedMatrixLayoutCreate(&plan->out, CUDA_R_16BF, kSlots,
                                             batch->dim_n, batch->dim_one,
                                             batch->dim_n));
  }
  const int* dims[3][3] = {{batch->dim_k, batch->dim_n, batch->dim_k},
                           {batch->dim_k, batch->dim_one, batch->dim_k},
                           {batch->dim_n, batch->dim_one, batch->dim_n}};
  const cublasLtMatrixLayout_t layouts[] = {plan->w, plan->a, plan->out};
  const cublasLtMatrixLayoutAttribute_t attrs[] = {
      CUBLASLT_GROUPED_MATRIX_LAYOUT_ROWS_ARRAY,
      CUBLASLT_GROUPED_MATRIX_LAYOUT_COLS_ARRAY,
      CUBLASLT_GROUPED_MATRIX_LAYOUT_LD_ARRAY};
  const auto int_width = CUBLASLT_INTEGER_WIDTH_32;
  for (int i = 0; i < 3; ++i) {
    for (int j = 0; j < 3; ++j) {
      LT_TRY(cublasLtMatrixLayoutSetAttribute(layouts[i], attrs[j], &dims[i][j],
                                              sizeof(dims[i][j])));
    }
    LT_TRY(cublasLtMatrixLayoutSetAttribute(
        layouts[i],
        CUBLASLT_GROUPED_MATRIX_LAYOUT_ROWS_COLS_ARRAY_INTEGER_WIDTH,
        &int_width, sizeof(int_width)));
    LT_TRY(cublasLtMatrixLayoutSetAttribute(
        layouts[i], CUBLASLT_GROUPED_MATRIX_LAYOUT_LD_ARRAY_INTEGER_WIDTH,
        &int_width, sizeof(int_width)));
  }
  const void* w_sf = batch->weight_scales;
  const void* a_sf = batch->activation_scales;
  LT_TRY(cublasLtMatmulDescSetAttribute(
      plan->op, CUBLASLT_MATMUL_DESC_A_SCALE_POINTER, &w_sf, sizeof(w_sf)));
  LT_TRY(cublasLtMatmulDescSetAttribute(
      plan->op, CUBLASLT_MATMUL_DESC_B_SCALE_POINTER, &a_sf, sizeof(a_sf)));
  if (is_new) {
    cublasLtMatmulPreferenceOpaque_t pref;
    LT_TRY(cublasLtMatmulPreferenceInit(&pref));
    LT_TRY(cublasLtMatmulPreferenceSetAttribute(
        &pref, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &workspace_bytes,
        sizeof(workspace_bytes)));
    const int64_t avg_n = n, avg_m = 1, avg_k = k;
    LT_TRY(cublasLtMatmulPreferenceSetAttribute(
        &pref, CUBLASLT_MATMUL_PREF_GROUPED_DESC_D_AVERAGE_ROWS, &avg_n,
        sizeof(avg_n)));
    LT_TRY(cublasLtMatmulPreferenceSetAttribute(
        &pref, CUBLASLT_MATMUL_PREF_GROUPED_DESC_D_AVERAGE_COLS, &avg_m,
        sizeof(avg_m)));
    LT_TRY(cublasLtMatmulPreferenceSetAttribute(
        &pref, CUBLASLT_MATMUL_PREF_GROUPED_AVERAGE_REDUCTION_DIM, &avg_k,
        sizeof(avg_k)));
    const uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
    LT_TRY(cublasLtMatmulPreferenceSetAttribute(
        &pref, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK, &reduction,
        sizeof(reduction)));
    cublasLtMatmulHeuristicResult_t heur{};
    int count = 0;
    LT_TRY(cublasLtMatmulAlgoGetHeuristic(GlobalLtHandle(), plan->op, plan->w,
                                          plan->a, plan->out, plan->out, &pref,
                                          1, &heur, &count));
    if (count != 1 || heur.state != CUBLAS_STATUS_SUCCESS) {
      return Status::Fail("MoE device decode: no grouped NVFP4 algorithm");
    }
    plan->algo = heur.algo;
    std::fprintf(stderr,
                 "[q4t] MoE device decode grouped NVFP4 enabled: "
                 "groups=10 N=%d K=%d cuBLASLt=%zu\n",
                 n, k, cublasLtGetVersion());
    cache.emplace(key, std::move(created));
  }
  LT_TRY(cublasLtMatmul(GlobalLtHandle(), plan->op, batch->alpha_ptrs,
                        batch->weights, plan->w, batch->activations, plan->a,
                        batch->beta_ptrs, batch->outputs, plan->out,
                        batch->outputs, plan->out, &plan->algo, workspace,
                        workspace_bytes, stream));
#undef LT_TRY
  return {};
}

}  // namespace

Status MoEDeviceDecode(const uint16_t* x, const int32_t* expert_ids,
                       const float* router_w, float* y,
                       const MoEWeightLayout& weights, const MoEWorkspace& ws,
                       void* gemm_ws, size_t gemm_ws_bytes,
                       cudaStream_t stream) {
  uint8_t* scratch = nullptr;
  if (cudaMallocAsync(&scratch, kBatchBytes + kSlots * kScaleStride, stream) !=
      cudaSuccess) {
    return Status::Fail("MoE device decode scratch allocation");
  }
  struct Release {
    void* ptr;
    cudaStream_t stream;
    ~Release() { cudaFreeAsync(ptr, stream); }
  } release{scratch, stream};
  auto* batch = reinterpret_cast<DeviceBatch*>(scratch);
  uint8_t* scales = scratch + kBatchBytes;
  auto* packed = reinterpret_cast<uint8_t*>(ws.a_packed);
  const StageWeights gu{weights.gu_packed,
                        weights.gu_sf,
                        weights.gu_w_scale2,
                        weights.gu_input_scale,
                        static_cast<size_t>(kIntermediate) * kHidden,
                        weights.gu_sf_block()};
  const StageWeights dn{weights.dn_packed,
                        weights.dn_sf,
                        weights.dn_w_scale2,
                        weights.dn_input_scale,
                        static_cast<size_t>(kHidden) * (kIntermediate / 2),
                        weights.dn_sf_block()};
  PrepareMoEDecodeKernel<false>
      <<<(kSlots * (kHidden / 16) + 255) / 256, 256, 0, stream>>>(
          x, expert_ids, gu, batch, packed, scales, ws.gu_out);
  if (cudaGetLastError() != cudaSuccess) {
    return Status::Fail("MoE device decode GU preparation");
  }
  Status s = GroupedProjection(batch, 2 * kIntermediate, kHidden, gemm_ws,
                               gemm_ws_bytes, stream);
  if (!s) return s;
  PrepareMoEDecodeKernel<true>
      <<<(kSlots * (kIntermediate / 16) + 255) / 256, 256, 0, stream>>>(
          ws.gu_out, expert_ids, dn, batch, packed, scales, ws.dn_out);
  if (cudaGetLastError() != cudaSuccess) {
    return Status::Fail("MoE device decode DN preparation");
  }
  s = GroupedProjection(batch, kHidden, kIntermediate, gemm_ws, gemm_ws_bytes,
                        stream);
  if (!s) return s;
  CombineMoEDecodeKernel<<<(kHidden + 255) / 256, 256, 0, stream>>>(
      ws.dn_out, router_w, y);
  if (cudaGetLastError() != cudaSuccess) {
    return Status::Fail("MoE device decode combine");
  }
  return {};
}

}  // namespace q4t::quant
