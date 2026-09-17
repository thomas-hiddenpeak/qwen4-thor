// Grouped FP4 MoE GEMM benefit probe (does the lever have expected upside?).
//
// The MoE routed experts run as E=512 separate cuBLASLt FP4 GEMMs per layer
// (gate/up then down). At decode the per-expert M_e is tiny (B=1 -> 10 experts
// x M_e=1; B=128 -> ~470 experts x M_e~2.7), so each GEMM is a sliver of a
// 128-row tensor-core tile and the launches don't overlap enough to stream the
// weights at peak DRAM bandwidth. A monolithic/grouped FP4 GEMM (CUTLASS group
// gemm) would process all experts in one persistent kernel and stream the
// weights continuously.
//
// This probe measures whether that upside is real WITHOUT building CUTLASS:
//   - per-expert cuBLASLt path, 1 stream (legacy) and 4 streams (production)
//   - the weight-streaming FLOOR: read every expert's packed FP4 + SF bytes
//     once at peak DRAM bandwidth (a grouped GEMM cannot beat this — it must
//     read each expert's weight once).
// headroom = per-expert(4-stream) time / floor time = the MAX speedup a
// grouped GEMM could deliver on the MoE GEMM portion. Large -> the lever has
// real upside; ~1x -> the per-expert path is already near the floor (no point).
//
// Real qwen4_exp dims: hidden 2560, moe_is 640, E 512, top_k 10.
// gate/up W = [1280, 2560] FP4, down W = [2560, 640] FP4. ~2.5 MB/expert.
// Build: q4t_moe_grouped_bench. Run: ./build/q4t_moe_grouped_bench
#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <vector>

#include "q4t/quant/fp4_gemm.h"
#include "q4t/quant/swizzle.h"

using q4t::quant::Fp4Gemm;
using q4t::quant::SfBufferSize;

namespace {

#define CK(x)                                                          \
  do {                                                                 \
    cudaError_t e = (x);                                               \
    if (e != cudaSuccess) {                                            \
      std::printf("CUDA %s:%d %s\n", __FILE__, __LINE__,               \
                  cudaGetErrorString(e));                              \
      std::exit(1);                                                    \
    }                                                                  \
  } while (0)

// Grid-stride read of a byte buffer (as uint4) with a XOR reduction to a sink
// so the compiler cannot elide the loads. Reads at peak DRAM bandwidth.
__global__ void StreamKernel(const uint4* __restrict__ p, size_t n4,
                             uint4* __restrict__ sink) {
  uint4 acc = {0, 0, 0, 0};
  for (size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x; i < n4;
       i += (size_t)gridDim.x * blockDim.x) {
    uint4 v = p[i];
    acc.x ^= v.x;
    acc.y ^= v.y;
    acc.z ^= v.z;
    acc.w ^= v.w;
  }
  if (threadIdx.x == 0) sink[blockIdx.x % 1024] = acc;
}

double EventMs(cudaEvent_t a, cudaEvent_t b) {
  float ms = 0;
  cudaEventElapsedTime(&ms, a, b);
  return ms;
}

}  // namespace

