// Focused operator comparison (O0/O1): T=1 (M=1) vs T=k+1 (M=k+1) GEMV/GEMM.
//
// Fixes IDENTICAL operator inputs and reports, for the first GEMV/GEMM (and
// the HC block-injection shape) operator, where the two paths first differ and
// whether the difference is accumulation-order or final-rounding. This is a
// diagnostic: it prints the evidence and returns true; it does not assert a
// contract. See docs/STATUS.md "当前阶段" (算子级对照) and
// .q4t-work/mtp-correctness-contract.md (O0–O2).
//
// Three paths compared for the same row-0 input x:
//   gemv  : Bf16Gev(x, W)                 — M=1 SIMT GEMV (T=1 decode path)
//   gemm1 : Bf16Gemm(x, W, M=1)           — NOTE: Bf16Gemm(M=1, beta=0)
//                                            dispatches to Bf16Gev by
//                                            default (linear.h), so gemm1
//                                            exercises the SAME GEMV kernel;
//                                            it is NOT an independent
//                                            cuBLASLt M=1 algorithm
//   gemm4 : Bf16Gemm([x;0;0;0], W, M=4)   — M=4 cuBLASLt row 0 (T=4 verify)
// gemv-vs-gemm4 is the actual T=1-vs-T=4 cross-algorithm divergence; the
// gemv-vs-gemm1 and gemm1-vs-gemm4 splits document the dispatch behavior.
// An FP64 reference shows whether both are valid roundings of slightly
// different FP32 sums (accumulation order) or one is systematically off.
#include "q4t/model/hyperconnection.h"
#include "q4t/model/linear.h"
#include "q4t/status.h"
#include "q4t/test.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

namespace {

using q4t::model::Bf16Gemm;
using q4t::model::Bf16Gev;
using q4t::model::Bf16GevFp32;
using q4t::model::Bf16SmallMShapeRouted;
using q4t::model::HyperConnectionMix;
using q4t::Status;

bool CudaAvailable() {
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

inline uint16_t F2B(float f) {
  return __bfloat16_as_ushort(__float2bfloat16(f));
}
inline double B2D(uint16_t h) {
  return static_cast<double>(__bfloat162float(__ushort_as_bfloat16(h)));
}

// FP64 reference: y[n] = sum_k x[k] * w[n, k].
std::vector<double> RefGemv(const std::vector<uint16_t>& x,
                            const std::vector<uint16_t>& w, int N, int K) {
  std::vector<double> y(N, 0.0);
  for (int n = 0; n < N; ++n) {
    double s = 0.0;
    for (int k = 0; k < K; ++k)
      s += B2D(x[static_cast<size_t>(k)]) *
           B2D(w[static_cast<size_t>(n) * K + k]);
    y[n] = s;
  }
  return y;
}

struct DiffStats {
  int first = -1;
  int count = 0;
  double max_abs = 0.0;
  double l2rel = 0.0;
};

// Element-wise compare two equal-length BF16 vectors.
DiffStats CompareBf16(const std::vector<uint16_t>& a,
                      const std::vector<uint16_t>& b) {
  DiffStats s;
  double num = 0, den = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    if (a[i] != b[i]) {
      if (s.first < 0) s.first = static_cast<int>(i);
      s.count++;
      s.max_abs = std::max(s.max_abs, std::fabs(B2D(a[i]) - B2D(b[i])));
    }
    const double av = B2D(a[i]), bv = B2D(b[i]);
    num += (av - bv) * (av - bv);
    den += bv * bv;
  }
  s.l2rel = std::sqrt(num / (den + 1e-30));
  return s;
}

double L2RelToRef(const std::vector<uint16_t>& v,
                  const std::vector<double>& ref) {
  double num = 0, den = 0;
  for (size_t i = 0; i < v.size(); ++i) {
    const double d = B2D(v[i]) - ref[i];
    num += d * d;
    den += ref[i] * ref[i];
  }
  return std::sqrt(num / (den + 1e-30));
}

void Report(const char* label, const DiffStats& s, size_t n) {
  std::printf("  %-22s first_diff=%-6d count=%-6d/%zu max_abs=%.3e l2rel=%.3e\n",
              label, s.first, s.count, n, s.max_abs, s.l2rel);
}

// Run the three-path comparison for a given (N, K) and deterministic seed.
bool RunCase(int N, int K, const std::vector<unsigned>& seeds, const char* tag) {
  uint16_t *d_x = nullptr, *d_w = nullptr, *d_y = nullptr, *d_x4 = nullptr,
           *d_y4 = nullptr;
  void* ws = nullptr;
  cudaMalloc(&d_w, static_cast<size_t>(N) * K * 2);
  cudaMalloc(&d_x, K * 2);
  cudaMalloc(&d_y, N * 2);
  cudaMalloc(&d_x4, static_cast<size_t>(4) * K * 2);
  cudaMalloc(&d_y4, static_cast<size_t>(4) * N * 2);
  cudaMalloc(&ws, 32u << 20);

  int worst_count = 0;
  double worst_max = 0.0, worst_l2 = 0.0;
  int worst_first = -1;
  for (unsigned seed : seeds) {
    std::mt19937 rng(seed);
    std::normal_distribution<double> dist(0.0, 0.1);
    std::vector<uint16_t> x(K), w(static_cast<size_t>(N) * K);
    for (auto& v : x) v = F2B(static_cast<float>(dist(rng)));
    for (auto& v : w) v = F2B(static_cast<float>(dist(rng)));
    cudaMemcpy(d_w, w.data(), w.size() * 2, cudaMemcpyHostToDevice);
    cudaMemcpy(d_x, x.data(), K * 2, cudaMemcpyHostToDevice);
    // x4 = [x; 0; 0; 0]: row 0 identical to the M=1 input, rows 1-3 zero.
    std::vector<uint16_t> x4(static_cast<size_t>(4) * K, 0);
    std::memcpy(x4.data(), x.data(), K * 2);
    cudaMemcpy(d_x4, x4.data(), x4.size() * 2, cudaMemcpyHostToDevice);

    // gemv: M=1 SIMT GEMV (T=1 decode path).
    Q4T_CHECK(Bf16Gev(d_x, d_w, d_y, N, K, 1.0f, nullptr));
    std::vector<uint16_t> y_gemv(N);
    cudaMemcpy(y_gemv.data(), d_y, N * 2, cudaMemcpyDeviceToHost);
    // gemm1: M=1 cuBLASLt (same M, GEMM kernel family).
    cudaMemset(d_y, 0, N * 2);
    const auto r1 = Bf16Gemm(d_x, d_w, d_y, 1, N, K, 1.0f, 0.0f, ws, 32u << 20,
                             nullptr);
    Q4T_CHECK(r1.has_algo);
    std::vector<uint16_t> y_gemm1(N);
    cudaMemcpy(y_gemm1.data(), d_y, N * 2, cudaMemcpyDeviceToHost);
    // gemm4: M=4 cuBLASLt, row 0 (T=4 verify path).
    const auto r4 = Bf16Gemm(d_x4, d_w, d_y4, 4, N, K, 1.0f, 0.0f, ws, 32u << 20,
                             nullptr);
    Q4T_CHECK(r4.has_algo);
    std::vector<uint16_t> y_gemm4(N);
    cudaMemcpy(y_gemm4.data(), d_y4, N * 2, cudaMemcpyDeviceToHost);
    cudaDeviceSynchronize();

    const std::vector<double> ref = RefGemv(x, w, N, K);
    std::printf("[case %s] N=%d K=%d seed=%u\n", tag, N, K, seed);
    const DiffStats g1 = CompareBf16(y_gemv, y_gemm1);
    const DiffStats m14 = CompareBf16(y_gemm1, y_gemm4);
    const DiffStats g4 = CompareBf16(y_gemv, y_gemm4);
    Report("gemv vs gemm1", g1, N);
    Report("gemm1 vs gemm4", m14, N);
    Report("gemv vs gemm4", g4, N);
    const double g_ref = L2RelToRef(y_gemv, ref);
    const double m1_ref = L2RelToRef(y_gemm1, ref);
    const double m4_ref = L2RelToRef(y_gemm4, ref);
    std::printf("  l2rel-to-FP64ref: gemv=%.3e gemm1=%.3e gemm4=%.3e\n", g_ref,
                m1_ref, m4_ref);
    if (g4.first >= 0) {
      std::printf("  first-diff elem %d: gemv=%.6f gemm1=%.6f gemm4=%.6f "
                  "ref=%.6f\n",
                  g4.first, B2D(y_gemv[g4.first]), B2D(y_gemm1[g4.first]),
                  B2D(y_gemm4[g4.first]), ref[g4.first]);
    }
    if (g4.count > worst_count ||
        (g4.count == worst_count && g4.max_abs > worst_max)) {
      worst_count = g4.count;
      worst_max = g4.max_abs;
      worst_l2 = g4.l2rel;
      worst_first = g4.first;
    }
  }  // for seed
  std::printf("  => worst gemv-vs-gemm4 over %zu seeds: count=%d max_abs=%.3e "
              "l2rel=%.3e first_diff=%d\n",
              seeds.size(), worst_count, worst_max, worst_l2, worst_first);

  cudaFree(d_w);
  cudaFree(d_x);
  cudaFree(d_y);
  cudaFree(d_x4);
  cudaFree(d_y4);
  cudaFree(ws);
  return true;
}

}  // namespace

