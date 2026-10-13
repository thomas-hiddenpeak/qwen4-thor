// Expert-grouped NVFP4 grouped GEMM bench (verify T=4 shapes).
//
// Characterizes the weight-traffic headroom of grouping (token,slot) rows by
// expert before the grouped GEMM. The production per-row M=1 grouped GEMM
// (moe_decode.cu) re-reads each expert's weights once per hitting row;
// production measurement (Q4T_MOE_SHARE_DIAG, 2026-10-13) shows
// unique/rows ~= 0.687 for T=4 (R=40 rows, ~27.5 distinct experts/layer),
// i.e. ~1.46x redundant weight traffic. Grouping rows by expert reads each
// expert's weights once.
//
// All modes are production-like: expert weights live in one 512-expert
// buffer, rows point at scattered expert offsets, and a 256 MB L2 flush
// precedes every measured GEMM (same harness as moe_gemm_bench.cu "prod").
//
// Modes:
//   perrow  : R groups, one M=1 row per group (current production path)
//   grouped : U groups, M_e rows per group (variable M_e, gathered rows)
//   padded  : R groups, real groups 0..U-1 + zero-column groups U..R-1
//             (checks whether a fixed-R plan can skip empty groups, which
//              would avoid any host readback of U in production)
//
// Distributions (R=40 rows = T=4 x k=10):
//   flat : U=40, all M_e=1
//   real : U=27, 13 groups of 2 + 14 groups of 1 (unique/rows=0.675)
//   hot  : U=20, 4 groups of 4 + 8 groups of 2 + 8 groups of 1
//
// Also verifies grouped outputs row-by-row against the per-row outputs
// (same activation rows; expect exact or ~1-ULP agreement).
#include <cublasLt.h>
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/quant/lt_cache.h"
#include "q4t/quant/swizzle.h"

using namespace q4t::quant;

#define CK(x)                                                     \
  do {                                                            \
    cudaError_t e = (x);                                          \
    if (e != cudaSuccess) {                                       \
      std::printf("CUDA error %s:%d: %s\n", __FILE__, __LINE__,   \
                  cudaGetErrorString(e));                         \
      std::exit(1);                                               \
    }                                                             \
  } while (0)

#define LCK(x)                                                    \
  do {                                                            \
    cublasStatus_t e = (x);                                       \
    if (e != CUBLAS_STATUS_SUCCESS) {                             \
      std::printf("cublasLt error %s:%d: %d\n", __FILE__, __LINE__, (int)e); \
      std::exit(1);                                               \
    }                                                             \
  } while (0)

__global__ void ReadBWKernel(const uint4* __restrict__ buf, size_t n4,
                             float* sink) {
  const size_t stride = static_cast<size_t>(blockDim.x) * gridDim.x;
  uint4 acc = {0, 0, 0, 0};
  for (size_t i = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
       i < n4; i += stride) {
    uint4 v = buf[i];
    acc.x += v.x; acc.y += v.y; acc.z += v.z; acc.w += v.w;
  }
  if (threadIdx.x == 0) sink[blockIdx.x] = acc.x + acc.y + acc.z + acc.w;
}

double MeasureReadBW(size_t bytes, int iters) {
  uint4* buf;
  CK(cudaMalloc(&buf, bytes));
  CK(cudaMemset(buf, 0x11, bytes));
  float* sink;
  CK(cudaMalloc(&sink, sizeof(float) * 4096));
  const int block = 256;
  const int grid = 20 * 8;
  const size_t n4 = bytes / 16;
  for (int i = 0; i < 3; ++i) ReadBWKernel<<<grid, block>>>(buf, n4, sink);
  CK(cudaDeviceSynchronize());
  cudaEvent_t a, b;
  CK(cudaEventCreate(&a));
  CK(cudaEventCreate(&b));
  CK(cudaEventRecord(a));
  for (int i = 0; i < iters; ++i) ReadBWKernel<<<grid, block>>>(buf, n4, sink);
  CK(cudaEventRecord(b));
  CK(cudaDeviceSynchronize());
  float ms = 0;
  CK(cudaEventElapsedTime(&ms, a, b));
  CK(cudaEventDestroy(a));
  CK(cudaEventDestroy(b));
  CK(cudaFree(buf));
  CK(cudaFree(sink));
  return static_cast<double>(bytes) * iters / (ms / 1000.0) / 1e9;
}

struct Dist {
  const char* name;
  std::vector<int> counts;  // M_e per group, sum == R
};