int main() {
  const int hs = 2560, moe_is = 640, E = 512;
  const int Ngu = 2 * moe_is, Kgu = hs;   // gate/up: [1280, 2560]
  const int Ndn = hs, Kdn = moe_is;       // down:    [2560, 640]
  const size_t gu_pk = (size_t)Ngu * Kgu / 2;      // packed bytes / expert
  const size_t gu_sf = SfBufferSize(Ngu, Kgu);
  const size_t dn_pk = (size_t)Ndn * Kdn / 2;
  const size_t dn_sf = SfBufferSize(Ndn, Kdn);
  const size_t per_expert = gu_pk + gu_sf + dn_pk + dn_sf;
  const size_t total_w = (size_t)E * per_expert;
  std::printf("Grouped FP4 MoE GEMM benefit probe (Thor SM110a)\n");
  std::printf("E=%d hs=%d moe_is=%d | per-expert weight %.2f MB | all-E %.2f GB\n",
              E, hs, moe_is, per_expert / 1e6, total_w / 1e9);
  std::printf("gu[%d,%d] dn[%d,%d] | peak DRAM ~241 GB/s\n\n", Ngu, Kgu, Ndn,
              Kdn);

  // Per-expert weight pools (contiguous; carve per-expert slices). Values are
  // irrelevant for bandwidth, so memset (fast, no host init).
  uint8_t *d_gu_pk, *d_gu_sf, *d_dn_pk, *d_dn_sf;
  CK(cudaMalloc(&d_gu_pk, (size_t)E * gu_pk));
  CK(cudaMalloc(&d_gu_sf, (size_t)E * gu_sf));
  CK(cudaMalloc(&d_dn_pk, (size_t)E * dn_pk));
  CK(cudaMalloc(&d_dn_sf, (size_t)E * dn_sf));
  CK(cudaMemset(d_gu_pk, 0x11, (size_t)E * gu_pk));
  CK(cudaMemset(d_gu_sf, 0x38, (size_t)E * gu_sf));  // e4m3 ~0.75
  CK(cudaMemset(d_dn_pk, 0x11, (size_t)E * dn_pk));
  CK(cudaMemset(d_dn_sf, 0x38, (size_t)E * dn_sf));
  uint4* d_sink;
  CK(cudaMalloc(&d_sink, 1024 * sizeof(uint4)));

  const int kStreams = 4;
  cudaStream_t st[kStreams];
  for (int i = 0; i < kStreams; ++i) CK(cudaStreamCreate(&st[i]));
  const size_t ws_bytes = 32u << 20;
  void* d_ws[kStreams];
  for (int i = 0; i < kStreams; ++i) CK(cudaMalloc(&d_ws[i], ws_bytes));

  std::printf("%-5s | per-expert 1-str      | per-expert 4-str      | "
              "stream floor        | headroom(4str/floor)\n", "M_e");

  const int Mes[] = {1, 2, 4, 8, 16, 32, 64};
  for (int M_e : Mes) {
    // Shared per-M_e activation + output buffers (one set per stream). The
    // weights are the DRAM-bound term; sharing activations is fine.
    const size_t a_gu_pk = (size_t)M_e * Kgu / 2, a_gu_sf = SfBufferSize(M_e, Kgu);
    const size_t a_dn_pk = (size_t)M_e * Kdn / 2, a_dn_sf = SfBufferSize(M_e, Kdn);
    uint8_t *agu_pk[kStreams], *agu_sf[kStreams], *adn_pk[kStreams], *adn_sf[kStreams];
    uint16_t *guo[kStreams], *dno[kStreams];
    for (int i = 0; i < kStreams; ++i) {
      CK(cudaMalloc(&agu_pk[i], a_gu_pk));
      CK(cudaMalloc(&agu_sf[i], a_gu_sf));
      CK(cudaMalloc(&adn_pk[i], a_dn_pk));
      CK(cudaMalloc(&adn_sf[i], a_dn_sf));
      CK(cudaMalloc(&guo[i], (size_t)M_e * Ngu * 2));
      CK(cudaMalloc(&dno[i], (size_t)M_e * Ndn * 2));
      CK(cudaMemset(agu_pk[i], 0x11, a_gu_pk));
      CK(cudaMemset(agu_sf[i], 0x38, a_gu_sf));
      CK(cudaMemset(adn_pk[i], 0x11, a_dn_pk));
      CK(cudaMemset(adn_sf[i], 0x38, a_dn_sf));
    }

    auto run_expert = [&](int e, int s) {
      Fp4Gemm(d_gu_pk + (size_t)e * gu_pk, d_gu_sf + (size_t)e * gu_sf,
              agu_pk[s], agu_sf[s], guo[s], M_e, Ngu, Kgu, 1.0f, 1.0f, d_ws[s],
              ws_bytes, st[s]);
      Fp4Gemm(d_dn_pk + (size_t)e * dn_pk, d_dn_sf + (size_t)e * dn_sf,
              adn_pk[s], adn_sf[s], dno[s], M_e, Ndn, Kdn, 1.0f, 1.0f, d_ws[s],
              ws_bytes, st[s]);
    };

    // Warm the plan cache (both shapes) + streams.
    for (int i = 0; i < kStreams; ++i) run_expert(0, i);
    CK(cudaDeviceSynchronize());

    cudaEvent_t a, b;
    CK(cudaEventCreate(&a));
    CK(cudaEventCreate(&b));
    const int iters = 20;

    // 1-stream per-expert.
    CK(cudaEventRecord(a, st[0]));
    for (int it = 0; it < iters; ++it)
      for (int e = 0; e < E; ++e) run_expert(e, 0);
    CK(cudaEventRecord(b, st[0]));
    CK(cudaEventSynchronize(b));
    double t1 = EventMs(a, b) / iters;

    // 4-stream per-expert (round-robin, production path).
    CK(cudaDeviceSynchronize());
    CK(cudaEventRecord(a, st[0]));
    for (int it = 0; it < iters; ++it)
      for (int e = 0; e < E; ++e) run_expert(e, e % kStreams);
    for (int i = 0; i < kStreams; ++i) CK(cudaStreamSynchronize(st[i]));
    CK(cudaEventRecord(b, st[0]));
    CK(cudaEventSynchronize(b));
    double t4 = EventMs(a, b) / iters;

    // Weight-streaming floor: read every expert's gu/dn packed + SF once.
    auto stream_all = [&]() {
      StreamKernel<<<1024, 256, 0, st[0]>>>(
          reinterpret_cast<uint4*>(d_gu_pk), (size_t)E * gu_pk / 16, d_sink);
      StreamKernel<<<1024, 256, 0, st[0]>>>(
          reinterpret_cast<uint4*>(d_gu_sf), (size_t)E * gu_sf / 16, d_sink);
      StreamKernel<<<1024, 256, 0, st[0]>>>(
          reinterpret_cast<uint4*>(d_dn_pk), (size_t)E * dn_pk / 16, d_sink);
      StreamKernel<<<1024, 256, 0, st[0]>>>(
          reinterpret_cast<uint4*>(d_dn_sf), (size_t)E * dn_sf / 16, d_sink);
    };
    stream_all();
    CK(cudaDeviceSynchronize());
    CK(cudaEventRecord(a, st[0]));
    for (int it = 0; it < iters; ++it) stream_all();
    CK(cudaEventRecord(b, st[0]));
    CK(cudaEventSynchronize(b));
    double tf = EventMs(a, b) / iters;

    const double gb = total_w / 1e9;
    std::printf(
        "%-5d | %7.2fms %6.1fGB/s | %7.2fms %6.1fGB/s | %6.2fms %6.1fGB/s | "
        "%.2fx\n",
        M_e, t1, gb / (t1 / 1e3), t4, gb / (t4 / 1e3), tf, gb / (tf / 1e3),
        t4 / tf);

    cudaEventDestroy(a);
    cudaEventDestroy(b);
    for (int i = 0; i < kStreams; ++i) {
      cudaFree(agu_pk[i]);
      cudaFree(agu_sf[i]);
      cudaFree(adn_pk[i]);
      cudaFree(adn_sf[i]);
      cudaFree(guo[i]);
      cudaFree(dno[i]);
    }
  }
  std::printf("\nheadroom = 4-stream per-expert / streaming floor = max grouped "
              "GEMM speedup on the MoE GEMM portion.\n");
  return 0;
}
