// Test for the Hyper-Connection (GatedResidual) backbone, against the real
// checkpoint. Loads the layer-0 attn_hyper_connection weights, runs mix and
// combine on a random [T, hc*hs] input, and compares against a full CPU
// reference that mirrors the grouped RMSNorm, low-rank gate, and inject math.
// Skipped (reported as pass) when CUDA or the real model is absent.
#include "q4t/io/weight_loader.h"
#include "q4t/model/hyperconnection.h"
#include "q4t/test.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
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
using q4t::model::HyperConnectionCombine;
using q4t::model::HyperConnectionMix;
using q4t::model::HyperConnectionWeights;
using q4t::model::LoadHyperConnection;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const int kHc = 4;
const int kHs = 2560;
const int kLowrank = 320;
const int kHcDim = kHc * kHs;
const float kEps = 1e-6f;
const int kT = 3;
const std::string kPrefix =
    "model.language_model.layers.0.attn_hyper_connection";

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

float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}
uint16_t FloatToBf16(float f) {
  const __nv_bfloat16 b = __float2bfloat16_rn(f);
  return *reinterpret_cast<const uint16_t*>(&b);
}

// GroupedGemmaRMSNorm: per-branch RMSNorm, scale by (1 + weight).
std::vector<float> CpuGroupedRmsNorm(const std::vector<float>& x,
                                     const std::vector<float>& weight, int T,
                                     int hc, int hs, float eps) {
  std::vector<float> out(static_cast<size_t>(T) * hc * hs);
  for (int t = 0; t < T; ++t) {
    for (int b = 0; b < hc; ++b) {
      const float* g = &x[static_cast<size_t>(t) * hc * hs + b * hs];
      float sumsq = 0.0f;
      for (int c = 0; c < hs; ++c) sumsq += g[c] * g[c];
      const float rs = 1.0f / std::sqrt(sumsq / hs + eps);
      float* og = &out[static_cast<size_t>(t) * hc * hs + b * hs];
      for (int c = 0; c < hs; ++c)
        og[c] = g[c] * rs * (1.0f + weight[static_cast<size_t>(b) * hs + c]);
    }
  }
  return out;
}

// y[t, n] = sum_k x[t, k] * W[n, k]  (W row-major [N, K]).
std::vector<float> CpuLinear(const std::vector<float>& x,
                             const std::vector<float>& W, int T, int N,
                             int K) {
  std::vector<float> y(static_cast<size_t>(T) * N);
  for (int t = 0; t < T; ++t) {
    for (int n = 0; n < N; ++n) {
      float acc = 0.0f;
      const float* wrow = &W[static_cast<size_t>(n) * K];
      const float* xrow = &x[static_cast<size_t>(t) * K];
      for (int k = 0; k < K; ++k) acc += xrow[k] * wrow[k];
      y[static_cast<size_t>(t) * N + n] = acc;
    }
  }
  return y;
}

float Silu(float v) { return v / (1.0f + std::exp(-v)); }
float Sigmoid(float v) { return 1.0f / (1.0f + std::exp(-v)); }

// Round a float to BF16 precision and back (simulates the device storing
// intermediates in BF16).
float Bf16Round(float f) { return Bf16ToFloat(FloatToBf16(f)); }

// L2 relative error: ||a-b|| / (||b|| + eps). Robust to near-zero elements
// (unlike elementwise max relative error, which blows up on ~0 values).
float L2RelErr(const std::vector<float>& a, const std::vector<float>& b) {
  double num = 0.0, den = 0.0;
  for (size_t i = 0; i < a.size(); ++i) {
    const double d = double(a[i]) - double(b[i]);
    num += d * d;
    den += double(b[i]) * double(b[i]);
  }
  return float(std::sqrt(num) / (std::sqrt(den) + 1e-6));
}

std::vector<uint16_t> ToBf16(const std::vector<float>& v) {
  std::vector<uint16_t> out(v.size());
  for (size_t i = 0; i < v.size(); ++i) out[i] = FloatToBf16(v[i]);
  return out;
}
std::vector<float> FromBf16(const std::vector<uint16_t>& v) {
  std::vector<float> out(v.size());
  for (size_t i = 0; i < v.size(); ++i) out[i] = Bf16ToFloat(v[i]);
  return out;
}

}  // namespace