std::vector<Dist> MakeDists(int R) {
  std::vector<Dist> d;
  Dist flat{"flat", std::vector<int>(R, 1)};
  Dist real{"real", {}};
  for (int i = 0; i < 13; ++i) real.counts.push_back(2);
  for (int i = 0; i < R - 26; ++i) real.counts.push_back(1);
  Dist hot{"hot", {}};
  for (int i = 0; i < 4; ++i) hot.counts.push_back(4);
  for (int i = 0; i < 8; ++i) hot.counts.push_back(2);
  for (int i = 0; i < R - 32; ++i) hot.counts.push_back(1);
  // Sanity: every distribution must cover exactly R rows.
  for (auto& d : {flat, real, hot}) {
    int sum = 0;
    for (int c : d.counts) sum += c;
    if (sum != R) std::abort();
  }
  d.push_back(flat);
  d.push_back(real);
  d.push_back(hot);
  return d;
}

struct Plan {
  cublasLtMatmulDesc_t op = nullptr;
  cublasLtMatrixLayout_t w = nullptr;
  cublasLtMatrixLayout_t a = nullptr;
  cublasLtMatrixLayout_t out = nullptr;
  cublasLtMatmulAlgo_t algo{};
  bool ok = false;
};

// Build a grouped NVFP4 plan: num_groups groups, per-group (rows, cols, ld)
// from device int arrays. Returns ok=false if no algorithm is available.
Plan MakePlan(int num_groups, int N, int K, const int* d_rows,
              const int* d_cols, const int* d_ld, const int* d_rows_w,
              const int* d_cols_w, const int* d_ld_w, const int* d_rows_o,
              const int* d_cols_o, const int* d_ld_o, const void* w_sf,
              const void* a_sf, size_t ws_bytes, int avg_m) {
  Plan p;
  LCK(cublasLtMatmulDescCreate(&p.op, CUBLAS_COMPUTE_32F, CUDA_R_32F));
  cublasOperation_t trans_w = CUBLAS_OP_T;
  LCK(cublasLtMatmulDescSetAttribute(p.op, CUBLASLT_MATMUL_DESC_TRANSA,
                                     &trans_w, sizeof(trans_w)));
  cublasLtPointerMode_t mode = CUBLASLT_POINTER_MODE_DEVICE;
  LCK(cublasLtMatmulDescSetAttribute(p.op, CUBLASLT_MATMUL_DESC_POINTER_MODE,
                                     &mode, sizeof(mode)));
  int64_t stride64 = 1;
  LCK(cublasLtMatmulDescSetAttribute(
      p.op, CUBLASLT_MATMUL_DESC_ALPHA_BATCH_STRIDE, &stride64,
      sizeof(stride64)));
  LCK(cublasLtMatmulDescSetAttribute(
      p.op, CUBLASLT_MATMUL_DESC_BETA_BATCH_STRIDE, &stride64,
      sizeof(stride64)));
  auto sf_mode = CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3;
  LCK(cublasLtMatmulDescSetAttribute(p.op, CUBLASLT_MATMUL_DESC_A_SCALE_MODE,
                                     &sf_mode, sizeof(sf_mode)));
  LCK(cublasLtMatmulDescSetAttribute(p.op, CUBLASLT_MATMUL_DESC_B_SCALE_MODE,
                                     &sf_mode, sizeof(sf_mode)));
  LCK(cublasLtGroupedMatrixLayoutCreate(&p.w, CUDA_R_4F_E2M1, num_groups,
                                        d_rows_w, d_cols_w, d_ld_w));
  LCK(cublasLtGroupedMatrixLayoutCreate(&p.a, CUDA_R_4F_E2M1, num_groups,
                                        d_rows, d_cols, d_ld));
  LCK(cublasLtGroupedMatrixLayoutCreate(&p.out, CUDA_R_16BF, num_groups,
                                        d_rows_o, d_cols_o, d_ld_o));
  const int* dims[3][3] = {{d_rows_w, d_cols_w, d_ld_w},
                           {d_rows, d_cols, d_ld},
                           {d_rows_o, d_cols_o, d_ld_o}};
  cublasLtMatrixLayout_t layouts[3] = {p.w, p.a, p.out};
  cublasLtMatrixLayoutAttribute_t attrs[3] = {
      CUBLASLT_GROUPED_MATRIX_LAYOUT_ROWS_ARRAY,
      CUBLASLT_GROUPED_MATRIX_LAYOUT_COLS_ARRAY,
      CUBLASLT_GROUPED_MATRIX_LAYOUT_LD_ARRAY};
  auto int_width = CUBLASLT_INTEGER_WIDTH_32;
  for (int i = 0; i < 3; ++i) {
    for (int j = 0; j < 3; ++j)
      LCK(cublasLtMatrixLayoutSetAttribute(layouts[i], attrs[j], &dims[i][j],
                                           sizeof(dims[i][j])));
    LCK(cublasLtMatrixLayoutSetAttribute(
        layouts[i],
        CUBLASLT_GROUPED_MATRIX_LAYOUT_ROWS_COLS_ARRAY_INTEGER_WIDTH,
        &int_width, sizeof(int_width)));
    LCK(cublasLtMatrixLayoutSetAttribute(
        layouts[i], CUBLASLT_GROUPED_MATRIX_LAYOUT_LD_ARRAY_INTEGER_WIDTH,
        &int_width, sizeof(int_width)));
  }
  LCK(cublasLtMatmulDescSetAttribute(p.op, CUBLASLT_MATMUL_DESC_A_SCALE_POINTER,
                                     &w_sf, sizeof(w_sf)));
  LCK(cublasLtMatmulDescSetAttribute(p.op, CUBLASLT_MATMUL_DESC_B_SCALE_POINTER,
                                     &a_sf, sizeof(a_sf)));
  cublasLtMatmulPreferenceOpaque_t pref;
  LCK(cublasLtMatmulPreferenceInit(&pref));
  LCK(cublasLtMatmulPreferenceSetAttribute(
      &pref, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &ws_bytes,
      sizeof(ws_bytes)));
  int64_t avg_n = N, avg_k = K, avg_m64 = avg_m;
  LCK(cublasLtMatmulPreferenceSetAttribute(
      &pref, CUBLASLT_MATMUL_PREF_GROUPED_DESC_D_AVERAGE_ROWS, &avg_n,
      sizeof(avg_n)));
  LCK(cublasLtMatmulPreferenceSetAttribute(
      &pref, CUBLASLT_MATMUL_PREF_GROUPED_DESC_D_AVERAGE_COLS, &avg_m64,
      sizeof(avg_m64)));
  LCK(cublasLtMatmulPreferenceSetAttribute(
      &pref, CUBLASLT_MATMUL_PREF_GROUPED_AVERAGE_REDUCTION_DIM, &avg_k,
      sizeof(avg_k)));
  uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
  LCK(cublasLtMatmulPreferenceSetAttribute(
      &pref, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK, &reduction,
      sizeof(reduction)));
  cublasLtMatmulHeuristicResult_t heur{};
  int count = 0;
  LCK(cublasLtMatmulAlgoGetHeuristic(GlobalLtHandle(), p.op, p.w, p.a, p.out,
                                     p.out, &pref, 1, &heur, &count));
  if (count != 1 || heur.state != CUBLAS_STATUS_SUCCESS) {
    std::printf("  [no algo] groups=%d N=%d K=%d avg_m=%d\n", num_groups, N,
                K, avg_m);
    cublasLtMatrixLayoutDestroy(p.out);
    cublasLtMatrixLayoutDestroy(p.a);
    cublasLtMatrixLayoutDestroy(p.w);
    cublasLtMatmulDescDestroy(p.op);
    return p;
  }
  p.algo = heur.algo;
  p.ok = true;
  return p;
}

