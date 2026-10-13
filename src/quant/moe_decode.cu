// Device-selected expert matrices, two grouped GEMMs, fixed-slot combine.
//
// Supports 1 <= T <= 8 tokens (R = T*k rows).
//
// T=1: every (token, slot) pair is one independent M=1 NVFP4 GEMM row, so
// the expert output uses the same per-row arithmetic as the original
// 10-group decode path (bit-identical).
//
// T>1: rows are grouped by expert on device (no host routing readback, no
// stream sync). Each expert e hit by M_e rows runs one M=M_e grouped GEMM,
// so its weights are read once instead of M_e times. Production
// measurement (Q4T_MOE_SHARE_DIAG, 2026-10-13) shows unique/rows ~= 0.687
// for T=4, i.e. ~1.46x redundant weight traffic in the per-row path.
// Inactive groups (fixed-R plan, cols=0) are skipped by cuBLASLt, so the
// active-expert count U never needs to reach the host. The grouped path is
// opt-in via Q4T_MOE_DECODE_GROUPED (per-row is the default; production
// sub-timing shows per-row is already L2-optimal).
#include "q4t/quant/moe_decode.h"

#include <cublasLt.h>
#include <cuda_bf16.h>

#include <cstdio>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <unordered_map>

#include "q4t/quant/format.h"
#include "q4t/quant/lt_cache.h"
#include "q4t/quant/moe_subtiming.h"

namespace q4t::quant {
namespace {

constexpr int kSlots = 10;
constexpr int kMaxT = 8;
constexpr int kMaxRows = kMaxT * kSlots;  // 80
constexpr int kArraySize = kMaxRows;  // Every pointer/shape array is
                                      // 16-byte aligned (80 * 8 = 640 B).
constexpr int kHidden = 2560;
constexpr int kIntermediate = 640;
constexpr int kPackedStride = kHidden / 2;
constexpr int kScaleStride = 40 * 512;  // SfBufferSize(1, 2560).
constexpr int kE = 512;
constexpr int kGuAtom = 40 * 512;   // SfBufferSize(1, kHidden).
constexpr int kDnAtom = 10 * 512;   // SfBufferSize(1, kIntermediate).
constexpr int kGuGTiles = 40;       // SfNumGtiles(kHidden).
constexpr int kDnGTiles = 10;       // SfNumGtiles(kIntermediate).
constexpr int kDnPackedStride = kIntermediate / 2;

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
  int dim_m[kArraySize];  // Grouped mode: per-group row count (0 = skip).
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

__device__ __forceinline__ size_t SfOffsetDev(int row, int group,
                                              int num_g_tiles) {
  const int i = row % 32;
  const int j = (row % 128) / 32;
  const int ga = group % 4;
  const int within = i * 16 + j * 4 + ga;
  return static_cast<size_t>(within) +
         static_cast<size_t>(group / 4) * 512 +
         static_cast<size_t>(row / 128) * static_cast<size_t>(num_g_tiles) *
             512;
}

// Expressions and BF16 boundary match GatherQuantKernel/SwiGLUQuantKernel.
// Only the placement of each independent row changes. Row r = token t = r/k,
// slot r%k; its expert is expert_ids[r].
template <bool Down>
__global__ void PrepareMoEDecodeKernel(const uint16_t* input,
                                       const int32_t* expert_ids,
                                       StageWeights weights, DeviceBatch* batch,
                                       uint8_t* packed, uint8_t* scales,
                                       uint16_t* output, int R, int k) {
  constexpr int kdim = Down ? kIntermediate : kHidden;
  constexpr int n = Down ? kHidden : 2 * kIntermediate;
  constexpr int groups = kdim / 16;
  constexpr int g_tiles = (groups + 3) / 4;
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx < R) {
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
    batch->dim_k[idx] = kdim;
    batch->dim_n[idx] = n;
    batch->dim_one[idx] = 1;
  }
  if (idx >= R * groups) return;
  const int r = idx / groups;
  const int g = idx % groups;
  const int e = expert_ids[r];
  const int t = r / k;
  const float inv_scale = weights.input_scale[e];
  // Down reads the gate/up GEMM output, laid out row-major over the R
  // independent rows (row r at r * 2 * kIntermediate); Up reads the hidden
  // state, laid out per token (token t at t * kHidden).
  const uint16_t* row = Down
                            ? input + static_cast<size_t>(r) * 2 * kIntermediate
                            : input + static_cast<size_t>(t) * kHidden;
  float a[16];
  float gmax = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    if constexpr (Down) {
      const float gv = Bf16ToFloat(row[g * 16 + j]);
      const float uv = Bf16ToFloat(row[kIntermediate + g * 16 + j]);
      const float s = 1.0f / (1.0f + __expf(-gv));
      a[j] = gv * s * uv;
    } else {
      a[j] = Bf16ToFloat(row[g * 16 + j]);
    }
    gmax = fmaxf(gmax, fabsf(a[j]));
  }
  const float block_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
  const uint8_t sf_code = FloatToE4m3(block_scale / inv_scale);
  const float eff = E4m3ToFloat(sf_code) * inv_scale;
  const float inv = eff > 0.0f ? 1.0f / eff : 0.0f;
  uint8_t* dst = packed + r * kPackedStride + g * 8;
#pragma unroll
  for (int j = 0; j < 8; ++j) {
    const int c0 = FloatToE2m1Code(a[2 * j] * inv);
    const int c1 = FloatToE2m1Code(a[2 * j + 1] * inv);
    dst[j] = static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
  }
  // Row zero of a separate full swizzle atom for each row.
  static_assert(g_tiles * 512 <= kScaleStride);
  scales[r * kScaleStride + (g / 4) * 512 + g % 4] = sf_code;
}