Q4T_TEST(gemm_t1_vs_tk1_general) {
  if (!CudaAvailable())
    Q4T_SKIP("no CUDA");
  // A realistic square projection (hidden 2560).
  return RunCase(2560, 2560, {12345u, 2024u, 777u}, "general-square");
}

Q4T_TEST(gemm_t1_vs_tk1_hc_inject) {
  if (!CudaAvailable())
    Q4T_SKIP("no CUDA");
  // HC block-injection shape (hc_count=4, hidden=2560 -> N=4, K=10240).
  // Bf16Gev routes this to the dedicated HcInjectGev SIMT kernel; the M=4
  // path uses cuBLASLt GEMM. This is the first GEMV/GEMM operator per layer.
  return RunCase(4, 10240, {67890u, 31337u, 5u}, "hc-inject");
}

// ---------------------------------------------------------------------------
// O0/O1 production-operator comparison (HC inject gate).
//
// The synthetic case above used the generic Bf16Gev, which is NOT the
// production T=1 path. The production T=1 HC inject is the fused kernel
// HcInjectGevGated (GEMV + in-register gate); the production T>1 path is
// Bf16Gemm + ApplyInjectGateKernel (exposed as HcInjectGateGeneric). These
// cases call the ACTUAL production operators on identical inputs and compare
// both the pre-gate projection (GEMM accumulation) and the post-gate output.
//
// The "actual" case loads the real S7 Part C a=2 operator inputs captured by
// the Q4T_OP_CAPTURE hook in PrepareInjectGate (see
// tests/model_verify_ckpt_samepath_test.cpp), so the comparison runs on the
// exact failing-decode input rather than synthetic data.
#include <unistd.h>

#include "q4t/model/hyperconnection.h"