void DestroyPlan(Plan& p) {
  if (!p.ok) return;
  cublasLtMatrixLayoutDestroy(p.out);
  cublasLtMatrixLayoutDestroy(p.a);
  cublasLtMatrixLayoutDestroy(p.w);
  cublasLtMatmulDescDestroy(p.op);
  p.ok = false;
}

// Per-plan device arrays. cuBLASLt grouped layouts hold pointers to these
// arrays and read them at GEMM execution time (device pointer mode), so each
// plan needs its own set: sharing arrays across plans makes the last writer
// win at execution.
struct PlanArrays {
  int n = 0;
  void* d_wptr = nullptr;
  void* d_wsptr = nullptr;
  void* d_aptr = nullptr;
  void* d_asfptr = nullptr;
  void* d_optr = nullptr;
  void* d_alpha_p = nullptr;
  void* d_beta_p = nullptr;
  int* d_rows = nullptr;
  int* d_cols = nullptr;
  int* d_ld = nullptr;
  int* d_rows_w = nullptr;
  int* d_cols_w = nullptr;
  int* d_ld_w = nullptr;
  int* d_rows_o = nullptr;
  int* d_cols_o = nullptr;
  int* d_ld_o = nullptr;

  explicit PlanArrays(int n_groups, const float* d_alpha, const float* d_beta,
                      int N, int K)
      : n(n_groups) {
    auto alloc = [&](void** p) {
      CK(cudaMalloc(p, sizeof(void*) * n_groups));
    };
    auto alloc_i = [&](int** p) {
      CK(cudaMalloc(p, sizeof(int) * n_groups));
    };
    alloc(&d_wptr);
    alloc(&d_wsptr);
    alloc(&d_aptr);
    alloc(&d_asfptr);
    alloc(&d_optr);
    alloc(&d_alpha_p);
    alloc(&d_beta_p);
    alloc_i(&d_rows);
    alloc_i(&d_cols);
    alloc_i(&d_ld);
    alloc_i(&d_rows_w);
    alloc_i(&d_cols_w);
    alloc_i(&d_ld_w);
    alloc_i(&d_rows_o);
    alloc_i(&d_cols_o);
    alloc_i(&d_ld_o);
    std::vector<int> rows(n_groups, K), cols_w(n_groups, N), ld(n_groups, K),
        rows_o(n_groups, N), ld_o(n_groups, N);
    CK(cudaMemcpy(d_rows, rows.data(), sizeof(int) * n_groups,
                  cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_ld, ld.data(), sizeof(int) * n_groups,
                  cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_rows_w, rows.data(), sizeof(int) * n_groups,
                  cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_cols_w, cols_w.data(), sizeof(int) * n_groups,
                  cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_ld_w, ld.data(), sizeof(int) * n_groups,
                  cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_rows_o, rows_o.data(), sizeof(int) * n_groups,
                  cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_ld_o, ld_o.data(), sizeof(int) * n_groups,
                  cudaMemcpyHostToDevice));
    std::vector<const float*> ap(n_groups), bp(n_groups);
    for (int i = 0; i < n_groups; ++i) {
      ap[i] = d_alpha + i;
      bp[i] = d_beta + i;
    }
    CK(cudaMemcpy(d_alpha_p, ap.data(), sizeof(void*) * n_groups,
                  cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_beta_p, bp.data(), sizeof(void*) * n_groups,
                  cudaMemcpyHostToDevice));
  }