__global__ void CombineMoEDecodeKernel(const uint16_t* down,
                                       const float* router_w, float* y,
                                       int T, int k) {
  const int t = blockIdx.y;
  const int c = blockIdx.x * blockDim.x + threadIdx.x;
  if (c >= kHidden) return;
  float acc = 0.0f;
  for (int slot = 0; slot < k; ++slot) {
    const int r = t * k + slot;
    acc += router_w[r] * Bf16ToFloat(down[r * kHidden + c]);
  }
  y[static_cast<size_t>(t) * kHidden + c] += acc;
}

// Observation-only expert-sharing diagnostic (gated by Q4T_MOE_SHARE_DIAG).
// Counts how many of the R (token,slot) rows hit distinct experts per layer,
// to quantify the weight-traffic headroom of grouping rows by expert.
// Never touches compute; off by default.
__global__ void MoEShareDiagKernel(const int32_t* __restrict__ expert_ids,
                                   int R, unsigned long long* unique_sum,
                                   unsigned long long* rows_sum,
                                   unsigned long long* call_sum) {
  if (blockIdx.x != 0 || threadIdx.x != 0) return;
  int unique = 0;
  for (int i = 0; i < R; ++i) {
    bool seen = false;
    for (int j = 0; j < i; ++j)
      if (expert_ids[j] == expert_ids[i]) {
        seen = true;
        break;
      }
    if (!seen) ++unique;
  }
  atomicAdd(unique_sum, static_cast<unsigned long long>(unique));
  atomicAdd(rows_sum, static_cast<unsigned long long>(R));
  atomicAdd(call_sum, 1ull);
}

// ============================================================================
// Expert-grouped path (T > 1).
//
// Row r = (token r/k, slot r%k) is routed to expert expert_ids[r]. Rows are
// compacted per expert: expert e's rows occupy compact rows
// [offset[e], offset[e+1]) in first-seen order (row_pos[r] is r's position
// within expert e). row_of_flat[r] is r's compact row, shared by both GEMM
// stages and the final combine.
// ============================================================================

