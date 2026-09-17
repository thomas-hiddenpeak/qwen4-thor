// Unit tests for the FP8 W8A16 decode path (gemv.cu / linear.h ProjGemm):
//   - QuantizeFp8RowKernel (via QuantizeToFp8Shadow): weight -> e4m3 + row scale
//   - Fp8GevKernel        (via Fp8Gev):               M=1 FP8 GEMV
//   - ProjGemm dispatch:  M=1 + shadow -> FP8, else BF16
//
// These EXERCISE the production FP8 kernels directly through the ungated
// QuantizeToFp8Shadow, so coverage does not depend on the Q4T_FP8_PROJ env
// gate. Synthetic weights (no model load); deterministic. Skipped when no CUDA.
#include "q4t/model/gemv.h"
#include "q4t/model/linear.h"
#include "q4t/test.h"

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

using q4t::model::Bf16Gemm;
using q4t::model::Fp8Gev;
using q4t::model::Fp8Shadow;
using q4t::model::ProjGemm;
using q4t::model::QuantizeToFp8Shadow;

bool CudaAvailable() {
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

inline uint16_t F2B(float f) {
  return __bfloat16_as_ushort(__float2bfloat16(f));
}
inline float B2F(uint16_t h) {
  return __bfloat162float(__ushort_as_bfloat16(h));
}

double L2Rel(const std::vector<float>& a, const std::vector<float>& ref) {
  double num = 0, den = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    const double d = a[i] - ref[i];
    num += d * d;
    den += static_cast<double>(ref[i]) * ref[i];
  }
  return std::sqrt(num / (den + 1e-30));
}

// FP32 reference GEMV over bf16-rounded inputs: y[n] = sum_k x[k] * w[n, k].
std::vector<float> RefGemv(const std::vector<uint16_t>& x,
                           const std::vector<uint16_t>& w, int N, int K) {
  std::vector<float> y(N, 0.0f);
  for (int n = 0; n < N; ++n) {
    double acc = 0;
    for (int k = 0; k < K; ++k)
      acc += static_cast<double>(B2F(x[k])) *
             B2F(w[static_cast<size_t>(n) * K + k]);
    y[n] = static_cast<float>(acc);
  }
  return y;
}

int ArgmaxBf16(const std::vector<uint16_t>& y) {
  int best = 0;
  float best_v = -1e30f;
  for (int i = 0; i < static_cast<int>(y.size()); ++i) {
    const float v = B2F(y[i]);
    if (v > best_v) {
      best_v = v;
      best = i;
    }
  }
  return best;
}

}  // namespace

// 1. Quantizer: e4m3(w / scale) * scale reconstructs w within e4m3 granularity,
//    and the shadow is actually allocated (not the gated no-op).
Q4T_TEST(fp8_shadow_quantize_roundtrip) {
  if (!CudaAvailable()) {
    std::printf("  (no CUDA, skipped)\n");
    return true;
  }
  const int N = 40, K = 128;
  std::mt19937 rng(7);
  std::normal_distribution<float> dist(0.0f, 0.02f);
  std::vector<uint16_t> w(static_cast<size_t>(N) * K);
  for (auto& v : w) v = F2B(dist(rng));

  uint16_t* d_w = nullptr;
  Q4T_CHECK(cudaMalloc(&d_w, w.size() * 2) == cudaSuccess);
  cudaMemcpy(d_w, w.data(), w.size() * 2, cudaMemcpyHostToDevice);

  Fp8Shadow sh;
  Q4T_CHECK(QuantizeToFp8Shadow(d_w, N, K, &sh, nullptr));
  Q4T_CHECK(sh.w != nullptr && sh.scale != nullptr);
  cudaDeviceSynchronize();

  std::vector<uint8_t> fp8(w.size());
  std::vector<float> scale(N);
  cudaMemcpy(fp8.data(), sh.w, fp8.size(), cudaMemcpyDeviceToHost);
  cudaMemcpy(scale.data(), sh.scale, N * sizeof(float), cudaMemcpyDeviceToHost);

  std::vector<float> recon(w.size()), orig(w.size());
  for (int n = 0; n < N; ++n) {
    for (int k = 0; k < K; ++k) {
      const size_t i = static_cast<size_t>(n) * K + k;
      __nv_fp8_e4m3 q;
      q.__x = fp8[i];
      recon[i] = static_cast<float>(q) * scale[n];
      orig[i] = B2F(w[i]);
    }
  }
  const double rel = L2Rel(recon, orig);
  std::printf("  quant roundtrip l2rel = %.3e\n", rel);
  Q4T_CHECK(rel < 0.05);  // aggregate e4m3 (3 mantissa bit) quant error
  sh.Free();
  cudaFree(d_w);
  return true;
}