Q4T_TEST(hyperconnection_mix_combine) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex)) {
    std::printf("  (skipped: model index not found)\n");
    return true;
  }

  WeightIndex* index = nullptr;
  Status s = WeightIndex::Open(kIndex, &index);
  if (!s.ok()) {
    std::printf("  index open failed: %s\n", s.message().c_str());
    return false;
  }
  WeightLoader* loader = nullptr;
  s = WeightLoader::Create(kModelDir, *index, 8, &loader);
  if (!s.ok()) {
    std::printf("  loader create failed: %s\n", s.message().c_str());
    return false;
  }

  // Load device weights.
  HyperConnectionWeights w;
  s = LoadHyperConnection(*loader, kPrefix, kHc, kHs, kLowrank, kEps, true, &w,
                          nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  // Host copies of the same weights for the CPU reference.
  auto read_host = [&](const std::string& name, size_t n) {
    std::vector<uint16_t> raw(n);
    Status ls = loader->ReadTensor(name, raw.data());
    if (!ls.ok()) return std::vector<float>();
    return FromBf16(raw);
  };
  std::vector<float> hc_norm = read_host(kPrefix + ".hc_norm.weight", kHcDim);
  std::vector<float> mix_down =
      read_host(kPrefix + ".input_mix_weight_down.weight",
                static_cast<size_t>(kLowrank) * kHcDim);
  std::vector<float> mix_up =
      read_host(kPrefix + ".input_mix_weight_up.weight",
                static_cast<size_t>(kHcDim) * kLowrank);
  std::vector<float> block_inject =
      read_host(kPrefix + ".block_inject_weight.weight",
                static_cast<size_t>(kHc) * kHcDim);
  if (hc_norm.empty() || mix_down.empty() || mix_up.empty() ||
      block_inject.empty()) {
    std::printf("  host weight read failed\n");
    w.Free();
    return false;
  }

  // Random hyper_input [T, hc*hs].
  std::mt19937 rng(12345);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  std::vector<float> hyper_in(static_cast<size_t>(kT) * kHcDim);
  for (auto& v : hyper_in) v = dist(rng);
  std::vector<uint16_t> hyper_in_bf = ToBf16(hyper_in);

  // Device buffers.
  uint16_t* d_in = nullptr;
  uint16_t* d_mixed = nullptr;
  uint16_t* d_normed = nullptr;
  uint16_t* d_block = nullptr;
  uint16_t* d_out = nullptr;
  void* d_ws = nullptr;
  const size_t ws_bytes = 32u * 1024u * 1024u;
  if (cudaMalloc(&d_in, hyper_in_bf.size() * sizeof(uint16_t)) !=
          cudaSuccess ||
      cudaMalloc(&d_mixed, static_cast<size_t>(kT) * kHs * sizeof(uint16_t)) !=
          cudaSuccess ||
      cudaMalloc(&d_normed, hyper_in_bf.size() * sizeof(uint16_t)) !=
          cudaSuccess ||
      cudaMalloc(&d_block, static_cast<size_t>(kT) * kHs * sizeof(uint16_t)) !=
          cudaSuccess ||
      cudaMalloc(&d_out, hyper_in_bf.size() * sizeof(uint16_t)) !=
          cudaSuccess ||
      cudaMalloc(&d_ws, ws_bytes) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    w.Free();
    return false;
  }
  cudaMemcpy(d_in, hyper_in_bf.data(),
             hyper_in_bf.size() * sizeof(uint16_t), cudaMemcpyHostToDevice);

  // ---- mix ----
  s = HyperConnectionMix(w, d_in, d_mixed, d_normed, kT, d_ws, ws_bytes,
                         nullptr);
  if (!s.ok()) {
    std::printf("  mix failed: %s\n", s.message().c_str());
    w.Free();
    return false;
  }
  // CPU reference for mix. Intermediates are rounded to BF16 to mirror the
  // device path (normed/down/up are all stored as BF16 on GPU).
  std::vector<float> normed_ref =
      CpuGroupedRmsNorm(hyper_in, hc_norm, kT, kHc, kHs, kEps);
  for (auto& v : normed_ref) v = Bf16Round(v);
  std::vector<float> down_ref =
      CpuLinear(normed_ref, mix_down, kT, kLowrank, kHcDim);
  for (auto& v : down_ref) v = Bf16Round(Silu(v / kHc));  // silu(x/hc)
  std::vector<float> up_ref = CpuLinear(down_ref, mix_up, kT, kHcDim, kLowrank);
  for (auto& v : up_ref) v = Bf16Round(v);
  std::vector<float> mixed_ref(static_cast<size_t>(kT) * kHs);
  for (int t = 0; t < kT; ++t) {
    for (int c = 0; c < kHs; ++c) {
      float acc = 0.0f;
      for (int b = 0; b < kHc; ++b) {
        const int idx = static_cast<size_t>(t) * kHcDim + b * kHs + c;
        acc += Sigmoid(up_ref[idx]) * normed_ref[idx];
      }
      mixed_ref[static_cast<size_t>(t) * kHs + c] = acc / kHc;
    }
  }
  std::vector<uint16_t> mixed_dev(static_cast<size_t>(kT) * kHs);
  cudaMemcpy(mixed_dev.data(), d_mixed,
             mixed_dev.size() * sizeof(uint16_t), cudaMemcpyDeviceToHost);
  const float mix_err = L2RelErr(FromBf16(mixed_dev), mixed_ref);
  std::printf("  mix l2_rel_err = %.3e\n", mix_err);

  // ---- combine ----
  std::vector<float> block_in(static_cast<size_t>(kT) * kHs);
  for (auto& v : block_in) v = dist(rng);
  std::vector<uint16_t> block_bf = ToBf16(block_in);
  cudaMemcpy(d_block, block_bf.data(), block_bf.size() * sizeof(uint16_t),
             cudaMemcpyHostToDevice);
  // Device normed from the mix step (already in d_normed).
  s = HyperConnectionCombine(w, d_block, d_in, d_normed, d_out, kT, d_ws,
                             ws_bytes, nullptr);
  if (!s.ok()) {
    std::printf("  combine failed: %s\n", s.message().c_str());
    w.Free();
    return false;
  }
  // CPU reference for combine (using the same normed_ref, already BF16-rounded
  // to match the device). R uses the BF16-stored hyper_input.
  std::vector<float> inject_raw =
      CpuLinear(normed_ref, block_inject, kT, kHc, kHcDim);
  for (auto& v : inject_raw) v = Bf16Round(v);
  std::vector<float> r_ref = hyper_in;
  for (auto& v : r_ref) v = Bf16Round(v);
  std::vector<float> out_ref(static_cast<size_t>(kT) * kHcDim);
  for (int t = 0; t < kT; ++t) {
    for (int b = 0; b < kHc; ++b) {
      const float inj = 2.0f * Sigmoid(
          inject_raw[static_cast<size_t>(t) * kHc + b] / kHc);
      for (int c = 0; c < kHs; ++c) {
        const size_t idx = static_cast<size_t>(t) * kHcDim + b * kHs + c;
        out_ref[idx] = r_ref[idx] +
                       block_in[static_cast<size_t>(t) * kHs + c] * inj;
      }
    }
  }
  std::vector<uint16_t> out_dev(static_cast<size_t>(kT) * kHcDim);
  cudaMemcpy(out_dev.data(), d_out, out_dev.size() * sizeof(uint16_t),
             cudaMemcpyDeviceToHost);
  const float combine_err = L2RelErr(FromBf16(out_dev), out_ref);
  std::printf("  combine l2_rel_err = %.3e\n", combine_err);

  // Cleanup.
  cudaFree(d_in);
  cudaFree(d_mixed);
  cudaFree(d_normed);
  cudaFree(d_block);
  cudaFree(d_out);
  cudaFree(d_ws);
  w.Free();

  // BF16 intermediates (normed/down/up/inject) limit precision; the CPU
  // reference rounds them to BF16 to match. L2 relative error should be at the
  // ~BF16 epsilon scale (~0.4%) since GEMM summing averages out the noise.
  Q4T_CHECK(mix_err < 2e-2f);
  Q4T_CHECK(combine_err < 2e-2f);
  return true;
}