  ~PlanArrays() {
    void* ptrs[7] = {d_wptr, d_wsptr, d_aptr, d_asfptr, d_optr, d_alpha_p,
                     d_beta_p};
    for (void* p : ptrs)
      if (p) CK(cudaFree(p));
    int* ints[9] = {d_rows, d_cols, d_ld, d_rows_w, d_cols_w, d_ld_w,
                    d_rows_o, d_cols_o, d_ld_o};
    for (int* p : ints)
      if (p) CK(cudaFree(p));
  }

  void SetCols(const std::vector<int>& cols) {
    CK(cudaMemcpy(d_cols, cols.data(), sizeof(int) * cols.size(),
                  cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_cols_o, cols.data(), sizeof(int) * cols.size(),
                  cudaMemcpyHostToDevice));
  }
};

struct Buffers {
  uint8_t* d_w;      // [E, N, K/2]
  uint8_t* d_wsf;    // [E, atom]
  uint8_t* d_a;      // per-row: [R, K/2]
  uint8_t* d_asf;    // per-row: [R, atom]
  uint8_t* d_ag;     // grouped: [R, K/2] gathered
  uint8_t* d_asfg;   // grouped: [Umax, atom]
  uint16_t* d_outA;  // per-row out [R, N]
  uint16_t* d_outB;  // grouped out [R, N] (row r at row_dst[r])
  float* d_alpha;
  float* d_beta;
};