namespace {

using q4t::model::HcInjectGev;
using q4t::model::HcInjectGevGated;
using q4t::model::HcInjectGateGeneric;

bool ReadBf16(const std::string& path, std::vector<uint16_t>* out) {
  std::ifstream f(path, std::ios::binary);
  if (!f) return false;
  f.seekg(0, std::ios::end);
  const std::streamoff bytes = f.tellg();
  if (bytes <= 0 || bytes % 2 != 0) return false;
  f.seekg(0, std::ios::beg);
  out->resize(static_cast<size_t>(bytes) / 2);
  f.read(reinterpret_cast<char*>(out->data()),
         static_cast<std::streamsize>(bytes));
  return static_cast<bool>(f);
}

// Run the production-operator three-path comparison for fixed inputs.
// x: [1, dim], w: [hc, dim]. Returns true (diagnostic, no contract assert).
bool RunHcProdCase(const std::vector<uint16_t>& x,
                   const std::vector<uint16_t>& w, int hc, int hs,
                   const char* tag) {
  const int dim = hc * hs;
  uint16_t *d_x = nullptr, *d_w = nullptr, *d_x4 = nullptr, *d_pA = nullptr,
           *d_pB = nullptr, *d_pC = nullptr, *d_yA = nullptr, *d_yB = nullptr,
           *d_yC = nullptr;
  void* ws = nullptr;
  cudaMalloc(&d_w, static_cast<size_t>(hc) * dim * 2);
  cudaMalloc(&d_x, static_cast<size_t>(dim) * 2);
  cudaMalloc(&d_x4, static_cast<size_t>(4) * dim * 2);
  cudaMalloc(&d_pA, hc * 2);
  cudaMalloc(&d_pB, hc * 2);
  cudaMalloc(&d_pC, static_cast<size_t>(4) * hc * 2);
  cudaMalloc(&d_yA, hc * 2);
  cudaMalloc(&d_yB, hc * 2);
  cudaMalloc(&d_yC, static_cast<size_t>(4) * hc * 2);
  cudaMalloc(&ws, 32u << 20);
  bool ok = true;

  cudaMemcpy(d_w, w.data(), w.size() * 2, cudaMemcpyHostToDevice);
  cudaMemcpy(d_x, x.data(), x.size() * 2, cudaMemcpyHostToDevice);
  std::vector<uint16_t> x4(static_cast<size_t>(4) * dim, 0);
  std::memcpy(x4.data(), x.data(), dim * 2);
  cudaMemcpy(d_x4, x4.data(), x4.size() * 2, cudaMemcpyHostToDevice);

  // Pre-gate projection (the GEMM/GEMV accumulation itself).
  ok &= HcInjectGev(d_x, d_w, d_pA, 1.0f, nullptr);
  const auto r1 = Bf16Gemm(d_x, d_w, d_pB, 1, hc, dim, 1.0f, 0.0f, ws,
                           32u << 20, nullptr);
  ok &= r1.has_algo;
  const auto r4 = Bf16Gemm(d_x4, d_w, d_pC, 4, hc, dim, 1.0f, 0.0f, ws,
                           32u << 20, nullptr);
  ok &= r4.has_algo;
  // Post-gate output (the production operator).
  ok &= HcInjectGevGated(d_x, d_w, d_yA, nullptr);
  ok &= HcInjectGateGeneric(d_x, d_w, d_yB, 1, hc, hs, ws, 32u << 20, nullptr)
            .ok();
  ok &= HcInjectGateGeneric(d_x4, d_w, d_yC, 4, hc, hs, ws, 32u << 20, nullptr)
            .ok();
  cudaDeviceSynchronize();

  std::vector<uint16_t> pA(hc), pB(hc), pC(4 * hc), yA(hc), yB(hc), yC(4 * hc);
  cudaMemcpy(pA.data(), d_pA, hc * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(pB.data(), d_pB, hc * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(pC.data(), d_pC, 4 * hc * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(yA.data(), d_yA, hc * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(yB.data(), d_yB, hc * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(yC.data(), d_yC, 4 * hc * 2, cudaMemcpyDeviceToHost);

  const std::vector<uint16_t> pC0(pC.begin(), pC.begin() + hc);
  const std::vector<uint16_t> yC0(yC.begin(), yC.begin() + hc);
  std::printf("[hc-prod %s] hc=%d hs=%d dim=%d\n", tag, hc, hs, dim);
  const DiffStats pp = CompareBf16(pA, pB);
  const DiffStats pm = CompareBf16(pB, pC0);
  const DiffStats pa = CompareBf16(pA, pC0);
  const DiffStats gg = CompareBf16(yA, yB);
  const DiffStats gm = CompareBf16(yB, yC0);
  const DiffStats g4 = CompareBf16(yA, yC0);
  Report("proj fused vs gemm1", pp, hc);
  Report("proj gemm1 vs gemm4", pm, hc);
  Report("proj fused vs gemm4", pa, hc);
  Report("gate fused vs gemm1", gg, hc);
  Report("gate gemm1 vs gemm4", gm, hc);
  Report("gate fused vs gemm4", g4, hc);
  // FP64 reference of the projection: shows whether both are valid roundings.
  std::vector<double> ref(hc, 0.0);
  for (int b = 0; b < hc; ++b)
    for (int k = 0; k < dim; ++k)
      ref[b] += B2D(x[k]) * B2D(w[static_cast<size_t>(b) * dim + k]);
  std::printf("  proj l2rel-to-FP64ref: fused=%.3e gemm1=%.3e gemm4=%.3e\n",
              L2RelToRef(pA, ref), L2RelToRef(pB, ref), L2RelToRef(pC0, ref));
  if (g4.first >= 0) {
    std::printf("  first gate-diff elem %d: fused=%.6f gemm1=%.6f gemm4=%.6f\n",
                g4.first, B2D(yA[g4.first]), B2D(yB[g4.first]),
                B2D(yC0[g4.first]));
    std::printf("  pre-gate at same elem: fused=%.6f gemm1=%.6f gemm4=%.6f "
                "ref=%.6f\n",
                B2D(pA[g4.first]), B2D(pB[g4.first]), B2D(pC0[g4.first]),
                ref[g4.first]);
  }
  cudaFree(d_w);
  cudaFree(d_x);
  cudaFree(d_x4);
  cudaFree(d_pA);
  cudaFree(d_pB);
  cudaFree(d_pC);
  cudaFree(d_yA);
  cudaFree(d_yB);
  cudaFree(d_yC);
  cudaFree(ws);
  return ok;
}

}  // namespace

Q4T_TEST(hc_inject_prod_synthetic) {
  if (!CudaAvailable())
    Q4T_SKIP("no CUDA");
  // Real HC shape (hc=4, hs=2560, dim=10240), deterministic seeds.
  const int hc = 4, hs = 2560, dim = hc * hs;
  bool ok = true;
  for (unsigned seed : {4242u, 99u, 20261011u}) {
    std::mt19937 rng(seed);
    std::normal_distribution<double> dist(0.0, 0.1);
    std::vector<uint16_t> x(dim), w(static_cast<size_t>(hc) * dim);
    for (auto& v : x) v = F2B(static_cast<float>(dist(rng)));
    for (auto& v : w) v = F2B(static_cast<float>(dist(rng)));
    std::printf("  seed=%u\n", seed);
    ok &= RunHcProdCase(x, w, hc, hs, "synthetic");
  }
  return ok;
}

// Load the real S7 Part C a=2 T=1 captures and run the production-operator
// comparison on the exact failing-decode inputs. Skips when no capture is
// present (the capture is produced by running model_verify_ckpt_samepath with
// Q4T_OP_CAPTURE set; see that test).
Q4T_TEST(hc_inject_actual_s7) {
  if (!CudaAvailable())
    Q4T_SKIP("no CUDA");
  const char* env = std::getenv("Q4T_OP_CAPTURE_DIR");
  const std::string dir = env ? env : "/tmp/q4t_op_capture_s7";
  const std::string meta_path = dir + "/hc_meta.txt";
  if (!std::ifstream(meta_path)) {
    std::printf("  (no S7 capture at %s; run model_verify_ckpt_samepath with "
                "Q4T_OP_CAPTURE first)\n",
                dir.c_str());
    return true;  // diagnostic: absence is not a failure
  }
  // Parse meta for T=1 call idx values.
  std::ifstream meta(meta_path);
  std::vector<int> t1_idx;
  std::string line;
  while (std::getline(meta, line)) {
    int idx = -1, T = -1;
    if (std::sscanf(line.c_str(), "idx=%d T=%d", &idx, &T) == 2 && T == 1)
      t1_idx.push_back(idx);
  }
  if (t1_idx.empty()) {
    std::printf("  (capture present but no T=1 call; nothing to compare)\n");
    return true;
  }
  std::printf("  S7 capture: %zu T=1 call(s) from %s\n", t1_idx.size(),
              dir.c_str());
  const int hc = 4, hs = 2560, dim = hc * hs;
  bool ok = true;
  for (int idx : t1_idx) {
    const std::string base =
        dir + "/hc_" + std::to_string(idx) + "_T1";
    std::vector<uint16_t> x, w;
    if (!ReadBf16(base + "_normed.bin", &x) ||
        !ReadBf16(base + "_block_inject.bin", &w)) {
      std::printf("  [idx %d] capture read failed\n", idx);
      ok = false;
      continue;
    }
    if (static_cast<int>(x.size()) != dim ||
        static_cast<int>(w.size()) != hc * dim) {
      std::printf("  [idx %d] unexpected capture size x=%zu w=%zu\n", idx,
                  x.size(), w.size());
      ok = false;
      continue;
    }
    ok &= RunHcProdCase(x, w, hc, hs, ("s7-idx" + std::to_string(idx)).c_str());
  }
  return ok;
}

// ---------------------------------------------------------------------------
// O1 first-divergence location: the mix down GEMM (first GEMM in the layer)
// on the real S7 Part C a=2 inputs. The production T=1 path is Bf16Gev (M=1
// GEMV, via ProjGemm); the T>1 path is Bf16Gemm (cuBLASLt). This runs both on
// the exact captured failing-decode input and reports the first divergence.
// It also cross-checks the test's T=1 recompute against the captured
// production d_down (sanity: we are calling the right operator).
namespace {

bool RunMixDownCase(const std::vector<uint16_t>& normed,
                    const std::vector<uint16_t>& mix_down,
                    const std::vector<uint16_t>& captured_down, int lr,
                    int hc_dim, const char* tag) {
  uint16_t *d_x = nullptr, *d_w = nullptr, *d_x4 = nullptr, *d_yA = nullptr,
           *d_yB = nullptr, *d_yC = nullptr;
  void* ws = nullptr;
  cudaMalloc(&d_w, static_cast<size_t>(lr) * hc_dim * 2);
  cudaMalloc(&d_x, static_cast<size_t>(hc_dim) * 2);
  cudaMalloc(&d_x4, static_cast<size_t>(4) * hc_dim * 2);
  cudaMalloc(&d_yA, lr * 2);
  cudaMalloc(&d_yB, lr * 2);
  cudaMalloc(&d_yC, static_cast<size_t>(4) * lr * 2);
  cudaMalloc(&ws, 32u << 20);
  bool ok = true;

  cudaMemcpy(d_w, mix_down.data(), mix_down.size() * 2,
             cudaMemcpyHostToDevice);
  cudaMemcpy(d_x, normed.data(), normed.size() * 2, cudaMemcpyHostToDevice);
  std::vector<uint16_t> x4(static_cast<size_t>(4) * hc_dim, 0);
  std::memcpy(x4.data(), normed.data(), hc_dim * 2);
  cudaMemcpy(d_x4, x4.data(), x4.size() * 2, cudaMemcpyHostToDevice);

  // T=1 production path (GEMV) and generic paths.
  ok &= Bf16Gev(d_x, d_w, d_yA, lr, hc_dim, 1.0f, nullptr);
  const auto r1 = Bf16Gemm(d_x, d_w, d_yB, 1, lr, hc_dim, 1.0f, 0.0f, ws,
                           32u << 20, nullptr);
  ok &= r1.has_algo;
  const auto r4 = Bf16Gemm(d_x4, d_w, d_yC, 4, lr, hc_dim, 1.0f, 0.0f, ws,
                           32u << 20, nullptr);
  ok &= r4.has_algo;
  cudaDeviceSynchronize();

  std::vector<uint16_t> yA(lr), yB(lr), yC(4 * lr);
  cudaMemcpy(yA.data(), d_yA, lr * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(yB.data(), d_yB, lr * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(yC.data(), d_yC, 4 * lr * 2, cudaMemcpyDeviceToHost);
  const std::vector<uint16_t> yC0(yC.begin(), yC.begin() + lr);

  std::printf("[mix-down %s] lr=%d hc_dim=%d\n", tag, lr, hc_dim);
  // Sanity: test T=1 recompute vs the captured production d_down.
  const DiffStats sanity = CompareBf16(yA, captured_down);
  Report("gemv vs captured-prod", sanity, lr);
  const DiffStats g1 = CompareBf16(yA, yB);
  const DiffStats m14 = CompareBf16(yB, yC0);
  const DiffStats g4 = CompareBf16(yA, yC0);
  Report("gemv vs gemm1", g1, lr);
  Report("gemm1 vs gemm4", m14, lr);
  Report("gemv vs gemm4", g4, lr);
  // FP64 reference.
  std::vector<double> ref(lr, 0.0);
  for (int n = 0; n < lr; ++n)
    for (int k = 0; k < hc_dim; ++k)
      ref[n] += B2D(normed[k]) *
                B2D(mix_down[static_cast<size_t>(n) * hc_dim + k]);
  std::printf("  l2rel-to-FP64ref: gemv=%.3e gemm1=%.3e gemm4=%.3e\n",
              L2RelToRef(yA, ref), L2RelToRef(yB, ref), L2RelToRef(yC0, ref));
  if (g4.first >= 0) {
    std::printf("  first-diff elem %d: gemv=%.6f gemm1=%.6f gemm4=%.6f "
                "ref=%.6f\n",
                g4.first, B2D(yA[g4.first]), B2D(yB[g4.first]),
                B2D(yC0[g4.first]), ref[g4.first]);
  }
  cudaFree(d_w);
  cudaFree(d_x);
  cudaFree(d_x4);
  cudaFree(d_yA);
  cudaFree(d_yB);
  cudaFree(d_yC);
  cudaFree(ws);
  return ok;
}

}  // namespace

Q4T_TEST(mix_down_actual_s7) {
  if (!CudaAvailable())
    Q4T_SKIP("no CUDA");
  const char* env = std::getenv("Q4T_OP_CAPTURE_DIR");
  const std::string dir = env ? env : "/tmp/q4t_op_capture_s7";
  const std::string meta_path = dir + "/mix_meta.txt";
  if (!std::ifstream(meta_path)) {
    std::printf("  (no S7 mix capture at %s; run model_verify_ckpt_samepath "
                "with Q4T_S7_CAPTURE first)\n",
                dir.c_str());
    return true;
  }
  std::ifstream meta(meta_path);
  std::vector<int> t1_idx;
  std::string line;
  while (std::getline(meta, line)) {
    int idx = -1, T = -1;
    if (std::sscanf(line.c_str(), "idx=%d T=%d", &idx, &T) == 2 && T == 1)
      t1_idx.push_back(idx);
  }
  if (t1_idx.empty()) {
    std::printf("  (mix capture present but no T=1 call)\n");
    return true;
  }
  std::printf("  S7 mix capture: %zu T=1 call(s) from %s\n", t1_idx.size(),
              dir.c_str());
  const int lr = 320, hc_dim = 10240;
  bool ok = true;
  for (int idx : t1_idx) {
    const std::string base = dir + "/mix_" + std::to_string(idx) + "_T1";
    std::vector<uint16_t> normed, mix_down, down;
    if (!ReadBf16(base + "_normed.bin", &normed) ||
        !ReadBf16(base + "_mix_down.bin", &mix_down) ||
        !ReadBf16(base + "_down.bin", &down)) {
      std::printf("  [idx %d] mix capture read failed\n", idx);
      ok = false;
      continue;
    }
    if (static_cast<int>(normed.size()) != hc_dim ||
        static_cast<int>(mix_down.size()) != lr * hc_dim ||
        static_cast<int>(down.size()) != lr) {
      std::printf("  [idx %d] unexpected mix capture size\n", idx);
      ok = false;
      continue;
    }
    ok &= RunMixDownCase(normed, mix_down, down, lr, hc_dim,
                         ("s7-idx" + std::to_string(idx)).c_str());
  }
  return ok;
}

// ---------------------------------------------------------------------------
// O1 accumulation/rounding evidence (mix-down idx12/13/14): capture the
// pre-rounding FP32 accumulation of the T=1 GEMV (Bf16GevFp32) and compare it
// with the T=4 cuBLASLt GEMM (M=4 row 0) BF16 result and the FP64 reference.
// This characterizes the M=1-vs-M=4 difference as accumulation-order vs
// rounding with real evidence (docs/STATUS.md 当前阶段 O1). Diagnostic only:
// prints and returns true; does not assert a contract.
namespace {

// The BF16 bin [lo, hi) that rounds (round-to-nearest-even) to `b`.
void Bf16Bin(uint16_t b, double* lo, double* hi) {
  const double v = B2D(b);
  // Step for the bin containing v: for |v| in [2^e, 2^(e+1)) step = 2^(e-7).
  int e = 0;
  double a = std::fabs(v);
  while (a >= 2.0) { a /= 2.0; ++e; }
  while (a < 1.0) { a *= 2.0; --e; }
  const double step = std::ldexp(1.0, e - 7);
  *lo = v - step / 2.0;
  *hi = v + step / 2.0;
}

bool RunMixDownFp32Case(const std::vector<uint16_t>& normed,
                        const std::vector<uint16_t>& mix_down, int lr,
                        int hc_dim, const char* tag) {
  uint16_t *d_x = nullptr, *d_w = nullptr, *d_x4 = nullptr, *d_yA = nullptr,
           *d_yC = nullptr;
  float* d_fp32 = nullptr;
  void* ws = nullptr;
  cudaMalloc(&d_w, static_cast<size_t>(lr) * hc_dim * 2);
  cudaMalloc(&d_x, static_cast<size_t>(hc_dim) * 2);
  cudaMalloc(&d_x4, static_cast<size_t>(4) * hc_dim * 2);
  cudaMalloc(&d_yA, lr * 2);
  cudaMalloc(&d_yC, static_cast<size_t>(4) * lr * 2);
  cudaMalloc(&d_fp32, lr * sizeof(float));
  cudaMalloc(&ws, 32u << 20);
  bool ok = true;

  cudaMemcpy(d_w, mix_down.data(), mix_down.size() * 2,
             cudaMemcpyHostToDevice);
  cudaMemcpy(d_x, normed.data(), normed.size() * 2, cudaMemcpyHostToDevice);
  std::vector<uint16_t> x4(static_cast<size_t>(4) * hc_dim, 0);
  std::memcpy(x4.data(), normed.data(), hc_dim * 2);
  cudaMemcpy(d_x4, x4.data(), x4.size() * 2, cudaMemcpyHostToDevice);

  // T=1 GEMV with FP32 capture.
  ok &= Bf16GevFp32(d_x, d_w, d_yA, d_fp32, lr, hc_dim, 1.0f, nullptr);
  // T=4 cuBLASLt GEMM, row 0.
  const auto r4 = Bf16Gemm(d_x4, d_w, d_yC, 4, lr, hc_dim, 1.0f, 0.0f, ws,
                           32u << 20, nullptr);
  ok &= r4.has_algo;
  cudaDeviceSynchronize();

  std::vector<uint16_t> yA(lr), yC(4 * lr);
  std::vector<float> fp32(lr);
  cudaMemcpy(yA.data(), d_yA, lr * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(yC.data(), d_yC, 4 * lr * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(fp32.data(), d_fp32, lr * sizeof(float), cudaMemcpyDeviceToHost);
  const std::vector<uint16_t> yC0(yC.begin(), yC.begin() + lr);

  // FP64 reference.
  std::vector<double> ref(lr);
  for (int n = 0; n < lr; ++n)
    for (int k = 0; k < hc_dim; ++k)
      ref[n] += B2D(normed[k]) *
                B2D(mix_down[static_cast<size_t>(n) * hc_dim + k]);

  std::printf("[mix-down-fp32 %s] lr=%d hc_dim=%d\n", tag, lr, hc_dim);
  int ndiff = 0;
  for (int n = 0; n < lr; ++n) {
    if (yA[n] == yC0[n]) continue;
    ++ndiff;
    double loA, hiA, loC, hiC;
    Bf16Bin(yA[n], &loA, &hiA);
    Bf16Bin(yC0[n], &loC, &hiC);
    std::printf("  elem %d: GEMV_bf16=%.6f (fp32=%.7f, bin[%.6f,%.6f)) "
                "cuBLASLt_bf16=%.6f (bin[%.6f,%.6f)) ref_fp64=%.7f\n",
                n, B2D(yA[n]), fp32[n], loA, hiA, B2D(yC0[n]), loC, hiC,
                ref[n]);
    const double dA = std::fabs(static_cast<double>(fp32[n]) - ref[n]);
    std::printf("      |fp32-ref|=%.3e  ref in GEMV bin? %s  ref in cuBLASLt "
                "bin? %s\n",
                dA, (ref[n] >= loA && ref[n] < hiA) ? "yes" : "no",
                (ref[n] >= loC && ref[n] < hiC) ? "yes" : "no");
  }
  std::printf("  => %d differing element(s)\n", ndiff);
  cudaFree(d_w);
  cudaFree(d_x);
  cudaFree(d_x4);
  cudaFree(d_yA);
  cudaFree(d_yC);
  cudaFree(d_fp32);
  cudaFree(ws);
  return ok;
}

}  // namespace

Q4T_TEST(mix_down_fp32_s7) {
  if (!CudaAvailable())
    Q4T_SKIP("no CUDA");
  const char* env = std::getenv("Q4T_OP_CAPTURE_DIR");
  const std::string dir = env ? env : "/tmp/q4t_op_capture_s7_v2";
  const std::string meta_path = dir + "/mix_meta.txt";
  if (!std::ifstream(meta_path)) {
    std::printf("  (no S7 mix capture at %s)\n", dir.c_str());
    return true;
  }
  std::ifstream meta(meta_path);
  std::vector<int> t1_idx;
  std::string line;
  while (std::getline(meta, line)) {
    int idx = -1, T = -1;
    if (std::sscanf(line.c_str(), "idx=%d T=%d", &idx, &T) == 2 && T == 1)
      t1_idx.push_back(idx);
  }
  const int lr = 320, hc_dim = 10240;
  bool ok = true;
  for (int idx : t1_idx) {
    const std::string base = dir + "/mix_" + std::to_string(idx) + "_T1";
    std::vector<uint16_t> normed, mix_down, down;
    if (!ReadBf16(base + "_normed.bin", &normed) ||
        !ReadBf16(base + "_mix_down.bin", &mix_down) ||
        !ReadBf16(base + "_down.bin", &down)) {
      std::printf("  [idx %d] capture read failed\n", idx);
      ok = false;
      continue;
    }
    if (static_cast<int>(normed.size()) != hc_dim ||
        static_cast<int>(mix_down.size()) != lr * hc_dim ||
        static_cast<int>(down.size()) != lr) {
      std::printf("  [idx %d] unexpected capture size\n", idx);
      ok = false;
      continue;
    }
    ok &= RunMixDownFp32Case(normed, mix_down, lr, hc_dim,
                             ("s7-idx" + std::to_string(idx)).c_str());
  }
  return ok;
}

// O2 candidate re-verification: drive the ACTUAL production mix-down branch
// (MixDownStep — the as-is extraction of the HyperConnectionMixImpl step,
// gated by Q4T_HC_MIX_ROWWISE_GEMV) on the captured S7 mix-down input and
// compare the T=4 row-0 output bit-exactly with the T=1 GEMV.
//
// The captured `normed` is the post-RMSNorm production input. This test
// feeds it to the mix-down operator directly: no layer entry point, no
// re-normalization, no dummy layer weights, so the operator input is
// exactly the captured input (the previous whole-layer version re-ran the
// RMSNorm on the captured normed and its "same operator input" claim was
// invalid). Strict input cross-check: the Q4T_OP_CAPTURE hook inside
// MixDownStep dumps the normed the branch actually consumed; the test
// verifies it is bit-identical to the input it passed.
// Negative control: the same call with the candidate OFF (default
// ProjGemm -> Bf16Gemm M=4 path) must reproduce the old row-0 behavior
// bit-exactly (deterministic cuBLASLt), i.e. the refactor keeps the old
// path intact.
namespace {

// Read a capture dir's mix_meta.txt; return the idx values in call order.
std::vector<int> ReadMixMetaIdx(const std::string& meta_path) {
  std::vector<int> idxs;
  std::ifstream meta(meta_path);
  std::string line;
  while (std::getline(meta, line)) {
    int idx = -1, T = -1;
    if (std::sscanf(line.c_str(), "idx=%d T=%d", &idx, &T) == 2)
      idxs.push_back(idx);
  }
  return idxs;
}

// Drive one production MixDownStep call (T=4, row 0 = captured normed,
// rows 1-3 zero — row-wise independence makes them irrelevant to row 0)
// with the capture hook into `cand_dir`; return the row-0 output and
// verify the consumed input is exactly what was passed.
bool RunCandidateCall(const std::vector<uint16_t>& normed,
                      const std::vector<uint16_t>& mix_down, int lr,
                      int hc_dim, const std::string& cand_dir, int T,
                      std::vector<uint16_t>* row0) {
  q4t::model::HyperConnectionWeights w;
  w.hc_count = 4;
  w.hidden_size = 2560;
  w.lowrank = lr;
  // mix_down_fp8 stays default (w == nullptr): the candidate gate's "no
  // FP8 shadow" condition holds and ProjGemm falls through to Bf16Gemm.
  if (cudaMalloc(&w.mix_down, mix_down.size() * 2) != cudaSuccess) {
    std::printf("  cudaMalloc mix_down: %s\n",
                cudaGetErrorString(cudaGetLastError()));
    return false;
  }
  if (cudaMemcpy(w.mix_down, mix_down.data(), mix_down.size() * 2,
                 cudaMemcpyHostToDevice) != cudaSuccess) {
    std::printf("  cudaMemcpy mix_down: %s\n",
                cudaGetErrorString(cudaGetLastError()));
    w.Free();
    return false;
  }

  std::vector<uint16_t> x4(static_cast<size_t>(T) * hc_dim, 0);
  std::memcpy(x4.data(), normed.data(), hc_dim * 2);
  uint16_t *d_x = nullptr, *d_down = nullptr;
  void* ws = nullptr;
  if (cudaMalloc(&d_x, x4.size() * 2) != cudaSuccess ||
      cudaMalloc(&d_down, static_cast<size_t>(T) * lr * 2) != cudaSuccess ||
      cudaMalloc(&ws, 32u << 20) != cudaSuccess) {
    std::printf("  cudaMalloc failed: %s\n",
                cudaGetErrorString(cudaGetLastError()));
    w.Free();
    return false;
  }
  cudaMemcpy(d_x, x4.data(), x4.size() * 2, cudaMemcpyHostToDevice);

  setenv("Q4T_OP_CAPTURE", cand_dir.c_str(), 1);
  Status s = q4t::model::MixDownStep(w, d_x, d_down, T, ws, 32u << 20,
                                     nullptr);
  cudaDeviceSynchronize();
  if (!s.ok()) {
    std::printf("  MixDownStep failed: %s\n", s.message().c_str());
    cudaFree(d_x);
    cudaFree(d_down);
    cudaFree(ws);
    w.Free();
    return false;
  }

  // The capture hook dumped this call's inputs/outputs. The static call
  // counter is process-wide, so resolve the idx from the meta.
  const std::vector<int> idxs = ReadMixMetaIdx(cand_dir + "/mix_meta.txt");
  if (idxs.empty()) {
    std::printf("  capture meta missing in %s\n", cand_dir.c_str());
    cudaFree(d_x);
    cudaFree(d_down);
    cudaFree(ws);
    w.Free();
    return false;
  }
  const int idx = idxs.back();
  const std::string base = cand_dir + "/mix_" + std::to_string(idx) + "_T" + std::to_string(T);
  std::vector<uint16_t> consumed, down;
  if (!ReadBf16(base + "_normed.bin", &consumed) ||
      !ReadBf16(base + "_down.bin", &down) ||
      consumed.size() != x4.size() ||
      down.size() != static_cast<size_t>(T) * lr) {
    std::printf("  candidate capture read failed (idx %d)\n", idx);
    cudaFree(d_x);
    cudaFree(d_down);
    cudaFree(ws);
    w.Free();
    return false;
  }
  // Strict input cross-check: the branch consumed exactly what we passed.
  const DiffStats in_chk = CompareBf16(consumed, x4);
  std::printf("  input cross-check (consumed vs passed): count=%d/%zu %s\n",
              in_chk.count, consumed.size(),
              in_chk.count == 0 ? "BIT-EXACT" : "MISMATCH");
  *row0 = std::vector<uint16_t>(down.begin(), down.begin() + lr);
  cudaFree(d_x);
  cudaFree(d_down);
  cudaFree(ws);
  w.Free();
  return in_chk.count == 0;
}

}  // namespace

Q4T_TEST(mix_down_candidate_s7) {
  if (!CudaAvailable())
    Q4T_SKIP("no CUDA");
  const char* env = std::getenv("Q4T_OP_CAPTURE_DIR");
  const std::string dir = env ? env : "/tmp/q4t_op_capture_s7_v2";
  const std::string meta_path = dir + "/mix_meta.txt";
  if (!std::ifstream(meta_path)) {
    std::printf("  (no S7 mix capture at %s)\n", dir.c_str());
    return true;
  }
  std::ifstream meta(meta_path);
  std::vector<int> t1_idx;
  std::string line;
  while (std::getline(meta, line)) {
    int idx = -1, T = -1;
    if (std::sscanf(line.c_str(), "idx=%d T=%d", &idx, &T) == 2 && T == 1)
      t1_idx.push_back(idx);
  }
  if (t1_idx.empty()) {
    std::printf("  (mix capture present but no T=1 call)\n");
    return true;
  }
  const int lr = 320, hc_dim = 10240;

  // Fresh capture dirs per phase (candidate ON / OFF negative control),
  // per batch size: T=4 (cap4, original bound) and T=8 (cap8 experiment).
  // The output base defaults to <capture dir>_cand_out; override with
  // Q4T_CAND_OUT_DIR so a re-run (e.g. the cap8 round) uses fresh dirs and
  // the rm -rf below does not clobber a previous round's *_cand_out_*
  // evidence. The capture dir itself is only read.
  const char* out_env = std::getenv("Q4T_CAND_OUT_DIR");
  const std::string out_base = out_env ? out_env : dir + "_cand_out";
  const std::string cand_dir_on = out_base + "_on";
  const std::string cand_dir_off = out_base + "_off";
  const std::string cand_dir_on_t8 = out_base + "_on_t8";
  const std::string cand_dir_off_t8 = out_base + "_off_t8";
  const int rm_rc =
      std::system(("rm -rf " + cand_dir_on + " " + cand_dir_off +
                   " " + cand_dir_on_t8 + " " + cand_dir_off_t8 +
                   " && mkdir -p " + cand_dir_on + " " + cand_dir_off +
                   " " + cand_dir_on_t8 + " " + cand_dir_off_t8)
                     .c_str());
  (void)rm_rc;

  bool ok = true;
  int on_calls = 0, off_calls = 0;
  for (int idx : t1_idx) {
    const std::string base = dir + "/mix_" + std::to_string(idx) + "_T1";
    std::vector<uint16_t> normed, mix_down, down;
    if (!ReadBf16(base + "_normed.bin", &normed) ||
        !ReadBf16(base + "_mix_down.bin", &mix_down) ||
        !ReadBf16(base + "_down.bin", &down)) {
      std::printf("  [idx %d] capture read failed\n", idx);
      ok = false;
      continue;
    }
    if (static_cast<int>(normed.size()) != hc_dim ||
        static_cast<int>(mix_down.size()) != lr * hc_dim ||
        static_cast<int>(down.size()) != lr) {
      std::printf("  [idx %d] unexpected capture size\n", idx);
      ok = false;
      continue;
    }

    // T=1 GEMV (production single-step path) on the captured row.
    uint16_t *d_x = nullptr, *d_w = nullptr, *d_y = nullptr;
    cudaMalloc(&d_x, hc_dim * 2);
    cudaMalloc(&d_w, mix_down.size() * 2);
    cudaMalloc(&d_y, lr * 2);
    cudaMemcpy(d_x, normed.data(), hc_dim * 2, cudaMemcpyHostToDevice);
    cudaMemcpy(d_w, mix_down.data(), mix_down.size() * 2,
               cudaMemcpyHostToDevice);
    bool gemv_ok = Bf16Gev(d_x, d_w, d_y, lr, hc_dim, 1.0f, nullptr);
    cudaDeviceSynchronize();
    std::vector<uint16_t> yA(lr);
    cudaMemcpy(yA.data(), d_y, lr * 2, cudaMemcpyDeviceToHost);
    cudaFree(d_x);
    cudaFree(d_w);
    cudaFree(d_y);
    if (!gemv_ok) {
      std::printf("  [idx %d] T=1 GEMV failed\n", idx);
      ok = false;
      continue;
    }

    // One (ON, OFF) phase pair at batch size T on the same captured row:
    // ON row 0 must be bit-exact vs the T=1 GEMV (same kernel, same
    // input); OFF row 0 must be bit-exact vs the direct Bf16Gemm M=T row
    // 0 (old behavior negative control).
    const auto run_phase = [&](int T, const std::string& dir_on,
                               const std::string& dir_off) {
      // Candidate ON (row-wise GEMV).
      setenv("Q4T_HC_MIX_ROWWISE_GEMV", "1", 1);
      std::vector<uint16_t> row_on;
      if (RunCandidateCall(normed, mix_down, lr, hc_dim, dir_on, T,
                           &row_on)) {
        on_calls++;
        const DiffStats d = CompareBf16(row_on, yA);
        std::printf("  [idx %d] candidate ON  (T=%d row0) vs T=1 GEMV: "
                    "first_diff=%d count=%d/%d max_abs=%.3e l2rel=%.3e %s\n",
                    idx, T, d.first, d.count, lr, d.max_abs, d.l2rel,
                    d.count == 0 ? "BIT-EXACT" : "DIFF");
        if (d.count != 0) ok = false;
      } else {
        ok = false;
      }
      // Candidate OFF (default ProjGemm -> Bf16Gemm M=T): old behavior.
      unsetenv("Q4T_HC_MIX_ROWWISE_GEMV");
      std::vector<uint16_t> row_off;
      if (RunCandidateCall(normed, mix_down, lr, hc_dim, dir_off, T,
                           &row_off)) {
        off_calls++;
        uint16_t *d_xT = nullptr, *d_w2 = nullptr, *d_yT = nullptr;
        void* ws2 = nullptr;
        cudaMalloc(&d_xT, static_cast<size_t>(T) * hc_dim * 2);
        cudaMalloc(&d_w2, mix_down.size() * 2);
        cudaMalloc(&d_yT, static_cast<size_t>(T) * lr * 2);
        cudaMalloc(&ws2, 32u << 20);
        std::vector<uint16_t> xT(static_cast<size_t>(T) * hc_dim, 0);
        std::memcpy(xT.data(), normed.data(), hc_dim * 2);
        cudaMemcpy(d_xT, xT.data(), xT.size() * 2, cudaMemcpyHostToDevice);
        cudaMemcpy(d_w2, mix_down.data(), mix_down.size() * 2,
                   cudaMemcpyHostToDevice);
        const auto rT = Bf16Gemm(d_xT, d_w2, d_yT, T, lr, hc_dim, 1.0f, 0.0f,
                                 ws2, 32u << 20, nullptr);
        cudaDeviceSynchronize();
        std::vector<uint16_t> yT(static_cast<size_t>(T) * lr);
        cudaMemcpy(yT.data(), d_yT, yT.size() * 2, cudaMemcpyDeviceToHost);
        cudaFree(d_xT);
        cudaFree(d_w2);
        cudaFree(d_yT);
        cudaFree(ws2);
        if (!rT.has_algo) {
          std::printf("  [idx %d] Bf16Gemm M=%d reference failed\n", idx, T);
          ok = false;
          return;
        }
        const std::vector<uint16_t> yT_0(yT.begin(), yT.begin() + lr);
        const DiffStats d = CompareBf16(row_off, yT_0);
        std::printf("  [idx %d] candidate OFF (T=%d row0) vs Bf16Gemm M=%d: "
                    "first_diff=%d count=%d/%d max_abs=%.3e l2rel=%.3e %s\n",
                    idx, T, T, d.first, d.count, lr, d.max_abs, d.l2rel,
                    d.count == 0 ? "BIT-EXACT" : "DIFF");
        if (d.count != 0) ok = false;
        // Old behavior vs T=1 GEMV: report (expected single-ULP class, not
        // asserted) so the residual stays visible next to the control.
        const DiffStats d2 = CompareBf16(row_off, yA);
        std::printf("  [idx %d] candidate OFF (T=%d row0) vs T=1 GEMV: "
                    "first_diff=%d count=%d/%d max_abs=%.3e l2rel=%.3e "
                    "(residual, not asserted)\n",
                    idx, T, d2.first, d2.count, lr, d2.max_abs, d2.l2rel);
      } else {
        ok = false;
      }
    };

    run_phase(4, cand_dir_on, cand_dir_off);        // cap4 (original)
    run_phase(8, cand_dir_on_t8, cand_dir_off_t8);  // cap8 (new)
  }

  unsetenv("Q4T_HC_MIX_ROWWISE_GEMV");
  unsetenv("Q4T_OP_CAPTURE");
  std::printf("  candidate production-branch calls: on=%d off=%d "
              "(T=4 cap4 + T=8 cap8, no FP8 shadow, hc_dim%%8==0)\n",
              on_calls, off_calls);
  return ok;
}

// Wiring check for Bf16SmallMGemm (src/model/small_m_gemm.cu): Bf16Gemm
// with 2 <= M <= 8 routes every dense shape (K % 8 == 0) to the
// hand-written small-M kernel (all-dense policy, see
// Bf16SmallMShapeRouted). The Scattered kernel reproduces Bf16GevKernel's
// per-lane accumulation order exactly, so every routed row is bit-identical
// to the M=1 GEMV — the T-path GEMV-alignment contract that keeps the MTP
// verify step (T=k+1) from flipping near-tie argmaxes vs plain greedy.
// Asserts:
//   (a) policy — Bf16SmallMShapeRouted routes all dense 2 <= M <= 8 shapes
//       with K % 8 == 0 and rejects M out of range / K % 8 != 0;
//   (b) determinism — repeated routed M=4 calls (HC down) are bit-stable;
//   (c) correctness — row-0 l2rel to an FP64 reference within the BF16
//       accumulation tolerance (same class as Bf16Gev);
//   (d) row-0 bit-identical to the M=1 GEMV path (GEMV-alignment contract);
//   (e) routing proof — under Q4T_ABLATE_GEMM_TILE the M=1 entry pads to
//       M=4 and forces the cuBLASLt plan (the small-M path is skipped for
//       that ablation); if the default M=4 path were still cuBLASLt, the
//       two row-0 results would be bit-identical;
//   (f) a second routed shape (HC up N=10240 K=320): row-0 bit-identical
//       to the M=1 GEMV, default differs from the ablation-forced cuBLASLt
//       M=4 row-0 (single-ULP class, routing proof), deterministic +
//       correct.
Q4T_TEST(smallm_gemm_wiring) {
  // (a) policy unit checks (no GPU needed).
  const auto R = Bf16SmallMShapeRouted;
  bool ok = true;
  ok = ok && R(2, 320, 10240) && R(4, 320, 10240) && R(8, 320, 10240);
  ok = ok && R(2, 248320, 2560) && R(4, 248320, 2560);
  // K=2560 large-N shapes are routed for ALL M=2..8 (verify T=4 and the
  // chunked-prefill last chunk T=8 must share the GEMV-aligned Scattered
  // arithmetic; see Bf16SmallMShapeRouted).
  ok = ok && R(5, 248320, 2560) && R(8, 248320, 2560);
  ok = ok && R(5, 10240, 2560) && R(8, 10240, 2560);
  // All-dense policy: every dense projection shape with K % 8 == 0 is
  // routed for all M=2..8 (incl. the former "measured losers" at M=4,
  // which the v2 bench shows are cuBLASLt ties or wins on the decode/
  // verify hot path; the M=8 prefill-only losers never touch it).
  ok = ok && R(4, 10240, 320) && R(4, 10240, 2560) && R(4, 2560, 6144);
  ok = ok && R(4, 6144, 2560) && R(4, 7168, 2560) && R(8, 7168, 2560);
  ok = ok && !R(1, 320, 10240) && !R(9, 320, 10240) && !R(4, 320, 10241);
  if (!CudaAvailable()) {
    if (!ok) return false;
    Q4T_SKIP("no CUDA (policy checks passed)");
  }
  // Routed shape: HC down (N=320, K=10240), Scattered variant.
  const int M = 4, N = 320, K = 10240;
  std::mt19937 rng(20261012u);
  std::normal_distribution<double> dist(0.0, 0.1);
  std::vector<uint16_t> xh(static_cast<size_t>(M) * K),
      wh(static_cast<size_t>(N) * K);
  for (auto& v : xh) v = F2B(static_cast<float>(dist(rng)));
  for (auto& v : wh) v = F2B(static_cast<float>(dist(rng)));

  uint16_t *d_x = nullptr, *d_w = nullptr, *d_y = nullptr, *d_y1 = nullptr;
  void* ws = nullptr;
  if (cudaMalloc(&d_x, static_cast<size_t>(M) * K * 2) != cudaSuccess ||
      cudaMalloc(&d_w, static_cast<size_t>(N) * K * 2) != cudaSuccess ||
      cudaMalloc(&d_y, static_cast<size_t>(M) * N * 2) != cudaSuccess ||
      cudaMalloc(&d_y1, static_cast<size_t>(N) * 2) != cudaSuccess ||
      cudaMalloc(&ws, 32u << 20) != cudaSuccess) {
    return false;
  }
  cudaMemcpy(d_x, xh.data(), static_cast<size_t>(M) * K * 2,
             cudaMemcpyHostToDevice);
  cudaMemcpy(d_w, wh.data(), static_cast<size_t>(N) * K * 2,
             cudaMemcpyHostToDevice);

  // (b) determinism: two M=4 runs bit-identical.
  const auto r1 = Bf16Gemm(d_x, d_w, d_y, M, N, K, 1.0f, 0.0f, ws, 32u << 20,
                           nullptr);
  Q4T_CHECK(r1.has_algo);
  std::vector<uint16_t> y1(static_cast<size_t>(M) * N),
      y2(static_cast<size_t>(M) * N);
  cudaMemcpy(y1.data(), d_y, static_cast<size_t>(M) * N * 2,
             cudaMemcpyDeviceToHost);
  const auto r2 = Bf16Gemm(d_x, d_w, d_y, M, N, K, 1.0f, 0.0f, ws, 32u << 20,
                           nullptr);
  Q4T_CHECK(r2.has_algo);
  cudaMemcpy(y2.data(), d_y, static_cast<size_t>(M) * N * 2,
             cudaMemcpyDeviceToHost);
  cudaDeviceSynchronize();
  const DiffStats det = CompareBf16(y1, y2);
  Report("HCdown M4 run1 vs run2", det, y1.size());
  ok = ok && det.count == 0;

  // (c) correctness vs FP64 reference (row 0).
  std::vector<uint16_t> x0(K);
  std::memcpy(x0.data(), xh.data(), K * 2);
  const std::vector<double> ref = RefGemv(x0, wh, N, K);
  std::vector<uint16_t> row0(y1.begin(), y1.begin() + N);
  const double l2ref = L2RelToRef(row0, ref);
  std::printf("  l2rel-to-FP64ref: HCdown M4 row0 = %.3e\n", l2ref);
  ok = ok && l2ref < 2e-3;

  // (d) row-0 vs M=1 GEMV: must be bit-identical (GEMV-alignment
  // contract of the Scattered kernel).
  Q4T_CHECK(Bf16Gev(d_x, d_w, d_y1, N, K, 1.0f, nullptr));
  std::vector<uint16_t> y_gemv(N);
  cudaMemcpy(y_gemv.data(), d_y1, N * 2, cudaMemcpyDeviceToHost);
  const DiffStats g4 = CompareBf16(row0, y_gemv);
  Report("HCdown M4 row0 vs M1 GEMV (must be identical)", g4, N);
  ok = ok && g4.count == 0;

  // (e) routing proof: ablation-forced cuBLASLt M=4 row 0 must differ from
  // the default M=4 row 0 (different summation order).
  setenv("Q4T_ABLATE_GEMM_TILE", "1", 1);
  const auto ra = Bf16Gemm(d_x, d_w, d_y1, 1, N, K, 1.0f, 0.0f, ws, 32u << 20,
                           nullptr);
  Q4T_CHECK(ra.has_algo);
  unsetenv("Q4T_ABLATE_GEMM_TILE");
  std::vector<uint16_t> y_cub(N);
  cudaMemcpy(y_cub.data(), d_y1, N * 2, cudaMemcpyDeviceToHost);
  const DiffStats route = CompareBf16(row0, y_cub);
  Report("HCdown M4 default vs M4 cuBLASLt", route, N);
  ok = ok && route.count > 0;

  // (f) routed shape (HC up N=10240 K=320): the all-dense shape policy
  // (Bf16SmallMShapeRouted) routes it to the Scattered kernel, so the
  // default M=4 row-0 must be bit-identical to the M=1 GEMV (T-path
  // GEMV-alignment contract) and must differ from the ablation-forced
  // cuBLASLt M=4 row-0 (routing proof, single-ULP class); also
  // deterministic + correct.
  {
    const int N2 = 10240, K2 = 320;
    std::vector<uint16_t> x2h(static_cast<size_t>(M) * K2),
        w2h(static_cast<size_t>(N2) * K2);
    for (auto& v : x2h) v = F2B(static_cast<float>(dist(rng)));
    for (auto& v : w2h) v = F2B(static_cast<float>(dist(rng)));
    uint16_t *d_x2 = nullptr, *d_w2 = nullptr, *d_y2 = nullptr;
    if (cudaMalloc(&d_x2, static_cast<size_t>(M) * K2 * 2) != cudaSuccess ||
        cudaMalloc(&d_w2, static_cast<size_t>(N2) * K2 * 2) != cudaSuccess ||
        cudaMalloc(&d_y2, static_cast<size_t>(M) * N2 * 2) != cudaSuccess) {
      return false;
    }
    cudaMemcpy(d_x2, x2h.data(), static_cast<size_t>(M) * K2 * 2,
               cudaMemcpyHostToDevice);
    cudaMemcpy(d_w2, w2h.data(), static_cast<size_t>(N2) * K2 * 2,
               cudaMemcpyHostToDevice);
    const auto s1 = Bf16Gemm(d_x2, d_w2, d_y2, M, N2, K2, 1.0f, 0.0f, ws,
                             32u << 20, nullptr);
    Q4T_CHECK(s1.has_algo);
    std::vector<uint16_t> z1(static_cast<size_t>(M) * N2),
        z2(static_cast<size_t>(M) * N2);
    cudaMemcpy(z1.data(), d_y2, static_cast<size_t>(M) * N2 * 2,
               cudaMemcpyDeviceToHost);
    const auto s2 = Bf16Gemm(d_x2, d_w2, d_y2, M, N2, K2, 1.0f, 0.0f, ws,
                             32u << 20, nullptr);
    Q4T_CHECK(s2.has_algo);
    cudaMemcpy(z2.data(), d_y2, static_cast<size_t>(M) * N2 * 2,
               cudaMemcpyDeviceToHost);
    const DiffStats det2 = CompareBf16(z1, z2);
    Report("HCup M4 run1 vs run2", det2, z1.size());
    // GEMV alignment: M=4 row0 vs M=1 GEMV (must be bit-identical).
    Q4T_CHECK(Bf16Gev(d_x2, d_w2, d_y2, N2, K2, 1.0f, nullptr));
    std::vector<uint16_t> z_gemv(N2);
    cudaMemcpy(z_gemv.data(), d_y2, N2 * 2, cudaMemcpyDeviceToHost);
    const DiffStats zalign = CompareBf16(std::vector<uint16_t>(z1.begin(),
                                                               z1.begin() + N2),
                                         z_gemv);
    Report("HCup M4 row0 vs M1 GEMV (must be identical)", zalign, N2);
    // Routing proof: ablation-forced cuBLASLt M=4 row 0 must differ from
    // the default (hand-written) M=4 row 0.
    setenv("Q4T_ABLATE_GEMM_TILE", "1", 1);
    const auto sa = Bf16Gemm(d_x2, d_w2, d_y2, 1, N2, K2, 1.0f, 0.0f, ws,
                             32u << 20, nullptr);
    Q4T_CHECK(sa.has_algo);
    unsetenv("Q4T_ABLATE_GEMM_TILE");
    std::vector<uint16_t> z_cub(N2);
    cudaMemcpy(z_cub.data(), d_y2, N2 * 2, cudaMemcpyDeviceToHost);
    const DiffStats nroute = CompareBf16(std::vector<uint16_t>(z1.begin(),
                                                               z1.begin() + N2),
                                         z_cub);
    Report("HCup M4 default vs M4 cuBLASLt (must differ, ULP class)",
           nroute, N2);
    std::vector<uint16_t> x20(K2);
    std::memcpy(x20.data(), x2h.data(), K2 * 2);
    const std::vector<double> ref2 = RefGemv(x20, w2h, N2, K2);
    const double l2ref2 =
        L2RelToRef(std::vector<uint16_t>(z1.begin(), z1.begin() + N2), ref2);
    std::printf("  l2rel-to-FP64ref: HCup M4 row0 = %.3e\n", l2ref2);
    ok = ok && det2.count == 0 && zalign.count == 0 && nroute.count > 0 &&
         nroute.l2rel < 2e-3 && l2ref2 < 2e-3;
    cudaFree(d_x2);
    cudaFree(d_w2);
    cudaFree(d_y2);
  }

  // (g) Scattered routed shapes: row-0 must be bit-identical to M=1 GEMV.
  // The Scattered kernel reproduces Bf16GevKernel's per-lane K accumulation
  // order exactly (same lane stride, same float4 inner order, same
  // shfl_down tree; FMA operands commute), so the T=4 verify path aligns
  // with the T=1 decode path on these projections — eliminating the GEMM
  // source of the T-path residual.
  {
    const int Sc[][2] = {{10240, 2560}, {248320, 2560}};
    for (const auto& sc : Sc) {
      const int Ns = sc[0], Ks = sc[1];
      std::vector<uint16_t> xsh(static_cast<size_t>(M) * Ks),
          wsh(static_cast<size_t>(Ns) * Ks);
      for (auto& v : xsh) v = F2B(static_cast<float>(dist(rng)));
      for (auto& v : wsh) v = F2B(static_cast<float>(dist(rng)));
      uint16_t *d_xs = nullptr, *d_ws = nullptr, *d_ys = nullptr,
               *d_ys1 = nullptr;
      // x/y sized for M=8 (the larger of the M=4 and M=8 checks below).
      if (cudaMalloc(&d_xs, static_cast<size_t>(8) * Ks * 2) != cudaSuccess ||
          cudaMalloc(&d_ws, static_cast<size_t>(Ns) * Ks * 2) != cudaSuccess ||
          cudaMalloc(&d_ys, static_cast<size_t>(8) * Ns * 2) != cudaSuccess ||
          cudaMalloc(&d_ys1, Ns * 2) != cudaSuccess) {
        return false;
      }
      cudaMemcpy(d_xs, xsh.data(), static_cast<size_t>(M) * Ks * 2,
                 cudaMemcpyHostToDevice);
      cudaMemcpy(d_ws, wsh.data(), static_cast<size_t>(Ns) * Ks * 2,
                 cudaMemcpyHostToDevice);
      const auto sg = Bf16Gemm(d_xs, d_ws, d_ys, M, Ns, Ks, 1.0f, 0.0f, ws,
                               32u << 20, nullptr);
      Q4T_CHECK(sg.has_algo);
      Q4T_CHECK(Bf16Gev(d_xs, d_ws, d_ys1, Ns, Ks, 1.0f, nullptr));
      std::vector<uint16_t> row_s(Ns), gemv_s(Ns);
      cudaMemcpy(row_s.data(), d_ys, Ns * 2, cudaMemcpyDeviceToHost);
      cudaMemcpy(gemv_s.data(), d_ys1, Ns * 2, cudaMemcpyDeviceToHost);
      cudaDeviceSynchronize();
      const DiffStats sg4 = CompareBf16(row_s, gemv_s);
      std::printf("[scattered N=%d K=%d] M4 row0 vs M1 GEMV\n", Ns, Ks);
      Report("row0 vs GEMV (must be identical)", sg4, Ns);
      ok = ok && sg4.count == 0;
      // M=8 (chunked-prefill last chunk): same Scattered kernel (MAXM=8
      // template) must stay bit-identical to GEMV per row, keeping the
      // T=8 prefill path on the same arithmetic as T=1 decode.
      if (Ns == 10240) {
        const int M8 = 8;
        std::vector<uint16_t> x8h(static_cast<size_t>(M8) * Ks);
        for (auto& v : x8h) v = F2B(static_cast<float>(dist(rng)));
        cudaMemcpy(d_xs, x8h.data(), static_cast<size_t>(M8) * Ks * 2,
                   cudaMemcpyHostToDevice);
        const auto sg8 = Bf16Gemm(d_xs, d_ws, d_ys, M8, Ns, Ks, 1.0f, 0.0f,
                                  ws, 32u << 20, nullptr);
        Q4T_CHECK(sg8.has_algo);
        Q4T_CHECK(Bf16Gev(d_xs, d_ws, d_ys1, Ns, Ks, 1.0f, nullptr));
        cudaMemcpy(gemv_s.data(), d_ys1, Ns * 2, cudaMemcpyDeviceToHost);
        cudaMemcpy(row_s.data(), d_ys, Ns * 2, cudaMemcpyDeviceToHost);
        const DiffStats sg8d = CompareBf16(row_s, gemv_s);
        std::printf("[scattered N=%d K=%d] M8 row0 vs M1 GEMV\n", Ns, Ks);
        Report("row0 vs GEMV M8 (must be identical)", sg8d, Ns);
        ok = ok && sg8d.count == 0;
      }
      cudaFree(d_xs);
      cudaFree(d_ws);
      cudaFree(d_ys);
      cudaFree(d_ys1);
    }
  }

  cudaFree(d_x);
  cudaFree(d_w);
  cudaFree(d_y);
  cudaFree(d_y1);
  cudaFree(ws);
  return ok;
}
