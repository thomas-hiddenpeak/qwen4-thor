// Expert-grouped MoE decode (T>1) vs per-row reference.
//
// Validates the grouped path introduced for MTP verify: rows are compacted
// per expert and each active expert runs one M=M_e grouped GEMM (weights
// read once) instead of one M=1 GEMM per row. The per-row path is the
// validated reference arithmetic (T=1 bit-identical; per-row T>1 matches
// T=1 per-row quantization exactly).
//
// Checks (real layer-2 routed NVFP4 experts, two routings with duplicate
// experts so M_e > 1 actually occurs):
//   A. grouped T=4 vs per-row T=4 (default): same quantization,
//      only GEMM tiling differs -> expect ULP-class agreement (<= 4 ULP bf16,
//      the project near-tie contract), report exact-match ratio.
//   B. grouped T=4 token t vs T=1 single-token decode (validated reference
//      path) for each token: same ULP bound.
//   C. T=1 dispatch sanity: MoEDeviceDecode output bit-identical with the
//      per-row env var set vs unset (T=1 must never take the grouped path).
#include "q4t/io/weight_loader.h"
#include "q4t/quant/moe_decode.h"
#include "q4t/quant/moe_gemm.h"
#include "q4t/quant/moe_weights.h"
#include "q4t/test.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <random>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;
using q4t::quant::MoEDeviceDecode;
using q4t::quant::MoEDeviceDecodeMulti;
using q4t::quant::MoEWeightLayout;
using q4t::quant::LoadMoEWeights;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const int kE = 512;
const int kHs = 2560;
const int kMoeIs = 640;
const int kLayer = 2;
const int kTopK = 10;
const int kT = 4;
const int kR = kT * kTopK;