// 2. Fp8Gev (the production M=1 decode kernel) runs, matches the FP32 reference
//    within the FP8 W8A16 noise band, and is measurably noisier than the BF16
//    GEMV (so the FP8 path is genuinely taken, not silently BF16).
Q4T_TEST(fp8_gev_matches_reference_in_noise_band) {
  if (!CudaAvailable()) {
    std::printf("  (no CUDA, skipped)\n");
    return true;
  }
  const int N = 512, K = 256;
  std::mt19937 rng(11);
  std::normal_distribution<float> wd(0.0f, 0.02f), xd(0.0f, 1.0f);
  std::vector<uint16_t> w(static_cast<size_t>(N) * K), x(K);
  for (auto& v : w) v = F2B(wd(rng));
  for (auto& v : x) v = F2B(xd(rng));

  uint16_t *d_w = nullptr, *d_x = nullptr, *d_yfp = nullptr, *d_ybf = nullptr;
  cudaMalloc(&d_w, w.size() * 2);
  cudaMalloc(&d_x, K * 2);
  cudaMalloc(&d_yfp, N * 2);
  cudaMalloc(&d_ybf, N * 2);
  cudaMemcpy(d_w, w.data(), w.size() * 2, cudaMemcpyHostToDevice);
  cudaMemcpy(d_x, x.data(), K * 2, cudaMemcpyHostToDevice);
  void* ws = nullptr;
  cudaMalloc(&ws, 32u << 20);

  Fp8Shadow sh;
  Q4T_CHECK(QuantizeToFp8Shadow(d_w, N, K, &sh, nullptr));
  Q4T_CHECK(Fp8Gev(d_x, sh, d_yfp, N, K, 1.0f, nullptr));
  const auto r = Bf16Gemm(d_x, d_w, d_ybf, 1, N, K, 1.0f, 0.0f, ws, 32u << 20,
                          nullptr);
  Q4T_CHECK(r.has_algo);
  cudaDeviceSynchronize();

  std::vector<uint16_t> yfp(N), ybf(N);
  cudaMemcpy(yfp.data(), d_yfp, N * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(ybf.data(), d_ybf, N * 2, cudaMemcpyDeviceToHost);
  std::vector<float> yfpf(N), ybff(N);
  for (int n = 0; n < N; ++n) {
    yfpf[n] = B2F(yfp[n]);
    ybff[n] = B2F(ybf[n]);
  }

  const std::vector<float> ref = RefGemv(x, w, N, K);
  const double rel_fp8 = L2Rel(yfpf, ref);
  const double rel_bf16 = L2Rel(ybff, ref);
  std::printf("  Fp8Gev l2rel=%.3e  Bf16 l2rel=%.3e\n", rel_fp8, rel_bf16);

  double sumsq = 0;
  for (float v : yfpf) sumsq += static_cast<double>(v) * v;
  Q4T_CHECK(sumsq > 0.0);          // Fp8Gev actually produced output
  Q4T_CHECK(rel_fp8 < 0.06);       // within the FP8 W8A16 noise band
  Q4T_CHECK(rel_fp8 > rel_bf16);   // genuinely FP8 (noisier than BF16)

  sh.Free();
  cudaFree(d_w);
  cudaFree(d_x);
  cudaFree(d_yfp);
  cudaFree(d_ybf);
  cudaFree(ws);
  return true;
}

// 3. ProjGemm dispatch (the exact selection the layer forwards rely on):
//    M=1 + shadow -> FP8 (== Fp8Gev); M=1 + null -> BF16 (== Bf16Gemm);
//    M=2 + shadow -> BF16 (FP8 is decode/M=1 only).
Q4T_TEST(proj_gemm_dispatches_fp8_only_at_m1_with_shadow) {
  if (!CudaAvailable()) {
    std::printf("  (no CUDA, skipped)\n");
    return true;
  }
  const int N = 320, K = 128;
  std::mt19937 rng(23);
  std::normal_distribution<float> wd(0.0f, 0.02f), xd(0.0f, 1.0f);
  std::vector<uint16_t> w(static_cast<size_t>(N) * K), x2(static_cast<size_t>(2) * K);
  for (auto& v : w) v = F2B(wd(rng));
  for (auto& v : x2) v = F2B(xd(rng));

  uint16_t *d_w = nullptr, *d_x = nullptr, *d_fp8 = nullptr, *d_proj = nullptr,
           *d_bf = nullptr, *d_proj2 = nullptr, *d_bf2 = nullptr;
  cudaMalloc(&d_w, w.size() * 2);
  cudaMalloc(&d_x, x2.size() * 2);
  cudaMalloc(&d_fp8, N * 2);
  cudaMalloc(&d_proj, N * 2);
  cudaMalloc(&d_bf, N * 2);
  cudaMalloc(&d_proj2, 2 * N * 2);
  cudaMalloc(&d_bf2, 2 * N * 2);
  cudaMemcpy(d_w, w.data(), w.size() * 2, cudaMemcpyHostToDevice);
  cudaMemcpy(d_x, x2.data(), x2.size() * 2, cudaMemcpyHostToDevice);
  void* ws = nullptr;
  cudaMalloc(&ws, 32u << 20);

  Fp8Shadow sh;
  Q4T_CHECK(QuantizeToFp8Shadow(d_w, N, K, &sh, nullptr));
  Q4T_CHECK(Fp8Gev(d_x, sh, d_fp8, N, K, 1.0f, nullptr));  // reference FP8
  const auto rb =
      Bf16Gemm(d_x, d_w, d_bf, 1, N, K, 1.0f, 0.0f, ws, 32u << 20, nullptr);
  Q4T_CHECK(rb.has_algo);

  // M=1 + shadow -> FP8 path (bit-equal to Fp8Gev).
  const auto r1 =
      ProjGemm(d_x, d_w, &sh, d_proj, 1, N, K, 1.0f, 0.0f, ws, 32u << 20, nullptr);
  Q4T_CHECK(r1.has_algo);
  // M=1 + null shadow -> BF16 path (bit-equal to Bf16Gemm).
  const auto r2 = ProjGemm(d_x, d_w, nullptr, d_proj, 1, N, K, 1.0f, 0.0f, ws,
                           32u << 20, nullptr);  // note: reuses d_proj below
  Q4T_CHECK(r2.has_algo);
  cudaDeviceSynchronize();

  std::vector<uint16_t> fp8(N), bf(N), proj_fp8(N), proj_bf(N);
  cudaMemcpy(fp8.data(), d_fp8, N * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(bf.data(), d_bf, N * 2, cudaMemcpyDeviceToHost);
  // Re-run to capture each path's output separately (d_proj was overwritten).
  ProjGemm(d_x, d_w, &sh, d_proj, 1, N, K, 1.0f, 0.0f, ws, 32u << 20, nullptr);
  cudaDeviceSynchronize();
  cudaMemcpy(proj_fp8.data(), d_proj, N * 2, cudaMemcpyDeviceToHost);
  ProjGemm(d_x, d_w, nullptr, d_proj, 1, N, K, 1.0f, 0.0f, ws, 32u << 20, nullptr);
  cudaDeviceSynchronize();
  cudaMemcpy(proj_bf.data(), d_proj, N * 2, cudaMemcpyDeviceToHost);

  Q4T_CHECK(std::memcmp(proj_fp8.data(), fp8.data(), N * 2) == 0);  // FP8 taken
  Q4T_CHECK(std::memcmp(proj_bf.data(), bf.data(), N * 2) == 0);    // BF16 taken
  Q4T_CHECK(std::memcmp(fp8.data(), bf.data(), N * 2) != 0);  // paths differ

  // M=2 + shadow -> BF16 (FP8 is M=1 only): equals Bf16Gemm(M=2).
  const auto r3 = ProjGemm(d_x, d_w, &sh, d_proj2, 2, N, K, 1.0f, 0.0f, ws,
                           32u << 20, nullptr);
  const auto r4 =
      Bf16Gemm(d_x, d_w, d_bf2, 2, N, K, 1.0f, 0.0f, ws, 32u << 20, nullptr);
  Q4T_CHECK(r3.has_algo && r4.has_algo);
  cudaDeviceSynchronize();
  std::vector<uint16_t> p2(2 * N), b2(2 * N);
  cudaMemcpy(p2.data(), d_proj2, 2 * N * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(b2.data(), d_bf2, 2 * N * 2, cudaMemcpyDeviceToHost);
  Q4T_CHECK(std::memcmp(p2.data(), b2.data(), 2 * N * 2) == 0);  // no FP8 at M=2

  sh.Free();
  cudaFree(d_w);
  cudaFree(d_x);
  cudaFree(d_fp8);
  cudaFree(d_proj);
  cudaFree(d_bf);
  cudaFree(d_proj2);
  cudaFree(d_bf2);
  cudaFree(ws);
  return true;
}

// 4. Head diff (the auditor's ask at unit scale): an lm_head-shaped GEMV over a
//    frozen hidden. A clearly-separated argmax is preserved under FP8.
Q4T_TEST(fp8_head_argmax_preserved_for_confident_token) {
  if (!CudaAvailable()) {
    std::printf("  (no CUDA, skipped)\n");
    return true;
  }
  const int N = 2048, K = 256;  // vocab-ish x hidden
  std::mt19937 rng(31);
  std::normal_distribution<float> wd(0.0f, 0.02f), xd(0.0f, 1.0f);
  std::vector<uint16_t> w(static_cast<size_t>(N) * K), x(K);
  for (auto& v : w) v = F2B(wd(rng));
  for (auto& v : x) v = F2B(xd(rng));
  // Make one row clearly dominant (aligned with x) so the argmax is confident.
  const int winner = 1234;
  for (int k = 0; k < K; ++k)
    w[static_cast<size_t>(winner) * K + k] = F2B(0.5f * B2F(x[k]));

  uint16_t *d_w = nullptr, *d_x = nullptr, *d_fp = nullptr, *d_bf = nullptr;
  cudaMalloc(&d_w, w.size() * 2);
  cudaMalloc(&d_x, K * 2);
  cudaMalloc(&d_fp, N * 2);
  cudaMalloc(&d_bf, N * 2);
  cudaMemcpy(d_w, w.data(), w.size() * 2, cudaMemcpyHostToDevice);
  cudaMemcpy(d_x, x.data(), K * 2, cudaMemcpyHostToDevice);
  void* ws = nullptr;
  cudaMalloc(&ws, 32u << 20);

  Fp8Shadow sh;
  Q4T_CHECK(QuantizeToFp8Shadow(d_w, N, K, &sh, nullptr));
  Q4T_CHECK(Fp8Gev(d_x, sh, d_fp, N, K, 1.0f, nullptr));
  const auto r = Bf16Gemm(d_x, d_w, d_bf, 1, N, K, 1.0f, 0.0f, ws, 32u << 20,
                          nullptr);
  Q4T_CHECK(r.has_algo);
  cudaDeviceSynchronize();

  std::vector<uint16_t> yfp(N), ybf(N);
  cudaMemcpy(yfp.data(), d_fp, N * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(ybf.data(), d_bf, N * 2, cudaMemcpyDeviceToHost);
  const int amax_fp = ArgmaxBf16(yfp);
  const int amax_bf = ArgmaxBf16(ybf);
  std::printf("  argmax bf16=%d fp8=%d (winner=%d)\n", amax_bf, amax_fp, winner);
  Q4T_CHECK(amax_bf == winner);   // BF16 picks the confident token
  Q4T_CHECK(amax_fp == winner);   // FP8 preserves it

  sh.Free();
  cudaFree(d_w);
  cudaFree(d_x);
  cudaFree(d_fp);
  cudaFree(d_bf);
  cudaFree(ws);
  return true;
}
