// Per-row grouped NVFP4 GEMM (verify T=4 shapes) vs DRAM floor.
// Characterizes the moe_decode.cu grouped path: one call per MoE layer with
// R = T*10 rows (T=4 -> R=40). Production sub-timing sums 36 layers per
// forward (gemm_gu ~11ms/forward ~= 300us/layer); this bench measures the
// per-call cost. Result (2026-10-13): both the contiguous-slice mode and the
// production-like mode (512-expert buffer, scattered expert pointers, L2
// flush before each call) hit ~1.0-1.2x the HBM read floor (~246 GB/s),
// matching the nsys profile of the real verify step (nvjet ptrGroup
// ~230us gu / ~118us dn per layer). The grouped GEMM is already near the
// DRAM floor; remaining MoE headroom is expert-weight L2 residency.
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


// Production-like mode: expert weights live in one big E-expert buffer (like
// gu_packed/dn_packed), rows point at scattered expert offsets, and an L2
// flush (256 MB read stream) precedes every measured GEMM to emulate the
// attention/linear traffic that dirties L2 before each MoE layer.
int RunProductionMode(int iters, double bw) {
  const int E = 512;
  const int R = 40;
  struct Shape { int N, K; const char* name; };
  Shape shapes[] = {{1280, 2560, "gemm_gu"}, {2560, 640, "gemm_dn"}};
  size_t ws_bytes = 32 * 1024 * 1024;
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

  for (auto& s : shapes) {
    const size_t stride = static_cast<size_t>(s.N) * (s.K / 2);
    size_t w_bytes = static_cast<size_t>(E) * stride;
    uint8_t* d_w;
    CK(cudaMalloc(&d_w, w_bytes));
    CK(cudaMemset(d_w, 0x11, w_bytes));
    size_t wsf_bytes = static_cast<size_t>(E) * SfBufferSize(1, s.K);
    uint8_t* d_wsf;
    CK(cudaMalloc(&d_wsf, wsf_bytes));
    CK(cudaMemset(d_wsf, 0x33, wsf_bytes));
    size_t a_bytes = static_cast<size_t>(R) * (s.K / 2);
    uint8_t* d_a;
    CK(cudaMalloc(&d_a, a_bytes));
    CK(cudaMemset(d_a, 0x11, a_bytes));
    size_t asf_bytes = static_cast<size_t>(R) * SfBufferSize(1, s.K);
    uint8_t* d_asf;
    CK(cudaMalloc(&d_asf, asf_bytes));
    CK(cudaMemset(d_asf, 0x33, asf_bytes));
    uint16_t* d_out;
    CK(cudaMalloc(&d_out, static_cast<size_t>(R) * s.N * 2));

    std::vector<const void*> h_wptr(R), h_wsptr(R), h_aptr(R), h_asfptr(R);
    std::vector<void*> h_optr(R);
    std::vector<float> h_alpha(R, 1.0f), h_beta(R, 0.0f);
    std::vector<int> h_dim_k(R, s.K), h_dim_n(R, s.N), h_dim_one(R, 1);
    for (int r = 0; r < R; ++r) {
      const int e = (r * 13) % E;  // 40 distinct experts
      h_wptr[r] = d_w + e * stride;
      h_wsptr[r] = d_wsf + SfBufferSize(1, s.K) * e;
      h_aptr[r] = d_a + static_cast<size_t>(r) * (s.K / 2);
      h_asfptr[r] = d_asf + SfBufferSize(1, s.K) * r;
      h_optr[r] = d_out + static_cast<size_t>(r) * s.N;
    }
    void* d_wptr; CK(cudaMalloc(&d_wptr, sizeof(void*) * R));
    CK(cudaMemcpy(d_wptr, h_wptr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    void* d_wsptr; CK(cudaMalloc(&d_wsptr, sizeof(void*) * R));
    CK(cudaMemcpy(d_wsptr, h_wsptr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    void* d_aptr; CK(cudaMalloc(&d_aptr, sizeof(void*) * R));
    CK(cudaMemcpy(d_aptr, h_aptr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    void* d_asfptr; CK(cudaMalloc(&d_asfptr, sizeof(void*) * R));
    CK(cudaMemcpy(d_asfptr, h_asfptr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    void* d_optr; CK(cudaMalloc(&d_optr, sizeof(void*) * R));
    CK(cudaMemcpy(d_optr, h_optr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    float* d_alpha; CK(cudaMalloc(&d_alpha, sizeof(float) * R));
    CK(cudaMemcpy(d_alpha, h_alpha.data(), sizeof(float) * R, cudaMemcpyHostToDevice));
    float* d_beta; CK(cudaMalloc(&d_beta, sizeof(float) * R));
    CK(cudaMemcpy(d_beta, h_beta.data(), sizeof(float) * R, cudaMemcpyHostToDevice));
    int* d_dim_k; CK(cudaMalloc(&d_dim_k, sizeof(int) * R));
    CK(cudaMemcpy(d_dim_k, h_dim_k.data(), sizeof(int) * R, cudaMemcpyHostToDevice));
    int* d_dim_n; CK(cudaMalloc(&d_dim_n, sizeof(int) * R));
    CK(cudaMemcpy(d_dim_n, h_dim_n.data(), sizeof(int) * R, cudaMemcpyHostToDevice));
    int* d_dim_one; CK(cudaMalloc(&d_dim_one, sizeof(int) * R));
    CK(cudaMemcpy(d_dim_one, h_dim_one.data(), sizeof(int) * R, cudaMemcpyHostToDevice));
    std::vector<const float*> h_ap(R), h_bp(R);
    for (int r = 0; r < R; ++r) { h_ap[r] = d_alpha + r; h_bp[r] = d_beta + r; }
    void* d_ap; CK(cudaMalloc(&d_ap, sizeof(const float*) * R));
    CK(cudaMemcpy(d_ap, h_ap.data(), sizeof(const float*) * R, cudaMemcpyHostToDevice));
    void* d_bp; CK(cudaMalloc(&d_bp, sizeof(const float*) * R));
    CK(cudaMemcpy(d_bp, h_bp.data(), sizeof(const float*) * R, cudaMemcpyHostToDevice));

    cublasLtMatmulDesc_t op;
    LCK(cublasLtMatmulDescCreate(&op, CUBLAS_COMPUTE_32F, CUDA_R_32F));
    cublasOperation_t trans_w = CUBLAS_OP_T;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSA, &trans_w, sizeof(trans_w)));
    cublasLtPointerMode_t mode = CUBLASLT_POINTER_MODE_DEVICE;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_POINTER_MODE, &mode, sizeof(mode)));
    int64_t stride64 = 1;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_ALPHA_BATCH_STRIDE, &stride64, sizeof(stride64)));
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_BETA_BATCH_STRIDE, &stride64, sizeof(stride64)));
    auto sf_mode = CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_A_SCALE_MODE, &sf_mode, sizeof(sf_mode)));
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_B_SCALE_MODE, &sf_mode, sizeof(sf_mode)));
    cublasLtMatrixLayout_t w, a, out;
    LCK(cublasLtGroupedMatrixLayoutCreate(&w, CUDA_R_4F_E2M1, R, d_dim_k, d_dim_n, d_dim_k));
    LCK(cublasLtGroupedMatrixLayoutCreate(&a, CUDA_R_4F_E2M1, R, d_dim_k, d_dim_one, d_dim_k));
    LCK(cublasLtGroupedMatrixLayoutCreate(&out, CUDA_R_16BF, R, d_dim_n, d_dim_one, d_dim_n));
    const int* dims[3][3] = {{d_dim_k, d_dim_n, d_dim_k},
                             {d_dim_k, d_dim_one, d_dim_k},
                             {d_dim_n, d_dim_one, d_dim_n}};
    cublasLtMatrixLayout_t layouts[3] = {w, a, out};
    cublasLtMatrixLayoutAttribute_t attrs[3] = {
        CUBLASLT_GROUPED_MATRIX_LAYOUT_ROWS_ARRAY,
        CUBLASLT_GROUPED_MATRIX_LAYOUT_COLS_ARRAY,
        CUBLASLT_GROUPED_MATRIX_LAYOUT_LD_ARRAY};
    auto int_width = CUBLASLT_INTEGER_WIDTH_32;
    for (int i = 0; i < 3; ++i) {
      for (int j = 0; j < 3; ++j)
        LCK(cublasLtMatrixLayoutSetAttribute(layouts[i], attrs[j], &dims[i][j], sizeof(dims[i][j])));
      LCK(cublasLtMatrixLayoutSetAttribute(layouts[i],
          CUBLASLT_GROUPED_MATRIX_LAYOUT_ROWS_COLS_ARRAY_INTEGER_WIDTH, &int_width, sizeof(int_width)));
      LCK(cublasLtMatrixLayoutSetAttribute(layouts[i],
          CUBLASLT_GROUPED_MATRIX_LAYOUT_LD_ARRAY_INTEGER_WIDTH, &int_width, sizeof(int_width)));
    }
    const void* w_sf = d_wsptr;
    const void* a_sf = d_asfptr;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_A_SCALE_POINTER, &w_sf, sizeof(w_sf)));
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_B_SCALE_POINTER, &a_sf, sizeof(a_sf)));
    cublasLtMatmulPreferenceOpaque_t pref;
    LCK(cublasLtMatmulPreferenceInit(&pref));
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &ws_bytes, sizeof(ws_bytes)));
    int64_t avg_n = s.N, avg_m = 1, avg_k = s.K;
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_GROUPED_DESC_D_AVERAGE_ROWS, &avg_n, sizeof(avg_n)));
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_GROUPED_DESC_D_AVERAGE_COLS, &avg_m, sizeof(avg_m)));
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_GROUPED_AVERAGE_REDUCTION_DIM, &avg_k, sizeof(avg_k)));
    uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK, &reduction, sizeof(reduction)));
    cublasLtMatmulHeuristicResult_t heur{};
    int count = 0;
    LCK(cublasLtMatmulAlgoGetHeuristic(GlobalLtHandle(), op, w, a, out, out, &pref, 1, &heur, &count));
    if (count != 1 || heur.state != CUBLAS_STATUS_SUCCESS) {
      std::printf("no algo (prod) R=%d N=%d K=%d\n", R, s.N, s.K);
      return 2;
    }
    auto run_gemm = [&]() {
      return cublasLtMatmul(GlobalLtHandle(), op, d_ap, d_wptr, w, d_aptr, a,
                            d_bp, d_optr, out, d_optr, out, &heur.algo, ws,
                            ws_bytes, stream);
    };
    auto run_flush = [&]() {
      ReadBWKernel<<<fgrid, fblock, 0, stream>>>(flush, flush_bytes / 16, sink);
    };
    for (int i = 0; i < 3; ++i) { run_flush(); LCK(run_gemm()); }
    CK(cudaDeviceSynchronize());
    cudaEvent_t ev_a, ev_b;
    CK(cudaEventCreate(&ev_a));
    CK(cudaEventCreate(&ev_b));
    std::vector<float> per(iters);
    for (int i = 0; i < iters; ++i) {
      run_flush();
      CK(cudaEventRecord(ev_a, stream));
      LCK(run_gemm());
      CK(cudaEventRecord(ev_b, stream));
      CK(cudaDeviceSynchronize());
      CK(cudaEventElapsedTime(&per[i], ev_a, ev_b));
    }
    std::sort(per.begin(), per.end());
    double mean = 0;
    for (float v : per) mean += v;
    mean /= iters;
    double w_traffic = static_cast<double>(R) * s.N * (s.K / 2);
    double floor_ms = w_traffic / (bw * 1e9) * 1000.0;
    std::printf("%s (prod): R=%d N=%d K=%d median=%.2f ms mean=%.2f ms  "
                "traffic=%.1fMB  BW_floor=%.2f ms  ratio=%.1fx  eff_BW=%.0f GB/s\n",
                s.name, R, s.N, s.K, per[iters / 2], mean, w_traffic / 1e6,
                floor_ms, mean / floor_ms, w_traffic / (mean / 1000.0) / 1e9);
    CK(cudaFree(d_w)); CK(cudaFree(d_wsf)); CK(cudaFree(d_a)); CK(cudaFree(d_asf));
    CK(cudaFree(d_out)); CK(cudaFree(d_wptr)); CK(cudaFree(d_wsptr));
    CK(cudaFree(d_aptr)); CK(cudaFree(d_asfptr)); CK(cudaFree(d_optr));
    CK(cudaFree(d_alpha)); CK(cudaFree(d_beta)); CK(cudaFree(d_dim_k));
    CK(cudaFree(d_dim_n)); CK(cudaFree(d_dim_one)); CK(cudaFree(d_ap));
    CK(cudaFree(d_bp));
    cublasLtMatrixLayoutDestroy(out);
    cublasLtMatrixLayoutDestroy(a);
    cublasLtMatrixLayoutDestroy(w);
    cublasLtMatmulDescDestroy(op);
  }
  CK(cudaFree(flush));
  CK(cudaFree(sink));
  CK(cudaFree(ws));
  CK(cudaStreamDestroy(stream));
  return 0;
}

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
  if (argc > 2 && std::string(argv[2]) == "prod") return RunProductionMode(iters, bw);

  const int R = 40;
  struct Shape { int N, K; const char* name; };
  Shape shapes[] = {{1280, 2560, "gemm_gu"}, {2560, 640, "gemm_dn"}};
  size_t ws_bytes = 32 * 1024 * 1024;
  void* ws;
  CK(cudaMalloc(&ws, ws_bytes));
  cudaStream_t stream;
  CK(cudaStreamCreate(&stream));

  for (auto& s : shapes) {
    // R distinct experts, each N x K/2 packed.
    size_t w_bytes = static_cast<size_t>(R) * s.N * (s.K / 2);
    std::vector<uint8_t> h_w(w_bytes);
    for (auto& b : h_w)
      b = static_cast<uint8_t>((rand() & 0xF) | ((rand() & 0xF) << 4));
    uint8_t* d_w;
    CK(cudaMalloc(&d_w, w_bytes));
    CK(cudaMemcpy(d_w, h_w.data(), w_bytes, cudaMemcpyHostToDevice));
    size_t wsf = SfBufferSize(1, s.K) * R;
    std::vector<uint8_t> h_wsf(wsf);
    for (auto& b : h_wsf) b = static_cast<uint8_t>(rand() & 0x7F);
    uint8_t* d_wsf;
    CK(cudaMalloc(&d_wsf, wsf));
    CK(cudaMemcpy(d_wsf, h_wsf.data(), wsf, cudaMemcpyHostToDevice));
    size_t a_bytes = static_cast<size_t>(R) * (s.K / 2);
    std::vector<uint8_t> h_a(a_bytes);
    for (auto& b : h_a) b = static_cast<uint8_t>(rand() & 0xFF);
    uint8_t* d_a;
    CK(cudaMalloc(&d_a, a_bytes));
    CK(cudaMemcpy(d_a, h_a.data(), a_bytes, cudaMemcpyHostToDevice));
    size_t asf = SfBufferSize(R, s.K);
    std::vector<uint8_t> h_asf(asf);
    for (auto& b : h_asf) b = static_cast<uint8_t>(rand() & 0x7F);
    uint8_t* d_asf;
    CK(cudaMalloc(&d_asf, asf));
    CK(cudaMemcpy(d_asf, h_asf.data(), asf, cudaMemcpyHostToDevice));
    uint16_t* d_out;
    CK(cudaMalloc(&d_out, static_cast<size_t>(R) * s.N * 2));

    // per-row pointer arrays (device)
    std::vector<const void*> h_wptr(R), h_wsptr(R), h_aptr(R), h_asfptr(R);
    std::vector<void*> h_optr(R);
    std::vector<float> h_alpha(R, 1.0f), h_beta(R, 0.0f);
    std::vector<int> h_dim_k(R, s.K), h_dim_n(R, s.N), h_dim_one(R, 1);
    for (int r = 0; r < R; ++r) {
      h_wptr[r] = d_w + static_cast<size_t>(r) * s.N * (s.K / 2);
      h_wsptr[r] = d_wsf + SfBufferSize(1, s.K) * r;
      h_aptr[r] = d_a + static_cast<size_t>(r) * (s.K / 2);
      h_asfptr[r] = d_asf;
      h_optr[r] = d_out + static_cast<size_t>(r) * s.N;
    }
    void* d_wptr; CK(cudaMalloc(&d_wptr, sizeof(void*) * R));
    CK(cudaMemcpy(d_wptr, h_wptr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    void* d_wsptr; CK(cudaMalloc(&d_wsptr, sizeof(void*) * R));
    CK(cudaMemcpy(d_wsptr, h_wsptr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    void* d_aptr; CK(cudaMalloc(&d_aptr, sizeof(void*) * R));
    CK(cudaMemcpy(d_aptr, h_aptr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    void* d_asfptr; CK(cudaMalloc(&d_asfptr, sizeof(void*) * R));
    CK(cudaMemcpy(d_asfptr, h_asfptr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    void* d_optr; CK(cudaMalloc(&d_optr, sizeof(void*) * R));
    CK(cudaMemcpy(d_optr, h_optr.data(), sizeof(void*) * R, cudaMemcpyHostToDevice));
    float* d_alpha; CK(cudaMalloc(&d_alpha, sizeof(float) * R));
    CK(cudaMemcpy(d_alpha, h_alpha.data(), sizeof(float) * R, cudaMemcpyHostToDevice));
    float* d_beta; CK(cudaMalloc(&d_beta, sizeof(float) * R));
    CK(cudaMemcpy(d_beta, h_beta.data(), sizeof(float) * R, cudaMemcpyHostToDevice));
    int* d_dim_k; CK(cudaMalloc(&d_dim_k, sizeof(int) * R));
    CK(cudaMemcpy(d_dim_k, h_dim_k.data(), sizeof(int) * R, cudaMemcpyHostToDevice));
    int* d_dim_n; CK(cudaMalloc(&d_dim_n, sizeof(int) * R));
    CK(cudaMemcpy(d_dim_n, h_dim_n.data(), sizeof(int) * R, cudaMemcpyHostToDevice));
    int* d_dim_one; CK(cudaMalloc(&d_dim_one, sizeof(int) * R));
    CK(cudaMemcpy(d_dim_one, h_dim_one.data(), sizeof(int) * R, cudaMemcpyHostToDevice));
    // alpha/beta ptr arrays point into d_alpha/d_beta per row
    std::vector<const float*> h_ap(R), h_bp(R);
    for (int r = 0; r < R; ++r) { h_ap[r] = d_alpha + r; h_bp[r] = d_beta + r; }
    void* d_ap; CK(cudaMalloc(&d_ap, sizeof(const float*) * R));
    CK(cudaMemcpy(d_ap, h_ap.data(), sizeof(const float*) * R, cudaMemcpyHostToDevice));
    void* d_bp; CK(cudaMalloc(&d_bp, sizeof(const float*) * R));
    CK(cudaMemcpy(d_bp, h_bp.data(), sizeof(const float*) * R, cudaMemcpyHostToDevice));

    // descriptors
    cublasLtMatmulDesc_t op;
    LCK(cublasLtMatmulDescCreate(&op, CUBLAS_COMPUTE_32F, CUDA_R_32F));
    cublasOperation_t trans_w = CUBLAS_OP_T;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSA, &trans_w, sizeof(trans_w)));
    cublasLtPointerMode_t mode = CUBLASLT_POINTER_MODE_DEVICE;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_POINTER_MODE, &mode, sizeof(mode)));
    int64_t stride = 1;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_ALPHA_BATCH_STRIDE, &stride, sizeof(stride)));
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_BETA_BATCH_STRIDE, &stride, sizeof(stride)));
    auto sf_mode = CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_A_SCALE_MODE, &sf_mode, sizeof(sf_mode)));
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_B_SCALE_MODE, &sf_mode, sizeof(sf_mode)));
    cublasLtMatrixLayout_t w, a, out;
    LCK(cublasLtGroupedMatrixLayoutCreate(&w, CUDA_R_4F_E2M1, R, d_dim_k,
                                          d_dim_n, d_dim_k));
    LCK(cublasLtGroupedMatrixLayoutCreate(&a, CUDA_R_4F_E2M1, R, d_dim_k,
                                          d_dim_one, d_dim_k));
    LCK(cublasLtGroupedMatrixLayoutCreate(&out, CUDA_R_16BF, R, d_dim_n,
                                          d_dim_one, d_dim_n));
    const int* dims[3][3] = {{d_dim_k, d_dim_n, d_dim_k},
                             {d_dim_k, d_dim_one, d_dim_k},
                             {d_dim_n, d_dim_one, d_dim_n}};
    cublasLtMatrixLayout_t layouts[3] = {w, a, out};
    cublasLtMatrixLayoutAttribute_t attrs[3] = {
        CUBLASLT_GROUPED_MATRIX_LAYOUT_ROWS_ARRAY,
        CUBLASLT_GROUPED_MATRIX_LAYOUT_COLS_ARRAY,
        CUBLASLT_GROUPED_MATRIX_LAYOUT_LD_ARRAY};
    auto int_width = CUBLASLT_INTEGER_WIDTH_32;
    for (int i = 0; i < 3; ++i) {
      for (int j = 0; j < 3; ++j)
        LCK(cublasLtMatrixLayoutSetAttribute(layouts[i], attrs[j], &dims[i][j], sizeof(dims[i][j])));
      LCK(cublasLtMatrixLayoutSetAttribute(layouts[i],
          CUBLASLT_GROUPED_MATRIX_LAYOUT_ROWS_COLS_ARRAY_INTEGER_WIDTH, &int_width, sizeof(int_width)));
      LCK(cublasLtMatrixLayoutSetAttribute(layouts[i],
          CUBLASLT_GROUPED_MATRIX_LAYOUT_LD_ARRAY_INTEGER_WIDTH, &int_width, sizeof(int_width)));
    }
    const void* w_sf = d_wsptr;
    const void* a_sf = d_asfptr;
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_A_SCALE_POINTER, &w_sf, sizeof(w_sf)));
    LCK(cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_B_SCALE_POINTER, &a_sf, sizeof(a_sf)));
    cublasLtMatmulPreferenceOpaque_t pref;
    LCK(cublasLtMatmulPreferenceInit(&pref));
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &ws_bytes, sizeof(ws_bytes)));
    int64_t avg_n = s.N, avg_m = 1, avg_k = s.K;
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_GROUPED_DESC_D_AVERAGE_ROWS, &avg_n, sizeof(avg_n)));
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_GROUPED_DESC_D_AVERAGE_COLS, &avg_m, sizeof(avg_m)));
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_GROUPED_AVERAGE_REDUCTION_DIM, &avg_k, sizeof(avg_k)));
    uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
    LCK(cublasLtMatmulPreferenceSetAttribute(&pref, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK, &reduction, sizeof(reduction)));
    cublasLtMatmulHeuristicResult_t heur{};
    int count = 0;
    LCK(cublasLtMatmulAlgoGetHeuristic(GlobalLtHandle(), op, w, a, out, out, &pref, 1, &heur, &count));
    if (count != 1 || heur.state != CUBLAS_STATUS_SUCCESS) {
      std::printf("no algo R=%d N=%d K=%d\n", R, s.N, s.K);
      return 2;
    }
    // time
    for (int i = 0; i < 3; ++i)
      LCK(cublasLtMatmul(GlobalLtHandle(), op, d_ap, d_wptr, w, d_aptr, a,
                        d_bp, d_optr, out, d_optr, out, &heur.algo, ws, ws_bytes, stream));
    CK(cudaDeviceSynchronize());
    cudaEvent_t ev_a, ev_b;
    CK(cudaEventCreate(&ev_a));
    CK(cudaEventCreate(&ev_b));
    CK(cudaEventRecord(ev_a, stream));
    for (int i = 0; i < iters; ++i)
      LCK(cublasLtMatmul(GlobalLtHandle(), op, d_ap, d_wptr, w, d_aptr, a,
                        d_bp, d_optr, out, d_optr, out, &heur.algo, ws, ws_bytes, stream));
    CK(cudaEventRecord(ev_b, stream));
    CK(cudaDeviceSynchronize());
    float ms = 0;
    CK(cudaEventElapsedTime(&ms, ev_a, ev_b));
    double w_traffic = static_cast<double>(R) * s.N * (s.K / 2);
    double floor_ms = w_traffic / (bw * 1e9) * 1000.0;
    std::printf("%s: R=%d N=%d K=%d cublasLt=%.2f ms  traffic=%.1fMB  "
                "BW_floor=%.2f ms  ratio=%.1fx  eff_BW=%.0f GB/s\n",
                s.name, R, s.N, s.K, ms / iters, w_traffic / 1e6, floor_ms,
                (ms / iters) / floor_ms, w_traffic / (ms / iters / 1000.0) / 1e9);
    // cleanup
    CK(cudaFree(d_w)); CK(cudaFree(d_wsf)); CK(cudaFree(d_a)); CK(cudaFree(d_asf));
    CK(cudaFree(d_out)); CK(cudaFree(d_wptr)); CK(cudaFree(d_wsptr));
    CK(cudaFree(d_aptr)); CK(cudaFree(d_asfptr)); CK(cudaFree(d_optr));
    CK(cudaFree(d_alpha)); CK(cudaFree(d_beta)); CK(cudaFree(d_dim_k));
    CK(cudaFree(d_dim_n)); CK(cudaFree(d_dim_one)); CK(cudaFree(d_ap));
    CK(cudaFree(d_bp));
    cublasLtMatrixLayoutDestroy(out);
    cublasLtMatrixLayoutDestroy(a);
    cublasLtMatrixLayoutDestroy(w);
    cublasLtMatmulDescDestroy(op);
  }
  CK(cudaFree(ws));
  CK(cudaStreamDestroy(stream));
  std::printf("done\n");
  return 0;
}
