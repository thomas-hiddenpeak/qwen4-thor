// W8A16 FP8 GEMV feasibility spike (decode projection lever).
//
// Decode's #1 cost is the BF16 projection GEMVs (q/k/v/o + DeltaNet + lm_head),
// ~58% of a single-request decode step (nsys 2026-09-16). They are DRAM-bound
// on the BF16 weight bytes. Storing the weight as FP8 e4m3 (1 byte vs 2) with a
// per-output-channel scale halves the weight traffic; the activation stays
// BF16 (W8A16), so there is no activation LUT (unlike the W4A4 MoE GEMV, which
// could not beat cuBLASLt — see fp4-gemv-negative-result.md). The FP8->float
// convert is a single hardware cvt, so the kernel should stay bandwidth-bound
// and run ~2x faster than the BF16 GEMV.
//
// This standalone spike measures, per real projection shape:
//   - BF16 GEMV time (baseline, copied from src/model/gemv.cu)
//   - FP8 W8A16 GEMV time (this file)
//   - speedup + achieved DRAM bandwidth
//   - quality: L2-rel of each output vs an FP32 reference, and FP8 vs BF16
//
// Synthetic Gaussian weights (data-independent bandwidth; quality is a ballpark
// proxy for the real-weight validation the full effort would run). No model
// load. Build: q4t_fp8_gemv_proto. Run: ./build/q4t_fp8_gemv_proto
#include <cuda_bf16.h>
#include <cuda_fp8.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

namespace {

#define CK(x)                                                             \
  do {                                                                    \
    cudaError_t e = (x);                                                  \
    if (e != cudaSuccess) {                                               \
      std::printf("CUDA error %s:%d: %s\n", __FILE__, __LINE__,           \
                  cudaGetErrorString(e));                                 \
      std::exit(1);                                                       \
    }                                                                     \
  } while (0)

constexpr int kThreads = 256;  // 8 warps/block

// ---- BF16 GEMV baseline (mirrors src/model/gemv.cu Bf16GevKernel) ----------
__device__ __forceinline__ void F32x2Fma(uint64_t& acc, const float2& a,
                                          const float2& b) {
  asm volatile("fma.rn.f32x2 %0, %1, %2, %0;"
               : "+l"(acc)
               : "l"(reinterpret_cast<const uint64_t&>(a)),
                 "l"(reinterpret_cast<const uint64_t&>(b)));
}
__device__ __forceinline__ float F32x2Reduce(uint64_t acc) {
  float2 v;
  std::memcpy(&v, &acc, 8);
  return v.x + v.y;
}

__global__ void Bf16GevKernel(const uint16_t* __restrict__ w,
                              const uint16_t* __restrict__ x,
                              uint16_t* __restrict__ y, int N, int K,
                              float alpha) {
  constexpr int kWarp = 32;
  constexpr int kWarps = kThreads / kWarp;
  extern __shared__ uint16_t s_x[];
  const int warp_id = threadIdx.x / kWarp;
  const int lane = threadIdx.x & (kWarp - 1);
  const int num_blocks = (N + kWarps - 1) / kWarps;
  const int out_idx = blockIdx.x + warp_id * num_blocks;
  for (int i = threadIdx.x; i < K; i += kThreads) s_x[i] = x[i];
  __syncthreads();
  if (out_idx >= N) return;
  const uint16_t* wrow = w + static_cast<size_t>(out_idx) * K;
  uint64_t acc = 0;
  const int k8 = K / 8;
  const float4* w_v4 = reinterpret_cast<const float4*>(wrow);
  const float4* x_v4 = reinterpret_cast<const float4*>(s_x);
  for (int i = lane; i < k8; i += kWarp) {
    const float4 w4 = w_v4[i];
    const float4 x4 = x_v4[i];
    const __nv_bfloat162* w2 = reinterpret_cast<const __nv_bfloat162*>(&w4);
    const __nv_bfloat162* x2 = reinterpret_cast<const __nv_bfloat162*>(&x4);
#pragma unroll
    for (int j = 0; j < 4; ++j)
      F32x2Fma(acc, __bfloat1622float2(x2[j]), __bfloat1622float2(w2[j]));
  }
  float sum = F32x2Reduce(acc);
  for (int k = k8 * 8 + lane; k < K; k += kWarp)
    sum += __bfloat162float(__ushort_as_bfloat16(s_x[k])) *
           __bfloat162float(__ushort_as_bfloat16(wrow[k]));
#pragma unroll
  for (int off = 16; off > 0; off >>= 1)
    sum += __shfl_down_sync(0xffffffff, sum, off);
  if (lane == 0) y[out_idx] = __bfloat16_as_ushort(__float2bfloat16(alpha * sum));
}

// ---- FP8 W8A16 GEMV (weight e4m3 + per-output-channel scale) ---------------
// wrow = W_fp8 [N, K] (e4m3, 1 byte/elem), wscale[N] per-channel float. The
// reconstructed weight is float(fp8) * wscale[out_idx]; the dot is scaled once
// at the end. Reads 16 e4m3 (uint4, 16B) per iter vs the BF16 path's 8 bf16.
__global__ void Fp8GevKernel(const uint8_t* __restrict__ w,
                             const float* __restrict__ wscale,
                             const uint16_t* __restrict__ x,
                             uint16_t* __restrict__ y, int N, int K,
                             float alpha) {
  constexpr int kWarp = 32;
  constexpr int kWarps = kThreads / kWarp;
  extern __shared__ uint16_t s_x[];
  const int warp_id = threadIdx.x / kWarp;
  const int lane = threadIdx.x & (kWarp - 1);
  const int num_blocks = (N + kWarps - 1) / kWarps;
  const int out_idx = blockIdx.x + warp_id * num_blocks;
  for (int i = threadIdx.x; i < K; i += kThreads) s_x[i] = x[i];
  __syncthreads();
  if (out_idx >= N) return;
  const uint8_t* wrow = w + static_cast<size_t>(out_idx) * K;
  float sum = 0.0f;
  const int k16 = K / 16;
  const uint4* w_v16 = reinterpret_cast<const uint4*>(wrow);
  for (int i = lane; i < k16; i += kWarp) {
    const uint4 raw = w_v16[i];
    const __nv_fp8_e4m3* wf = reinterpret_cast<const __nv_fp8_e4m3*>(&raw);
    const int base = i * 16;
#pragma unroll
    for (int j = 0; j < 16; ++j)
      sum += __bfloat162float(__ushort_as_bfloat16(s_x[base + j])) *
             static_cast<float>(wf[j]);
  }
  for (int k = k16 * 16 + lane; k < K; k += kWarp) {
    __nv_fp8_e4m3 wv;
    wv.__x = wrow[k];
    sum += __bfloat162float(__ushort_as_bfloat16(s_x[k])) *
           static_cast<float>(wv);
  }
  sum *= wscale[out_idx];
#pragma unroll
  for (int off = 16; off > 0; off >>= 1)
    sum += __shfl_down_sync(0xffffffff, sum, off);
  if (lane == 0) y[out_idx] = __bfloat16_as_ushort(__float2bfloat16(alpha * sum));
}

float Bf16(float f) {  // round-trip through bf16 storage
  return __bfloat162float(__float2bfloat16(f));
}

double L2Rel(const std::vector<float>& a, const std::vector<float>& ref) {
  double num = 0, den = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    double d = a[i] - ref[i];
    num += d * d;
    den += (double)ref[i] * ref[i];
  }
  return std::sqrt(num / (den + 1e-30));
}