// Single-block plan: per-expert counts, exclusive offsets over E experts,
// active-expert list, per-row positions, and the active count U.
// counts and U must be zeroed before launch.
__global__ void MoEDecodeGroupPlanKernel(
    const int32_t* __restrict__ expert_ids, int R,
    int32_t* __restrict__ counts, int32_t* __restrict__ offset,
    int32_t* __restrict__ expert_list, int32_t* __restrict__ row_pos,
    int32_t* __restrict__ U) {
  __shared__ int s_inc[kE];
  const int e = threadIdx.x;  // one thread per expert, 512 threads
  if (e < R) {
    const int ex = expert_ids[e];
    row_pos[e] = atomicAdd(&counts[ex], 1);
  }
  __syncthreads();
  const int c = counts[e];
  s_inc[e] = c;
  __syncthreads();  // initial stores visible before the first scan read
  for (int d = 1; d < kE; d <<= 1) {
    const int v = (e >= d) ? s_inc[e - d] : 0;
    __syncthreads();
    s_inc[e] += v;
    __syncthreads();
  }
  offset[e] = s_inc[e] - c;  // exclusive prefix
  if (e == kE - 1) offset[kE] = s_inc[kE - 1];
  if (c > 0) {
    const int slot = atomicAdd(U, 1);
    expert_list[slot] = e;
  }
}

// Gather + quantize one row into its expert's compact region. Same
// quantization expressions as PrepareMoEDecodeKernel; only the row
// placement changes (compact row dst = offset[e] + row_pos[r], local row
// row_pos[r] inside the expert's shared swizzle atom).
template <bool Down>
__global__ void MoEDecodeGroupGatherKernel(
    const uint16_t* input, const int32_t* __restrict__ expert_ids,
    const int32_t* __restrict__ offset, const int32_t* __restrict__ row_pos,
    const int32_t* row_of_flat, StageWeights weights,
    uint8_t* packed, uint8_t* scales, int32_t* __restrict__ row_of_flat_out,
    int R, int k) {
  constexpr int kdim = Down ? kIntermediate : kHidden;
  constexpr int groups = kdim / 16;
  constexpr int g_tiles = Down ? kDnGTiles : kGuGTiles;
  constexpr int atom = Down ? kDnAtom : kGuAtom;
  constexpr int packed_stride = Down ? kDnPackedStride : kPackedStride;
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx < R && !Down) {
    const int e = expert_ids[idx];
    row_of_flat_out[idx] = offset[e] + row_pos[idx];
  }
  if (idx >= R * groups) return;
  const int r = idx / groups;
  const int g = idx % groups;
  const int e = expert_ids[r];
  const int dst = offset[e] + row_pos[r];
  const int local = row_pos[r];
  const float inv_scale = weights.input_scale[e];
  // Down reads the gate/up GEMM output, compact row row_of_flat[r] at
  // row_of_flat[r] * 2 * kIntermediate; Up reads the hidden state, laid out
  // per token (token t at t * kHidden).
  const uint16_t* row = Down
                            ? input +
                                  static_cast<size_t>(row_of_flat[r]) *
                                      2 * kIntermediate
                            : input +
                                  static_cast<size_t>(r / k) * kHidden;
  float a[16];
  float gmax = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    if constexpr (Down) {
      const float gv = Bf16ToFloat(row[g * 16 + j]);
      const float uv = Bf16ToFloat(row[kIntermediate + g * 16 + j]);
      const float s = 1.0f / (1.0f + __expf(-gv));
      a[j] = gv * s * uv;
    } else {
      a[j] = Bf16ToFloat(row[g * 16 + j]);
    }
    gmax = fmaxf(gmax, fabsf(a[j]));
  }
  const float block_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
  const uint8_t sf_code = FloatToE4m3(block_scale / inv_scale);
  const float eff = E4m3ToFloat(sf_code) * inv_scale;
  const float inv = eff > 0.0f ? 1.0f / eff : 0.0f;
  uint8_t* dst_p = packed + static_cast<size_t>(dst) * packed_stride + g * 8;