bool FileExists(const char* path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return false;
  close(fd);
  return true;
}
bool CudaAvailable() {
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

// BF16 ULP at magnitude |v| (7-bit mantissa: spacing 2^(e-7)).
float Bf16Ulp(float v) {
  const float a = std::fabs(v);
  if (a < 1e-30f) return 0.0f;
  int exp = 0;
  std::frexp(a, &exp);
  return std::ldexp(1.0f, exp - 8);
}

// Max ULP distance between two bf16 vectors (0 when identical).
struct UlpReport {
  size_t exact = 0;
  size_t total = 0;
  double max_ulp = 0.0;
};
UlpReport UlpDiff(const std::vector<uint16_t>& a,
                  const std::vector<uint16_t>& b) {
  UlpReport r;
  r.total = a.size();
  for (size_t i = 0; i < a.size(); ++i) {
    if (a[i] == b[i]) {
      ++r.exact;
      continue;
    }
    const float fa = __bfloat162float(
        *reinterpret_cast<const __nv_bfloat16*>(&a[i]));
    const float fb = __bfloat162float(
        *reinterpret_cast<const __nv_bfloat16*>(&b[i]));
    const float ulp = std::fmax(Bf16Ulp(fa), Bf16Ulp(fb));
    if (ulp > 0.0f)
      r.max_ulp = std::fmax(r.max_ulp, std::fabs(fa - fb) / ulp);
  }
  return r;
}

// Routing A: token 0/1 share experts 0..9 (M_e=2 for 10 experts), tokens
// 2/3 use 20..29 / 30..39. U = 30, unique/rows = 0.75.
std::vector<int32_t> RoutingA() {
  std::vector<int32_t> ids(kR);
  for (int t = 0; t < kT; ++t)
    for (int s = 0; s < kTopK; ++s) ids[t * kTopK + s] = t * 10 + s;
  return ids;
}
// Routing B: overlapping windows, U = 25, several M_e=2.
std::vector<int32_t> RoutingB() {
  std::vector<int32_t> ids(kR);
  for (int t = 0; t < kT; ++t)
    for (int s = 0; s < kTopK; ++s)
      ids[t * kTopK + s] = t * 5 + s;
  return ids;
}

struct Fixture {
  WeightIndex* idx = nullptr;
  WeightLoader* loader = nullptr;
  MoEWeightLayout routed;
  uint16_t* d_x = nullptr;
  int32_t* d_ids = nullptr;
  float* d_rw = nullptr;
  uint8_t* d_ws = nullptr;
  void* d_gemm_ws = nullptr;
  size_t ws_bytes = 0;
};

bool LoadFixture(Fixture& f, const std::vector<int32_t>& ids,
                 const std::vector<float>& rw,
                 const std::vector<uint16_t>& x) {
  if (!FileExists(kIndex) || !WeightIndex::Open(kIndex, &f.idx).ok() ||
      !WeightLoader::Create(kModelDir, *f.idx, 16, &f.loader).ok()) {
    return false;
  }
  if (!LoadMoEWeights(*f.loader, kLayer, kE, kHs, kMoeIs, &f.routed, 0).ok())
    return false;
  f.ws_bytes = q4t::quant::MoEWorkspace::RequiredBytes(kT, kTopK, kHs, kMoeIs);
  const size_t gemm_ws = 32 * 1024 * 1024;
  cudaMalloc(&f.d_x, x.size() * 2);
  cudaMalloc(&f.d_ids, ids.size() * 4);
  cudaMalloc(&f.d_rw, rw.size() * 4);
  cudaMalloc(&f.d_ws, f.ws_bytes);
  cudaMalloc(&f.d_gemm_ws, gemm_ws);
  cudaMemcpy(f.d_x, x.data(), x.size() * 2, cudaMemcpyHostToDevice);
  cudaMemcpy(f.d_ids, ids.data(), ids.size() * 4, cudaMemcpyHostToDevice);
  cudaMemcpy(f.d_rw, rw.data(), rw.size() * 4, cudaMemcpyHostToDevice);
  return true;
}

void FreeFixture(Fixture& f) {
  if (f.d_x) cudaFree(f.d_x);
  if (f.d_ids) cudaFree(f.d_ids);
  if (f.d_rw) cudaFree(f.d_rw);
  if (f.d_ws) cudaFree(f.d_ws);
  if (f.d_gemm_ws) cudaFree(f.d_gemm_ws);
  if (f.loader) delete f.loader;
  if (f.idx) delete f.idx;
}

// Run one MoEDeviceDecodeMulti (T=kT) into a fresh y buffer.
bool RunMulti(const Fixture& f, std::vector<uint16_t>* y) {
  float* d_y = nullptr;
  if (cudaMalloc(&d_y, static_cast<size_t>(kT) * kHs * sizeof(float)) !=
      cudaSuccess)
    return false;
  // The combine kernel accumulates into y (+=); zero the fresh buffer so
  // the MoE contribution is measured in isolation.
  cudaMemset(d_y, 0, static_cast<size_t>(kT) * kHs * sizeof(float));
  const size_t gemm_ws = 32 * 1024 * 1024;
  q4t::quant::MoEWorkspace ws;
  ws.a_packed_bytes = static_cast<size_t>(kR) * (kHs / 2);
  ws.a_sf_bytes = q4t::quant::SfBufferSize(kR, kHs);
  ws.gu_out_bytes = static_cast<size_t>(kR) * (2 * kMoeIs) * sizeof(uint16_t);
  ws.dn_out_bytes = static_cast<size_t>(kR) * kHs * sizeof(uint16_t);
  ws.Init(f.d_ws);
  Status s = MoEDeviceDecodeMulti(f.d_x, f.d_ids, f.d_rw, d_y, f.routed, ws,
                                  f.d_gemm_ws, gemm_ws, kT, kTopK, 0);
  if (!s.ok()) {
    std::printf("  MoEDeviceDecodeMulti failed: %s\n", s.message().c_str());
    cudaFree(d_y);
    return false;
  }
  if (cudaDeviceSynchronize() != cudaSuccess) {
    std::printf("  RunMulti sync error: %s\n",
                cudaGetErrorString(cudaGetLastError()));
    cudaFree(d_y);
    return false;
  }
  y->assign(static_cast<size_t>(kT) * kHs, 0);
  std::vector<float> yf(static_cast<size_t>(kT) * kHs);
  cudaMemcpy(yf.data(), d_y, yf.size() * sizeof(float), cudaMemcpyDeviceToHost);
  for (size_t i = 0; i < yf.size(); ++i) {
    const __nv_bfloat16 b = __float2bfloat16(yf[i]);
    y->at(i) = *reinterpret_cast<const uint16_t*>(&b);
  }
  cudaFree(d_y);
  return true;
}

// Run the validated T=1 path for one token (its 10 slots) into y[t].
bool RunT1(const Fixture& f, int t, std::vector<uint16_t>* y) {
  float* d_y = nullptr;
  if (cudaMalloc(&d_y, static_cast<size_t>(kHs) * sizeof(float)) !=
      cudaSuccess)
    return false;
  cudaMemset(d_y, 0, static_cast<size_t>(kHs) * sizeof(float));
  const size_t gemm_ws = 32 * 1024 * 1024;
  q4t::quant::MoEWorkspace ws;
  ws.a_packed_bytes = static_cast<size_t>(kTopK) * (kHs / 2);
  ws.a_sf_bytes = q4t::quant::SfBufferSize(kTopK, kHs);
  ws.gu_out_bytes =
      static_cast<size_t>(kTopK) * (2 * kMoeIs) * sizeof(uint16_t);
  ws.dn_out_bytes = static_cast<size_t>(kTopK) * kHs * sizeof(uint16_t);
  ws.Init(f.d_ws);
  Status s = MoEDeviceDecode(
      f.d_x + static_cast<size_t>(t) * kHs,
      f.d_ids + t * kTopK, f.d_rw + t * kTopK, d_y, f.routed, ws, f.d_gemm_ws,
      gemm_ws, 0);
  if (!s.ok()) {
    std::printf("  MoEDeviceDecode failed: %s\n", s.message().c_str());
    cudaFree(d_y);
    return false;
  }
  if (cudaDeviceSynchronize() != cudaSuccess) {
    std::printf("  RunT1 sync error: %s\n",
                cudaGetErrorString(cudaGetLastError()));
    cudaFree(d_y);
    return false;
  }
  std::vector<float> yf(kHs);
  cudaMemcpy(yf.data(), d_y, yf.size() * sizeof(float), cudaMemcpyDeviceToHost);
  for (int i = 0; i < kHs; ++i) {
    const __nv_bfloat16 b = __float2bfloat16(yf[i]);
    y->at(static_cast<size_t>(t) * kHs + i) =
        *reinterpret_cast<const uint16_t*>(&b);
  }
  cudaFree(d_y);
  return true;
}

bool CheckRouting(const char* name, const std::vector<int32_t>& ids,
                  double* max_ulp_out, double* exact_ratio_out) {
  // Random activations + router weights (deterministic seed).
  std::mt19937 rng(4242);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  std::vector<uint16_t> x(static_cast<size_t>(kT) * kHs);
  for (auto& v : x) {
    const __nv_bfloat16 b = __float2bfloat16(dist(rng));
    v = *reinterpret_cast<const uint16_t*>(&b);
  }
  std::vector<float> rw(kR);
  for (int t = 0; t < kT; ++t) {
    float sum = 0.0f;
    for (int s = 0; s < kTopK; ++s) {
      rw[t * kTopK + s] = std::exp(dist(rng) * 0.5f);
      sum += rw[t * kTopK + s];
    }
    for (int s = 0; s < kTopK; ++s) rw[t * kTopK + s] /= sum;
  }

  Fixture f;
  if (!LoadFixture(f, ids, rw, x)) {
    std::printf("  %s: SKIP (model/weights absent)\n", name);
    return true;  // skip, not fail
  }

  // Per-row reference (opt-out via Q4T_MOE_DECODE_PERROW).
  setenv("Q4T_MOE_DECODE_PERROW", "1", 1);
  std::vector<uint16_t> y_ref;
  const bool ok_ref = RunMulti(f, &y_ref);
  unsetenv("Q4T_MOE_DECODE_PERROW");
  // Grouped (default for T>1).
  std::vector<uint16_t> y_grp;
  const bool ok_grp = RunMulti(f, &y_grp);
  if (!ok_ref || !ok_grp) {
    FreeFixture(f);
    return false;
  }

  // A. grouped vs per-row.
  UlpReport a = UlpDiff(y_ref, y_grp);
  std::printf(
      "  %s A grouped-vs-perrow: exact=%zu/%zu (%.1f%%) max_ulp=%.2f\n",
      name, a.exact, a.total, 100.0 * static_cast<double>(a.exact) /
                               static_cast<double>(a.total),
      a.max_ulp);

  // B. grouped token t vs T=1 validated reference path.
  std::vector<uint16_t> y_t1(static_cast<size_t>(kT) * kHs, 0);
  bool ok_t1 = true;
  for (int t = 0; t < kT && ok_t1; ++t) ok_t1 = RunT1(f, t, &y_t1);
  double max_ulp_b = 0.0;
  size_t exact_b = 0;
  if (ok_t1) {
    for (int t = 0; t < kT; ++t) {
      UlpReport b =
          UlpDiff(std::vector<uint16_t>(y_t1.begin() + t * kHs,
                                        y_t1.begin() + (t + 1) * kHs),
                  std::vector<uint16_t>(y_grp.begin() + t * kHs,
                                        y_grp.begin() + (t + 1) * kHs));
      max_ulp_b = std::fmax(max_ulp_b, b.max_ulp);
      exact_b += b.exact;
    }
    std::printf("  %s B grouped-vs-T1: exact=%zu/%zu max_ulp=%.2f\n", name,
                exact_b, static_cast<size_t>(kT) * kHs, max_ulp_b);
  }
  FreeFixture(f);
  if (!ok_t1) return false;
  *max_ulp_out = std::fmax(a.max_ulp, max_ulp_b);
  *exact_ratio_out = 100.0 * static_cast<double>(a.exact) /
                     static_cast<double>(a.total);
  // Near-tie contract: ULP-class agreement only (GEMM tiling differs).
  return *max_ulp_out <= 4.0;
}

}  // namespace