struct Shape {
  const char* name;
  int N, K;
};

void RunShape(const Shape& s) {
  const int N = s.N, K = s.K;
  std::mt19937 rng(1234 + N + K);
  std::normal_distribution<float> wdist(0.0f, 0.02f);  // typical weight std
  std::normal_distribution<float> xdist(0.0f, 1.0f);

  // Host weights (fp32), activation (bf16-rounded).
  std::vector<float> Wf((size_t)N * K);
  for (auto& v : Wf) v = wdist(rng);
  std::vector<uint16_t> xh(K);
  std::vector<float> xf(K);
  for (int k = 0; k < K; ++k) {
    float v = xdist(rng);
    xh[k] = __bfloat16_as_ushort(__float2bfloat16(v));
    xf[k] = Bf16(v);
  }

  // BF16 weights.
  std::vector<uint16_t> Wbf16((size_t)N * K);
  for (size_t i = 0; i < Wf.size(); ++i)
    Wbf16[i] = __bfloat16_as_ushort(__float2bfloat16(Wf[i]));

  // FP8 e4m3 weights, per-output-channel absmax scale (scale = absmax/448).
  std::vector<uint8_t> Wfp8((size_t)N * K);
  std::vector<float> wscale(N);
  for (int n = 0; n < N; ++n) {
    float amax = 0.0f;
    for (int k = 0; k < K; ++k) amax = std::fmax(amax, std::fabs(Wf[(size_t)n * K + k]));
    float sc = amax > 0 ? amax / 448.0f : 1.0f;
    wscale[n] = sc;
    for (int k = 0; k < K; ++k) {
      __nv_fp8_e4m3 q(Wf[(size_t)n * K + k] / sc);
      Wfp8[(size_t)n * K + k] = q.__x;
    }
  }

  // FP32 reference (bf16 activation, fp32 weight, fp32 accumulate).
  std::vector<float> yref(N);
  for (int n = 0; n < N; ++n) {
    double acc = 0;
    for (int k = 0; k < K; ++k) acc += (double)xf[k] * Wf[(size_t)n * K + k];
    yref[n] = (float)acc;
  }

  // Device buffers. To measure the DRAM-bound regime that real decode actually
  // is (each weight is read once per step, evicted before the next step reuses
  // it), cycle through a pool of distinct weight copies whose working set
  // exceeds the 32 MB L2 — otherwise a small weight stays L2-resident across
  // the 200 timing iters and the bench measures L2, not DRAM, bandwidth.
  const size_t kL2Defeat = 96ull << 20;  // 3x L2
  const size_t wbf_bytes = (size_t)N * K * 2;
  const size_t wfp_bytes = (size_t)N * K;
  const int nbuf_bf = (int)std::max<size_t>(1, (kL2Defeat + wbf_bytes - 1) / wbf_bytes);
  const int nbuf_fp = (int)std::max<size_t>(1, (kL2Defeat + wfp_bytes - 1) / wfp_bytes);

  uint16_t *d_x, *d_ybf, *d_yfp;
  float* d_wscale;
  CK(cudaMalloc(&d_x, K * 2));
  CK(cudaMalloc(&d_ybf, N * 2));
  CK(cudaMalloc(&d_yfp, N * 2));
  CK(cudaMalloc(&d_wscale, N * 4));
  CK(cudaMemcpy(d_x, xh.data(), K * 2, cudaMemcpyHostToDevice));
  CK(cudaMemcpy(d_wscale, wscale.data(), N * 4, cudaMemcpyHostToDevice));
  std::vector<uint16_t*> d_Wbf(nbuf_bf);
  std::vector<uint8_t*> d_Wfp8(nbuf_fp);
  for (int i = 0; i < nbuf_bf; ++i) {
    CK(cudaMalloc(&d_Wbf[i], wbf_bytes));
    CK(cudaMemcpy(d_Wbf[i], Wbf16.data(), wbf_bytes, cudaMemcpyHostToDevice));
  }
  for (int i = 0; i < nbuf_fp; ++i) {
    CK(cudaMalloc(&d_Wfp8[i], wfp_bytes));
    CK(cudaMemcpy(d_Wfp8[i], Wfp8.data(), wfp_bytes, cudaMemcpyHostToDevice));
  }

  const int kWarps = kThreads / 32;
  const int blocks = (N + kWarps - 1) / kWarps;
  const size_t smem = (size_t)K * 2;

  auto bench = [&](auto launch) {
    for (int i = 0; i < 10; ++i) launch(i);  // warmup
    CK(cudaDeviceSynchronize());
    cudaEvent_t a, b;
    cudaEventCreate(&a);
    cudaEventCreate(&b);
    const int iters = 200;
    cudaEventRecord(a);
    for (int i = 0; i < iters; ++i) launch(i);
    cudaEventRecord(b);
    CK(cudaEventSynchronize(b));
    float ms = 0;
    cudaEventElapsedTime(&ms, a, b);
    cudaEventDestroy(a);
    cudaEventDestroy(b);
    return ms / iters * 1000.0;  // us/call
  };

  double t_bf = bench([&](int i) {
    Bf16GevKernel<<<blocks, kThreads, smem>>>(d_Wbf[i % nbuf_bf], d_x, d_ybf, N,
                                              K, 1.0f);
  });
  double t_fp = bench([&](int i) {
    Fp8GevKernel<<<blocks, kThreads, smem>>>(d_Wfp8[i % nbuf_fp], d_wscale, d_x,
                                             d_yfp, N, K, 1.0f);
  });

  std::vector<uint16_t> ybf(N), yfp(N);
  CK(cudaMemcpy(ybf.data(), d_ybf, N * 2, cudaMemcpyDeviceToHost));
  CK(cudaMemcpy(yfp.data(), d_yfp, N * 2, cudaMemcpyDeviceToHost));
  std::vector<float> ybff(N), yfpf(N);
  for (int n = 0; n < N; ++n) {
    ybff[n] = __bfloat162float(__ushort_as_bfloat16(ybf[n]));
    yfpf[n] = __bfloat162float(__ushort_as_bfloat16(yfp[n]));
  }

  const double w_bf_gb = (double)N * K * 2 / 1e9;
  const double w_fp_gb = (double)N * K * 1 / 1e9;
  std::printf(
      "%-14s N=%-6d K=%-5d | BF16 %6.1fus %6.1f GB/s | FP8 %6.1fus %6.1f GB/s |"
      " %.2fx | l2rel bf16=%.2e fp8=%.2e fp8vsbf16=%.2e\n",
      s.name, N, K, t_bf, w_bf_gb / (t_bf / 1e6), t_fp, w_fp_gb / (t_fp / 1e6),
      t_bf / t_fp, L2Rel(ybff, yref), L2Rel(yfpf, yref), L2Rel(yfpf, ybff));

  cudaFree(d_x);
  cudaFree(d_ybf);
  cudaFree(d_yfp);
  for (auto p : d_Wbf) cudaFree(p);
  for (auto p : d_Wfp8) cudaFree(p);
  cudaFree(d_wscale);
}

}  // namespace

int main() {
  std::printf("W8A16 FP8 GEMV spike (Thor SM110a) — decode projection lever\n");
  std::printf("weight N(0,0.02), act bf16 N(0,1), per-channel e4m3 scale\n\n");
  // Representative qwen4_exp decode projection shapes (K = hidden 2560).
  const Shape shapes[] = {
      {"qkv/o_proj", 2560, 2560},   {"gate_up-ish", 10240, 2560},
      {"down-ish", 2560, 10240},    {"small_proj", 320, 2560},
      {"lm_head", 151936, 2560},
  };
  for (const auto& s : shapes) RunShape(s);
  return 0;
}