#pragma unroll
  for (int j = 0; j < 8; ++j) {
    const int c0 = FloatToE2m1Code(a[2 * j] * inv);
    const int c1 = FloatToE2m1Code(a[2 * j + 1] * inv);
    dst_p[j] = static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
  }
  // Shared swizzle atom per expert; local row < 32 keeps the atom single.
  scales[static_cast<size_t>(offset[e]) * atom +
         SfOffsetDev(local, g, g_tiles)] = sf_code;
}

// Build the fixed-R grouped batch: group i (i < U) is expert expert_list[i]
// with M_e rows; groups i >= U are skipped (dim_m = 0). One thread per group.
__global__ void MoEDecodeGroupBatchKernel(
    StageWeights weights, const int32_t* __restrict__ offset,
    const int32_t* __restrict__ expert_list, const int32_t* __restrict__ U,
    DeviceBatch* batch, uint8_t* compact_a, uint8_t* compact_asf,
    uint16_t* out, int R, int n, int kdim, int atom, int packed_stride) {
  const int i = threadIdx.x;
  if (i >= R) return;
  const int u = *U;
  int m = 0;
  const uint8_t* w = weights.packed;
  const uint8_t* wsp = weights.scales;
  uint8_t* a = compact_a;
  uint8_t* asf = compact_asf;
  uint16_t* o = out;
  float alpha = 1.0f;
  if (i < u) {
    const int e = expert_list[i];
    const int base = offset[e];
    m = offset[e + 1] - base;
    w = weights.packed + static_cast<size_t>(e) * weights.packed_stride;
    wsp = weights.scales + static_cast<size_t>(e) * weights.scale_stride;
    a = compact_a + static_cast<size_t>(base) * packed_stride;
    asf = compact_asf + static_cast<size_t>(base) * atom;
    o = out + static_cast<size_t>(base) * n;
    alpha = weights.global_scale[e] * weights.input_scale[e];
  }
  batch->weights[i] = w;
  batch->weight_scales[i] = wsp;
  batch->activations[i] = a;
  batch->activation_scales[i] = asf;
  batch->outputs[i] = o;
  batch->alpha[i] = alpha;
  batch->beta[i] = 0.0f;
  batch->alpha_ptrs[i] = &batch->alpha[i];
  batch->beta_ptrs[i] = &batch->beta[i];
  batch->dim_k[i] = kdim;
  batch->dim_n[i] = n;
  batch->dim_one[i] = 1;
  batch->dim_m[i] = m;
}

// Deterministic combine over the expert-grouped [R, hs] down-GEMM output:
// one thread per (token t, hs element c), each y[t,c] written exactly once
// (no atomics), rows resolved through row_of_flat.
__global__ void CombineMoEDecodeGroupedKernel(
    const uint16_t* __restrict__ down,
    const int32_t* __restrict__ row_of_flat, const float* __restrict__ router_w,
    float* y, int T, int k) {
  const int t = blockIdx.y;
  const int c = blockIdx.x * blockDim.x + threadIdx.x;
  if (c >= kHidden) return;
  float acc = 0.0f;
  for (int slot = 0; slot < k; ++slot) {
    const int r = t * k + slot;
    acc += router_w[r] *
           Bf16ToFloat(down[static_cast<size_t>(row_of_flat[r]) * kHidden +
                            c]);
  }
  y[static_cast<size_t>(t) * kHidden + c] += acc;
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
// R = number of groups; the plan is cached per (R, n, k, mode).
//   per-row  (grouped=false): one M=1 row per group, cols from dim_one.
//   grouped  (grouped=true) : M_e rows per group (cols from dim_m, a device
//   array read at execution time; 0 skips the group), so the active-expert
//   count never reaches the host.
Status GroupedProjection(DeviceBatch* batch, int R, int n, int k,
                         void* workspace, size_t workspace_bytes,
                         cudaStream_t stream, bool grouped) {
  std::lock_guard<std::mutex> lock(LtCacheMutex());
  using Cache = std::unordered_map<LtPlanKey, std::unique_ptr<GroupedPlan>,
                                   LtPlanKeyHash>;
  static Cache cache;
  const LtPlanKey key{R, n, k, workspace_bytes, grouped ? 32 : 16};
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
    LT_TRY(cublasLtGroupedMatrixLayoutCreate(&plan->w, CUDA_R_4F_E2M1, R,
                                             batch->dim_k, batch->dim_n,
                                             batch->dim_k));
    LT_TRY(cublasLtGroupedMatrixLayoutCreate(&plan->a, CUDA_R_4F_E2M1, R,
                                             batch->dim_k,
                                             grouped ? batch->dim_m
                                                     : batch->dim_one,
                                             batch->dim_k));
    LT_TRY(cublasLtGroupedMatrixLayoutCreate(&plan->out, CUDA_R_16BF, R,
                                             batch->dim_n,
                                             grouped ? batch->dim_m
                                                     : batch->dim_one,
                                             batch->dim_n));
  }
  const int* cols = grouped ? batch->dim_m : batch->dim_one;
  const int* dims[3][3] = {{batch->dim_k, batch->dim_n, batch->dim_k},
                           {batch->dim_k, cols, batch->dim_k},
                           {batch->dim_n, cols, batch->dim_n}};
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
                 "groups=%d N=%d K=%d mode=%s cuBLASLt=%zu\n",
                 R, n, k, grouped ? "expert-grouped" : "per-row",
                 cublasLtGetVersion());
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