int main(int argc, char** argv) {
  int iters = 50;
  if (argc > 1) iters = std::atoi(argv[1]);
  cudaFree(0);
  cudaDeviceProp prop;
  CK(cudaGetDeviceProperties(&prop, 0));
  std::printf("GPU: %s SMs=%d memBus=%dbit L2=%zuMB\n", prop.name,
              prop.multiProcessorCount, prop.memoryBusWidth,
              static_cast<size_t>(prop.l2CacheSize) / 1048576);
  double bw = MeasureReadBW(size_t(1) << 30, 10);
  std::printf("HBM read BW: %.1f GB/s\n", bw);

  const int E = 512;
  const int R = 40;
  struct Shape { int N, K; const char* name; };
  Shape shapes[] = {{1280, 2560, "gemm_gu"}, {2560, 640, "gemm_dn"}};
  const size_t ws_bytes = 32 * 1024 * 1024;
  void* ws;
  CK(cudaMalloc(&ws, ws_bytes));
  cudaStream_t stream;
  CK(cudaStreamCreate(&stream));

  const size_t flush_bytes = 256 * 1024 * 1024;
  uint4* flush;
  CK(cudaMalloc(&flush, flush_bytes));
  CK(cudaMemset(flush, 0x11, flush_bytes));
  float* sink;
  CK(cudaMalloc(&sink, sizeof(float) * 4096));
  const int fblock = 256;
  const int fgrid = 20 * 8;
  auto run_flush = [&]() {
    ReadBWKernel<<<fgrid, fblock, 0, stream>>>(flush, flush_bytes / 16, sink);
  };

  for (auto& s : shapes) {
    const int N = s.N, K = s.K;
    const size_t stride = static_cast<size_t>(N) * (K / 2);
    const size_t atom = SfBufferSize(1, K);
    const int num_g_tiles = SfNumGtiles(K);

    Buffers b{};
    CK(cudaMalloc(&b.d_w, static_cast<size_t>(E) * stride));
    CK(cudaMemset(b.d_w, 0x11, static_cast<size_t>(E) * stride));
    CK(cudaMalloc(&b.d_wsf, static_cast<size_t>(E) * atom));
    CK(cudaMemset(b.d_wsf, 0x33, static_cast<size_t>(E) * atom));
    CK(cudaMalloc(&b.d_a, static_cast<size_t>(R) * (K / 2)));
    CK(cudaMemset(b.d_a, 0x11, static_cast<size_t>(R) * (K / 2)));
    CK(cudaMalloc(&b.d_ag, static_cast<size_t>(R) * (K / 2)));
    CK(cudaMemset(b.d_ag, 0x11, static_cast<size_t>(R) * (K / 2)));
    CK(cudaMalloc(&b.d_outA, static_cast<size_t>(R) * N * 2));
    CK(cudaMalloc(&b.d_outB, static_cast<size_t>(R) * N * 2));
    CK(cudaMalloc(&b.d_alpha, sizeof(float) * R));
    CK(cudaMalloc(&b.d_beta, sizeof(float) * R));
    std::vector<float> h_alpha(R, 1.0f), h_beta(R, 0.0f);
    CK(cudaMemcpy(b.d_alpha, h_alpha.data(), sizeof(h_alpha),
                  cudaMemcpyHostToDevice));
    CK(cudaMemcpy(b.d_beta, h_beta.data(), sizeof(h_beta),
                  cudaMemcpyHostToDevice));

    // Per-row activation scales: row r atom, scale(group g) = f(r, g).
    std::vector<uint8_t> h_asf(R * atom, 0);
    // Grouped activation scales: filled per distribution below.
    std::vector<uint8_t> h_asfg(R * atom, 0);
    auto scale_val = [](int r, int g) {
      return static_cast<uint8_t>(0x20 + ((r * 7 + g * 3) & 0x0F));
    };
    for (int r = 0; r < R; ++r)
      for (int g = 0; g < K / 16; ++g)
        h_asf[static_cast<size_t>(r) * atom + SfOffset(0, g, num_g_tiles)] =
            scale_val(r, g);
    CK(cudaMalloc(&b.d_asf, static_cast<size_t>(R) * atom));
    CK(cudaMemcpy(b.d_asf, h_asf.data(), h_asf.size(), cudaMemcpyHostToDevice));
    CK(cudaMalloc(&b.d_asfg, static_cast<size_t>(R) * atom));

    for (auto& dist : MakeDists(R)) {
      const int U = static_cast<int>(dist.counts.size());
      // Row -> (group, local) and expert per group. Rows are assigned to
      // groups in order; group u owns expert (u*37)%E (distinct for u<E).
      std::vector<int> row_group(R), row_local(R), group_expert(U);
      {
        int r = 0;
        for (int u = 0; u < U; ++u) {
          group_expert[u] = (u * 37) % E;
          for (int i = 0; i < dist.counts[u]; ++i) {
            row_group[r] = u;
            row_local[r] = i;
            ++r;
          }
        }
      }
      std::vector<int> row_dst(R);
      std::vector<int> goff(U + 1, 0);
      for (int u = 0; u < U; ++u) goff[u + 1] = goff[u] + dist.counts[u];
      for (int r = 0; r < R; ++r) row_dst[r] = goff[row_group[r]] + row_local[r];

      // Grouped activation scales: group u atom, local row i, scale = f(r,g).
      std::fill(h_asfg.begin(), h_asfg.end(), 0);
      for (int r = 0; r < R; ++r) {
        const int u = row_group[r], i = row_local[r];
        for (int g = 0; g < K / 16; ++g)
          h_asfg[static_cast<size_t>(u) * atom + SfOffset(i, g, num_g_tiles)] =
              scale_val(r, g);
      }
      CK(cudaMemcpy(b.d_asfg, h_asfg.data(), h_asfg.size(),
                    cudaMemcpyHostToDevice));

      // Mode A (perrow): R groups, row r -> expert group_expert[row_group[r]].
      PlanArrays arrA(R, b.d_alpha, b.d_beta, N, K);
      {
        std::vector<const void*> wptr(R), wsptr(R), aptr(R), asfptr(R);
        std::vector<void*> optr(R);
        for (int r = 0; r < R; ++r) {
          const int e = group_expert[row_group[r]];
          wptr[r] = b.d_w + static_cast<size_t>(e) * stride;
          wsptr[r] = b.d_wsf + static_cast<size_t>(e) * atom;
          aptr[r] = b.d_a + static_cast<size_t>(r) * (K / 2);
          asfptr[r] = b.d_asf + static_cast<size_t>(r) * atom;
          optr[r] = b.d_outA + static_cast<size_t>(r) * N;
        }
        CK(cudaMemcpy(arrA.d_wptr, wptr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrA.d_wsptr, wsptr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrA.d_aptr, aptr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrA.d_asfptr, asfptr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrA.d_optr, optr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        arrA.SetCols(std::vector<int>(R, 1));
      }
      Plan planA = MakePlan(R, N, K, arrA.d_rows, arrA.d_cols, arrA.d_ld,
                            arrA.d_rows_w, arrA.d_cols_w, arrA.d_ld_w,
                            arrA.d_rows_o, arrA.d_cols_o, arrA.d_ld_o,
                            arrA.d_wsptr, arrA.d_asfptr, ws_bytes, 1);
      if (!planA.ok) {
        std::printf("%s %s perrow: NO ALGO\n", dist.name, s.name);
        continue;
      }
      auto runA = [&]() {
        return cublasLtMatmul(GlobalLtHandle(), planA.op, arrA.d_alpha_p,
                              arrA.d_wptr, planA.w, arrA.d_aptr, planA.a,
                              arrA.d_beta_p, arrA.d_optr, planA.out,
                              arrA.d_optr, planA.out, &planA.algo, ws,
                              ws_bytes, stream);
      };

      // Mode B (grouped): U groups, M_e rows each.
      PlanArrays arrB(U, b.d_alpha, b.d_beta, N, K);
      {
        std::vector<const void*> wptr(U), wsptr(U), aptr(U), asfptr(U);
        std::vector<void*> optr(U);
        for (int u = 0; u < U; ++u) {
          const int e = group_expert[u];
          wptr[u] = b.d_w + static_cast<size_t>(e) * stride;
          wsptr[u] = b.d_wsf + static_cast<size_t>(e) * atom;
          aptr[u] = b.d_ag + static_cast<size_t>(u) * (K / 2);
          asfptr[u] = b.d_asfg + static_cast<size_t>(u) * atom;
          optr[u] = b.d_outB + static_cast<size_t>(goff[u]) * N;
        }
        CK(cudaMemcpy(arrB.d_wptr, wptr.data(), sizeof(void*) * U,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrB.d_wsptr, wsptr.data(), sizeof(void*) * U,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrB.d_aptr, aptr.data(), sizeof(void*) * U,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrB.d_asfptr, asfptr.data(), sizeof(void*) * U,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrB.d_optr, optr.data(), sizeof(void*) * U,
                      cudaMemcpyHostToDevice));
        arrB.SetCols(dist.counts);
      }
      const int avg_m = (R + U - 1) / U;
      Plan planB = MakePlan(U, N, K, arrB.d_rows, arrB.d_cols, arrB.d_ld,
                            arrB.d_rows_w, arrB.d_cols_w, arrB.d_ld_w,
                            arrB.d_rows_o, arrB.d_cols_o, arrB.d_ld_o,
                            arrB.d_wsptr, arrB.d_asfptr, ws_bytes, avg_m);
      bool haveB = planB.ok;
      auto runB = [&]() {
        return cublasLtMatmul(GlobalLtHandle(), planB.op, arrB.d_alpha_p,
                              arrB.d_wptr, planB.w, arrB.d_aptr, planB.a,
                              arrB.d_beta_p, arrB.d_optr, planB.out,
                              arrB.d_optr, planB.out, &planB.algo, ws,
                              ws_bytes, stream);
      };

      // Mode C (padded): R groups, real 0..U-1, zero-column U..R-1.
      PlanArrays arrC(R, b.d_alpha, b.d_beta, N, K);
      {
        std::vector<const void*> wptr(R), wsptr(R), aptr(R), asfptr(R);
        std::vector<void*> optr(R);
        std::vector<int> cols(R), cols_o(R);
        for (int u = 0; u < R; ++u) {
          if (u < U) {
            const int e = group_expert[u];
            wptr[u] = b.d_w + static_cast<size_t>(e) * stride;
            wsptr[u] = b.d_wsf + static_cast<size_t>(e) * atom;
            aptr[u] = b.d_ag + static_cast<size_t>(u) * (K / 2);
            asfptr[u] = b.d_asfg + static_cast<size_t>(u) * atom;
            optr[u] = b.d_outB + static_cast<size_t>(goff[u]) * N;
            cols[u] = dist.counts[u];
            cols_o[u] = dist.counts[u];
          } else {
            const int e = group_expert[0];
            wptr[u] = b.d_w + static_cast<size_t>(e) * stride;
            wsptr[u] = b.d_wsf + static_cast<size_t>(e) * atom;
            aptr[u] = b.d_ag;
            asfptr[u] = b.d_asfg;
            optr[u] = b.d_outB;
            cols[u] = 0;
            cols_o[u] = 0;
          }
        }
        CK(cudaMemcpy(arrC.d_wptr, wptr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrC.d_wsptr, wsptr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrC.d_aptr, aptr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrC.d_asfptr, asfptr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrC.d_optr, optr.data(), sizeof(void*) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrC.d_cols, cols.data(), sizeof(int) * R,
                      cudaMemcpyHostToDevice));
        CK(cudaMemcpy(arrC.d_cols_o, cols_o.data(), sizeof(int) * R,
                      cudaMemcpyHostToDevice));
      }
      Plan planC = MakePlan(R, N, K, arrC.d_rows, arrC.d_cols, arrC.d_ld,
                            arrC.d_rows_w, arrC.d_cols_w, arrC.d_ld_w,
                            arrC.d_rows_o, arrC.d_cols_o, arrC.d_ld_o,
                            arrC.d_wsptr, arrC.d_asfptr, ws_bytes, 1);
      bool haveC = planC.ok;
      auto runC = [&]() {
        return cublasLtMatmul(GlobalLtHandle(), planC.op, arrC.d_alpha_p,
                              arrC.d_wptr, planC.w, arrC.d_aptr, planC.a,
                              arrC.d_beta_p, arrC.d_optr, planC.out,
                              arrC.d_optr, planC.out, &planC.algo, ws,
                              ws_bytes, stream);
      };

      auto bench = [&](const char* tag, auto run, int traffic_rows) {
        for (int i = 0; i < 3; ++i) {
          run_flush();
          LCK(run());
        }
        CK(cudaDeviceSynchronize());
        cudaEvent_t ev_a, ev_b;
        CK(cudaEventCreate(&ev_a));
        CK(cudaEventCreate(&ev_b));
        std::vector<float> per(iters);
        for (int i = 0; i < iters; ++i) {
          run_flush();
          CK(cudaEventRecord(ev_a, stream));
          LCK(run());
          CK(cudaEventRecord(ev_b, stream));
          CK(cudaDeviceSynchronize());
          CK(cudaEventElapsedTime(&per[i], ev_a, ev_b));
        }
        CK(cudaEventDestroy(ev_a));
        CK(cudaEventDestroy(ev_b));
        std::sort(per.begin(), per.end());
        double mean = 0;
        for (float v : per) mean += v;
        mean /= iters;
        // Weight traffic: perrow reads R experts' weights; grouped/padded
        // read U (zero-column groups read nothing).
        double traffic =
            static_cast<double>(traffic_rows) * N * (K / 2);
        double floor_ms = traffic / (bw * 1e9) * 1000.0;
        std::printf("%s %s %-7s U=%2d avg_m=%d median=%.3f ms mean=%.3f ms "
                    "grouped_floor=%.3f ms ratio=%.2fx eff_BW=%.0f GB/s\n",
                    dist.name, s.name, tag, U, avg_m, per[iters / 2], mean,
                    floor_ms, mean / floor_ms,
                    traffic / (mean / 1000.0) / 1e9);
      };

      bench("perrow", runA, R);
      if (haveB) bench("grouped", runB, U);
      if (haveC) bench("padded", runC, U);

      // Correctness: grouped rows vs per-row rows (same activation data).
      if (haveB) {
        if (std::getenv("Q4T_GRP_DIAG") != nullptr) {
          std::vector<const void*> hw(U), hws(U), ha(U), hasf(U);
          std::vector<void*> ho(U);
          std::vector<int> hcols(U);
          CK(cudaMemcpy(hw.data(), arrB.d_wptr, sizeof(void*)*U, cudaMemcpyDeviceToHost));
          CK(cudaMemcpy(hws.data(), arrB.d_wsptr, sizeof(void*)*U, cudaMemcpyDeviceToHost));
          CK(cudaMemcpy(ha.data(), arrB.d_aptr, sizeof(void*)*U, cudaMemcpyDeviceToHost));
          CK(cudaMemcpy(hasf.data(), arrB.d_asfptr, sizeof(void*)*U, cudaMemcpyDeviceToHost));
          CK(cudaMemcpy(ho.data(), arrB.d_optr, sizeof(void*)*U, cudaMemcpyDeviceToHost));
          CK(cudaMemcpy(hcols.data(), arrB.d_cols, sizeof(int)*U, cudaMemcpyDeviceToHost));
          for (int u = 0; u < U; ++u) {
            int e = group_expert[u];
            bool wbad = hw[u] != (b.d_w + (size_t)e*stride);
            bool abad = ha[u] != (b.d_ag + (size_t)u*(K/2));
            bool obad = ho[u] != (b.d_outB + (size_t)goff[u]*N);
            bool sbad = hasf[u] != (b.d_asfg + (size_t)u*atom);
            bool wsb = hws[u] != (b.d_wsf + (size_t)e*atom);
            if (wbad||abad||obad||sbad||wsb||hcols[u]!=dist.counts[u])
              std::printf("PTR group %d: w%s a%s o%s asf%s wsf%s cols=%d(exp %d)\n",
                u, wbad?"BAD":"ok", abad?"BAD":"ok", obad?"BAD":"ok",
                sbad?"BAD":"ok", wsb?"BAD":"ok", hcols[u], dist.counts[u]);
          }
          std::printf("PTR dump done (U=%d)\n", U);
        }
        LCK(runA());
        LCK(runB());
        CK(cudaDeviceSynchronize());
        std::vector<uint16_t> hA(static_cast<size_t>(R) * N),
            hB(static_cast<size_t>(R) * N);
        CK(cudaMemcpy(hA.data(), b.d_outA, hA.size() * 2,
                      cudaMemcpyDeviceToHost));
        CK(cudaMemcpy(hB.data(), b.d_outB, hB.size() * 2,
                      cudaMemcpyDeviceToHost));
        int exact = 0;
        double max_rel = 0.0;
        if (std::getenv("Q4T_GRP_DIAG") != nullptr) std::printf("DIAG wrong rows (%s %s):\n", dist.name, s.name);
        for (int r = 0; r < R; ++r) {
          const uint16_t* ra = hA.data() + static_cast<size_t>(r) * N;
          const uint16_t* rb =
              hB.data() + static_cast<size_t>(row_dst[r]) * N;
          int bad = 0;
          for (int c = 0; c < N; ++c) {
            if (ra[c] == rb[c]) {
              ++exact;
              continue;
            }
            ++bad;
            const float fa = __bfloat162float(
                *reinterpret_cast<const __nv_bfloat16*>(&ra[c]));
            const float fb = __bfloat162float(
                *reinterpret_cast<const __nv_bfloat16*>(&rb[c]));
            const float denom = std::fmaxf(std::fabs(fa), 1e-6f);
            max_rel = std::fmax(max_rel,
                                static_cast<double>(std::fabs(fa - fb) / denom));
          }
          if (bad && std::getenv("Q4T_GRP_DIAG") != nullptr) {
            int u = row_group[r], i = row_local[r];
            std::printf("row %2d (group %2d local %d): %d/%d bad", r, u, i, bad, N);
            int match_row = -1;
            for (int r2 = 0; r2 < R; ++r2) {
              int same = 0;
              for (int c = 0; c < N; ++c)
                if (rb[c] == hA.data()[static_cast<size_t>(r2) * N + c]) ++same;
              if (same == N) { match_row = r2; break; }
            }
            if (match_row >= 0) std::printf(" == perrow row %d", match_row);
            int partner = -1;
            for (int r2 = 0; r2 < R; ++r2)
              if (row_group[r2] == u && r2 != r) partner = r2;
            if (partner >= 0) {
              int same = 0;
              for (int c = 0; c < N; ++c)
                if (rb[c] == hB.data()[static_cast<size_t>(partner) * N + c]) ++same;
              if (same == N) std::printf(" == grouped partner row %d", partner);
            }
            std::printf("  c0: A=%g B=%g  c1: A=%g B=%g\n",
                        __bfloat162float(*reinterpret_cast<const __nv_bfloat16*>(&ra[0])),
                        __bfloat162float(*reinterpret_cast<const __nv_bfloat16*>(&rb[0])),
                        __bfloat162float(*reinterpret_cast<const __nv_bfloat16*>(&ra[1])),
                        __bfloat162float(*reinterpret_cast<const __nv_bfloat16*>(&rb[1])));
          }
        }
        std::printf("%s %s check: exact=%d/%d max_rel=%.3e\n", dist.name,
                    s.name, exact, R * N, max_rel);
      }
      if (haveC) {
        LCK(runA());
        LCK(runC());
        CK(cudaDeviceSynchronize());
        std::vector<uint16_t> hA(static_cast<size_t>(R) * N),
            hC(static_cast<size_t>(R) * N);
        CK(cudaMemcpy(hA.data(), b.d_outA, hA.size() * 2,
                      cudaMemcpyDeviceToHost));
        CK(cudaMemcpy(hC.data(), b.d_outB, hC.size() * 2,
                      cudaMemcpyDeviceToHost));
        int exact = 0;
        double max_rel = 0.0;
        for (int r = 0; r < R; ++r) {
          const uint16_t* ra = hA.data() + static_cast<size_t>(r) * N;
          const uint16_t* rc =
              hC.data() + static_cast<size_t>(row_dst[r]) * N;
          for (int c = 0; c < N; ++c) {
            if (ra[c] == rc[c]) {
              ++exact;
              continue;
            }
            const float fa = __bfloat162float(
                *reinterpret_cast<const __nv_bfloat16*>(&ra[c]));
            const float fc = __bfloat162float(
                *reinterpret_cast<const __nv_bfloat16*>(&rc[c]));
            const float denom = std::fmaxf(std::fabs(fa), 1e-6f);
            max_rel = std::fmax(max_rel,
                                static_cast<double>(std::fabs(fa - fc) / denom));
          }
        }
        std::printf("%s %s checkC: exact=%d/%d max_rel=%.3e\n", dist.name,
                    s.name, exact, R * N, max_rel);
      }

      DestroyPlan(planA);
      DestroyPlan(planB);
      DestroyPlan(planC);
    }

    CK(cudaFree(b.d_w)); CK(cudaFree(b.d_wsf)); CK(cudaFree(b.d_a));
    CK(cudaFree(b.d_ag)); CK(cudaFree(b.d_asf)); CK(cudaFree(b.d_asfg));
    CK(cudaFree(b.d_outA)); CK(cudaFree(b.d_outB));
    CK(cudaFree(b.d_alpha)); CK(cudaFree(b.d_beta));
  }
  CK(cudaFree(flush));
  CK(cudaFree(sink));
  CK(cudaFree(ws));
  CK(cudaStreamDestroy(stream));
  return 0;
}