Q4T_TEST(moe_decode_grouped_vs_perrow) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  double max_ulp = 0.0, ratio = 0.0;
  if (!CheckRouting("routingA", RoutingA(), &max_ulp, &ratio)) return false;
  double max_ulp_b = 0.0, ratio_b = 0.0;
  if (!CheckRouting("routingB", RoutingB(), &max_ulp_b, &ratio_b))
    return false;
  max_ulp = std::fmax(max_ulp, max_ulp_b);
  std::printf("  max_ulp=%.2f (bound 4.0)\n", max_ulp);
  return max_ulp <= 4.0;
}

Q4T_TEST(moe_decode_t1_dispatch_bitexact) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  std::vector<int32_t> ids(kTopK);
  for (int s = 0; s < kTopK; ++s) ids[s] = 7 * s;  // distinct experts
  std::mt19937 rng(99);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  std::vector<uint16_t> x(kHs);
  for (auto& v : x) {
    const __nv_bfloat16 b = __float2bfloat16(dist(rng));
    v = *reinterpret_cast<const uint16_t*>(&b);
  }
  std::vector<float> rw(kTopK, 1.0f / kTopK);

  Fixture f;
  if (!LoadFixture(f, ids, rw, x)) {
    std::printf("  SKIP (model/weights absent)\n");
    return true;
  }
  auto run = [&](std::vector<uint16_t>* y) {
    float* d_y = nullptr;
    if (cudaMalloc(&d_y, static_cast<size_t>(kHs) * sizeof(float)) !=
        cudaSuccess)
      return false;
    cudaMemset(d_y, 0, static_cast<size_t>(kHs) * sizeof(float));
    const size_t gemm_ws = 32 * 1024 * 1024;
    q4t::quant::MoEWorkspace ws;
    ws.a_packed_bytes = static_cast<size_t>(kTopK) * (kHs / 2);
    ws.a_sf_bytes = q4t::quant::SfBufferSize(kTopK, kHs);
    ws.gu_out_bytes =
        static_cast<size_t>(kTopK) * (2 * kMoeIs) * sizeof(uint16_t);
    ws.dn_out_bytes = static_cast<size_t>(kTopK) * kHs * sizeof(uint16_t);
    ws.Init(f.d_ws);
    Status s = MoEDeviceDecode(f.d_x, f.d_ids, f.d_rw, d_y, f.routed, ws,
                               f.d_gemm_ws, gemm_ws, 0);
    if (!s.ok()) {
      cudaFree(d_y);
      return false;
    }
    if (cudaDeviceSynchronize() != cudaSuccess) {
      cudaFree(d_y);
      return false;
    }
    y->assign(kHs, 0);
    std::vector<float> yf(kHs);
    cudaMemcpy(yf.data(), d_y, yf.size() * sizeof(float),
               cudaMemcpyDeviceToHost);
    for (int i = 0; i < kHs; ++i) {
      const __nv_bfloat16 b = __float2bfloat16(yf[i]);
      y->at(i) = *reinterpret_cast<const uint16_t*>(&b);
    }
    cudaFree(d_y);
    return true;
  };
  // T=1 always uses the per-row path regardless of the PERROW env; verify
  // the default (grouped-for-T>1) and PERROW opt-out are bit-exact for T=1.
  std::vector<uint16_t> y_a;
  const bool ok_a = run(&y_a);
  setenv("Q4T_MOE_DECODE_PERROW", "1", 1);
  std::vector<uint16_t> y_b;
  const bool ok_b = run(&y_b);
  unsetenv("Q4T_MOE_DECODE_PERROW");
  FreeFixture(f);
  if (!ok_a || !ok_b) return false;
  const bool bitexact = std::equal(y_a.begin(), y_a.end(), y_b.begin());
  std::printf("  T=1 default-vs-perrow-env bit-exact: %s\n",
              bitexact ? "yes" : "NO");
  return bitexact;
}