// Per-row path: R = T*k independent M=1 grouped GEMM rows. T=1 reference
// arithmetic; default for T>1 as well (see MoEDeviceDecodeMulti dispatch).
Status MoEDecodePerRow(const uint16_t* x, const int32_t* expert_ids,
                       const float* router_w, float* y,
                       const MoEWeightLayout& weights, const MoEWorkspace& ws,
                       void* gemm_ws, size_t gemm_ws_bytes, int T, int k,
                       cudaStream_t stream) {
  const int R = T * k;
  uint8_t* scratch = nullptr;
  if (cudaMallocAsync(
          &scratch, kBatchBytes + static_cast<size_t>(R) * kScaleStride,
          stream) != cudaSuccess) {
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
  const bool st = MoESubTiming::Instance().active();
  if (st) MoESubTiming::Instance().Start(0, stream);
  PrepareMoEDecodeKernel<false>
      <<<(R * (kHidden / 16) + 255) / 256, 256, 0, stream>>>(
          x, expert_ids, gu, batch, packed, scales, ws.gu_out, R, k);
  if (cudaGetLastError() != cudaSuccess) {
    return Status::Fail("MoE device decode GU preparation");
  }
  if (st) MoESubTiming::Instance().Stop(0, stream);
  if (st) MoESubTiming::Instance().Start(1, stream);
  Status s = GroupedProjection(batch, R, 2 * kIntermediate, kHidden, gemm_ws,
                               gemm_ws_bytes, stream, /*grouped=*/false);
  if (!s) return s;
  if (st) MoESubTiming::Instance().Stop(1, stream);
  if (st) MoESubTiming::Instance().Start(2, stream);
  PrepareMoEDecodeKernel<true>
      <<<(R * (kIntermediate / 16) + 255) / 256, 256, 0, stream>>>(
          ws.gu_out, expert_ids, dn, batch, packed, scales, ws.dn_out, R, k);
  if (cudaGetLastError() != cudaSuccess) {
    return Status::Fail("MoE device decode DN preparation");
  }
  if (st) MoESubTiming::Instance().Stop(2, stream);
  if (st) MoESubTiming::Instance().Start(3, stream);
  s = GroupedProjection(batch, R, kHidden, kIntermediate, gemm_ws,
                        gemm_ws_bytes, stream, /*grouped=*/false);
  if (!s) return s;
  if (st) MoESubTiming::Instance().Stop(3, stream);
  if (st) MoESubTiming::Instance().Start(4, stream);
  CombineMoEDecodeKernel<<<dim3((kHidden + 255) / 256, T), 256, 0, stream>>>(
      ws.dn_out, router_w, y, T, k);
  if (cudaGetLastError() != cudaSuccess) {
    return Status::Fail("MoE device decode combine");
  }
  if (st) MoESubTiming::Instance().Stop(4, stream);
  return {};
}

// Expert-grouped path: rows compacted per expert, one M=M_e grouped GEMM per
// active expert (weights read once), fixed-R plan with cols=0 skip groups.
// No host routing readback, no stream sync.
Status MoEDecodeGrouped(const uint16_t* x, const int32_t* expert_ids,
                        const float* router_w, float* y,
                        const MoEWeightLayout& weights, const MoEWorkspace& ws,
                        void* gemm_ws, size_t gemm_ws_bytes, int T, int k,
                        cudaStream_t stream) {
  const int R = T * k;
  // scratch: device batch + compact activation scales + routing metadata
  // (counts[E], U, offset[E+1], expert_list[R], row_pos[R], row_of_flat[R]).
  const size_t routing_bytes =
      static_cast<size_t>(kE) * sizeof(int32_t) + sizeof(int32_t) +
      static_cast<size_t>(kE + 1) * sizeof(int32_t) +
      3 * static_cast<size_t>(R) * sizeof(int32_t);
  uint8_t* scratch = nullptr;
  if (cudaMallocAsync(
          &scratch, kBatchBytes + static_cast<size_t>(R) * kScaleStride +
                         routing_bytes,
          stream) != cudaSuccess) {
    return Status::Fail("MoE device decode scratch allocation");
  }
  struct Release {
    void* ptr;
    cudaStream_t stream;
    ~Release() { cudaFreeAsync(ptr, stream); }
  } release{scratch, stream};
  auto* batch = reinterpret_cast<DeviceBatch*>(scratch);
  uint8_t* scales = scratch + kBatchBytes;
  int32_t* d_counts =
      reinterpret_cast<int32_t*>(scratch + kBatchBytes +
                                 static_cast<size_t>(R) * kScaleStride);
  int32_t* d_U = d_counts + kE;
  int32_t* d_offset = d_U + 1;
  int32_t* d_expert_list = d_offset + kE + 1;
  int32_t* d_row_pos = d_expert_list + R;
  int32_t* d_row_of_flat = d_row_pos + R;
  if (cudaMemsetAsync(d_counts, 0,
                      static_cast<size_t>(kE) * sizeof(int32_t) +
                          sizeof(int32_t),
                      stream) != cudaSuccess) {
    return Status::Fail("MoE device decode routing zeroing");
  }
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
  const bool st = MoESubTiming::Instance().active();
  // Phase 0: plan + up gather + batch build.
  if (st) MoESubTiming::Instance().Start(0, stream);
  MoEDecodeGroupPlanKernel<<<1, kE, 0, stream>>>(expert_ids, R, d_counts,
                                                 d_offset, d_expert_list,
                                                 d_row_pos, d_U);
  if (const auto e0 = cudaGetLastError(); e0 != cudaSuccess) {
    std::fprintf(stderr, "[q4t-debug] plan: %s\n",
                 cudaGetErrorString(e0));
    return Status::Fail("MoE device decode GU preparation");
  }
  if (std::getenv("Q4T_MOE_GROUP_DEBUG") != nullptr) {
    int32_t h_off[kE + 1] = {}, h_rpos[R] = {}, h_U = -1, h_cnt[16] = {};
    cudaMemcpy(h_off, d_offset, sizeof(h_off), cudaMemcpyDeviceToHost);
    cudaMemcpy(h_rpos, d_row_pos, sizeof(h_rpos), cudaMemcpyDeviceToHost);
    cudaMemcpy(&h_U, d_U, sizeof(h_U), cudaMemcpyDeviceToHost);
    cudaMemcpy(h_cnt, d_counts, sizeof(h_cnt), cudaMemcpyDeviceToHost);
    std::fprintf(stderr,
                 "[q4t-debug] scratch=%p kBatchBytes=%zu scales_end=%zu "
                 "d_counts=%p d_U=%p d_offset=%p d_expert_list=%p "
                 "d_row_pos=%p d_row_of_flat=%p\n",
                 (void*)scratch, kBatchBytes,
                 kBatchBytes + static_cast<size_t>(R) * kScaleStride,
                 (void*)d_counts, (void*)d_U, (void*)d_offset,
                 (void*)d_expert_list, (void*)d_row_pos,
                 (void*)d_row_of_flat);
    // Host write/readback probe on d_offset to confirm pointer identity.
    const int32_t magic = 123456789;
    cudaMemcpy(&d_offset[7], &magic, sizeof(magic), cudaMemcpyHostToDevice);
    int32_t rb = 0;
    cudaMemcpy(&rb, &d_offset[7], sizeof(rb), cudaMemcpyDeviceToHost);
    std::fprintf(stderr, "[q4t-debug] probe d_offset[7] wrote=%d readback=%d "
                 "off[7]=%d\n", magic, rb, h_off[7]);
    std::fprintf(stderr, "[q4t-debug] U=%d counts[0..15]=", h_U);
    for (int i = 0; i < 16; ++i)
      std::fprintf(stderr, "%d ", h_cnt[i]);
    std::fprintf(stderr, "\n");
    for (int e = 0; e < kE; ++e)
      if (h_off[e] != e || h_off[e + 1] != e + 1)
        std::fprintf(stderr, "[q4t-debug] off[%d]=%d off[%d+1]=%d\n", e,
                     h_off[e], e, h_off[e + 1]);
    for (int r = 0; r < R; ++r)
      std::fprintf(stderr, "[q4t-debug] rpos[%d]=%d\n", r, h_rpos[r]);
  }
  MoEDecodeGroupGatherKernel<false>
      <<<(R * (kHidden / 16) + 255) / 256, 256, 0, stream>>>(
          x, expert_ids, d_offset, d_row_pos, nullptr, gu, packed, scales,
          d_row_of_flat, R, k);
  if (const auto e1 = cudaGetLastError(); e1 != cudaSuccess) {
    std::fprintf(stderr, "[q4t-debug] gather-up: %s\n",
                 cudaGetErrorString(e1));
    return Status::Fail("MoE device decode GU preparation");
  }
  MoEDecodeGroupBatchKernel<<<R, 128, 0, stream>>>(
      gu, d_offset, d_expert_list, d_U, batch, packed, scales, ws.gu_out, R,
      2 * kIntermediate, kHidden, kGuAtom, kPackedStride);
  if (const auto e2 = cudaGetLastError(); e2 != cudaSuccess) {
    std::fprintf(stderr, "[q4t-debug] batch-gu: %s\n",
                 cudaGetErrorString(e2));
    return Status::Fail("MoE device decode GU preparation");
  }
  if (st) MoESubTiming::Instance().Stop(0, stream);
  // Phase 1: gate/up grouped GEMM (M_e rows per active expert).
  if (st) MoESubTiming::Instance().Start(1, stream);
  Status s = GroupedProjection(batch, R, 2 * kIntermediate, kHidden, gemm_ws,
                               gemm_ws_bytes, stream, /*grouped=*/true);
  if (!s) return s;
  if (st) MoESubTiming::Instance().Stop(1, stream);
  // Phase 2: down gather (SwiGLU + quantize) + batch build.
  if (st) MoESubTiming::Instance().Start(2, stream);
  MoEDecodeGroupGatherKernel<true>
      <<<(R * (kIntermediate / 16) + 255) / 256, 256, 0, stream>>>(
          ws.gu_out, expert_ids, d_offset, d_row_pos, d_row_of_flat, dn,
          packed, scales, nullptr, R, k);
  MoEDecodeGroupBatchKernel<<<R, 128, 0, stream>>>(
      dn, d_offset, d_expert_list, d_U, batch, packed, scales, ws.dn_out, R,
      kHidden, kIntermediate, kDnAtom, kDnPackedStride);
  if (cudaGetLastError() != cudaSuccess) {
    return Status::Fail("MoE device decode DN preparation");
  }
  if (st) MoESubTiming::Instance().Stop(2, stream);
  // Phase 3: down grouped GEMM.
  if (st) MoESubTiming::Instance().Start(3, stream);
  s = GroupedProjection(batch, R, kHidden, kIntermediate, gemm_ws,
                        gemm_ws_bytes, stream, /*grouped=*/true);
  if (!s) return s;
  if (st) MoESubTiming::Instance().Stop(3, stream);
  // Phase 4: deterministic gather combine.
  if (st) MoESubTiming::Instance().Start(4, stream);
  CombineMoEDecodeGroupedKernel<<<dim3((kHidden + 255) / 256, T), 256,
                                  0, stream>>>(
      ws.dn_out, d_row_of_flat, router_w, y, T, k);
  if (cudaGetLastError() != cudaSuccess) {
    return Status::Fail("MoE device decode combine");
  }
  if (st) MoESubTiming::Instance().Stop(4, stream);
  return {};
}

Status MoEDeviceDecodeMulti(const uint16_t* x, const int32_t* expert_ids,
                            const float* router_w, float* y,
                            const MoEWeightLayout& weights,
                            const MoEWorkspace& ws, void* gemm_ws,
                            size_t gemm_ws_bytes, int T, int k,
                            cudaStream_t stream) {
  // The kernels are generic in E (per-expert strides are the fixed
  // kHidden/kIntermediate constants, not derived from weights.E), so the
  // residency C-slot layout (E == C) can use this path too.
  if (T < 1 || T > kMaxT || k != kSlots || weights.hs != kHidden ||
      weights.moe_is != kIntermediate) {
    return Status::Fail("MoE device decode multi: unsupported shape");
  }
  const int R = T * k;
  if (std::getenv("Q4T_MOE_SHARE_DIAG") != nullptr) {
    static unsigned long long* d_diag = nullptr;
    static int diag_calls = 0;
    if (!d_diag &&
        cudaMalloc(&d_diag, 3 * sizeof(unsigned long long)) != cudaSuccess)
      d_diag = nullptr;
    if (d_diag) {
      MoEShareDiagKernel<<<1, 32, 0, stream>>>(expert_ids, R, d_diag,
                                               d_diag + 1, d_diag + 2);
      if (++diag_calls % 64 == 0) {
        unsigned long long h[3];
        if (cudaMemcpy(h, d_diag, sizeof(h), cudaMemcpyDeviceToHost) ==
            cudaSuccess) {
          if (h[2] > 0)
            std::fprintf(stderr,
                         "[moe-share-diag] layers=%llu rows=%llu unique=%llu "
                         "unique/rows=%.3f (R=%d, T=%d)\n",
                         h[2], h[1], h[0],
                         static_cast<double>(h[0]) /
                             static_cast<double>(h[1] ? h[1] : 1),
                         R, T);
        }
      }
    }
  }
  // T=1 keeps the per-row path bit-identically. T>1 defaults to the per-row
  // path (validated reference; production sub-timing shows it is already
  // L2-optimal, so expert grouping buys only 3-7% on the GEMM, see
  // moe_gemm_grouped_bench + subdetail-grouped-211310). Q4T_MOE_DECODE_GROUPED
  // switches T>1 to the expert-grouped path (A/B, future use).
  if (T == 1 || std::getenv("Q4T_MOE_DECODE_GROUPED") == nullptr) {
    return MoEDecodePerRow(x, expert_ids, router_w, y, weights, ws, gemm_ws,
                           gemm_ws_bytes, T, k, stream);
  }
  return MoEDecodeGrouped(x, expert_ids, router_w, y, weights, ws, gemm_ws,
                          gemm_ws_bytes, T, k, stream);
}

Status MoEDeviceDecode(const uint16_t* x, const int32_t* expert_ids,
                       const float* router_w, float* y,
                       const MoEWeightLayout& weights, const MoEWorkspace& ws,
                       void* gemm_ws, size_t gemm_ws_bytes,
                       cudaStream_t stream) {
  return MoEDeviceDecodeMulti(x, expert_ids, router_w, y, weights, ws,
                              gemm_ws, gemm_ws_bytes, 1, kSlots, stream);
}

}  // namespace q4t::quant
