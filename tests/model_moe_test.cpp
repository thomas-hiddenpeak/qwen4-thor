// Test for the complete MoE MLP (model/moe.h), against the real checkpoint.
// Loads the layer-2 routed NVFP4 experts + BF16 router/shared-expert weights,
// runs MoEForward on random [T, hs] input, and compares against a full CPU
// reference that mirrors the router top-k (softmax over the selected k), the
// NVFP4 routed experts (dequant + SwiGLU), the BF16 shared expert, and the
// gated combine. Reported as SKIP when CUDA or the model is absent.
#include "q4t/io/weight_loader.h"
#include "q4t/model/moe.h"
#include "q4t/quant/format.h"
#include "q4t/quant/moe_gemm.h"
#include "q4t/quant/moe_weights.h"
#include "q4t/quant/swizzle.h"
#include "q4t/test.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <map>
#include <random>
#include <string>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {

using q4t::Status;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;
using q4t::model::LoadMoEExtra;
using q4t::model::MoEExtraWeights;
using q4t::model::MoEForward;
using q4t::quant::E2m1ToFloat;
using q4t::quant::E4m3ToFloat;
using q4t::quant::FloatToE2m1Code;
using q4t::quant::FloatToE4m3;
using q4t::quant::LoadMoEWeights;
using q4t::quant::MoEWeightLayout;
using q4t::quant::SfNumGtiles;
using q4t::quant::SfOffset;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const int kE = 512;
const int kHs = 2560;
const int kMoeIs = 640;
const int kSharedIs = 640;
const int kLayer = 2;
const int kTopK = 10;
const int kT = 2;
const std::string kMlpPrefix =
    "model.language_model.layers.2.mlp";

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
float Bf16Round(float f) { return Bf16ToFloat(FloatToBf16(f)); }

// Host NVFP4 quantization of a [rows, K] float buffer (activation convention).
struct HostFp4 {
  std::vector<uint8_t> packed;
  std::vector<uint8_t> sf;  // row-major [rows, K/16]
};
HostFp4 HostQuantFp4(const std::vector<float>& v, int rows, int K,
                     float global_scale) {
  HostFp4 h;
  const int groups = K / 16;
  h.packed.assign(static_cast<size_t>(rows) * (K / 2), 0);
  h.sf.assign(static_cast<size_t>(rows) * groups, 0);
  for (int r = 0; r < rows; ++r) {
    for (int g = 0; g < groups; ++g) {
      float gmax = 0.0f;
      for (int j = 0; j < 16; ++j)
        gmax = std::fmax(gmax,
                         std::fabs(v[static_cast<size_t>(r) * K + g * 16 + j]));
      const float block_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
      const uint8_t sf = FloatToE4m3(block_scale / global_scale);
      const float eff = E4m3ToFloat(sf) * global_scale;
      const float inv = eff > 0.0f ? 1.0f / eff : 0.0f;
      h.sf[static_cast<size_t>(r) * groups + g] = sf;
      for (int j = 0; j < 8; ++j) {
        const int c0 = FloatToE2m1Code(
            v[static_cast<size_t>(r) * K + g * 16 + 2 * j] * inv);
        const int c1 = FloatToE2m1Code(
            v[static_cast<size_t>(r) * K + g * 16 + 2 * j + 1] * inv);
        h.packed[static_cast<size_t>(r) * (K / 2) + g * 8 + j] =
            static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
      }
    }
  }
  return h;
}
std::vector<float> HostDequantFp4(const HostFp4& h, int rows, int K,
                                  float global_scale) {
  const int groups = K / 16;
  std::vector<float> out(static_cast<size_t>(rows) * K, 0.0f);
  for (int r = 0; r < rows; ++r) {
    for (int g = 0; g < groups; ++g) {
      const float gs =
          E4m3ToFloat(h.sf[static_cast<size_t>(r) * groups + g]) * global_scale;
      const uint8_t* p = &h.packed[static_cast<size_t>(r) * (K / 2) + g * 8];
      for (int j = 0; j < 8; ++j) {
        out[static_cast<size_t>(r) * K + g * 16 + 2 * j] =
            E2m1ToFloat(p[j] & 0xF) * gs;
        out[static_cast<size_t>(r) * K + g * 16 + 2 * j + 1] =
            E2m1ToFloat((p[j] >> 4) & 0xF) * gs;
      }
    }
  }
  return out;
}
// Dequant one weight row (swizzled SF) to float [K].
std::vector<float> DequantWeightRow(const uint8_t* packed, const uint8_t* sf,
                                    int row, int K, int num_tiles,
                                    float global_scale) {
  const int groups = K / 16;
  std::vector<float> out(K);
  for (int g = 0; g < groups; ++g) {
    const float gs = E4m3ToFloat(sf[SfOffset(row, g, num_tiles)]) * global_scale;
    const uint8_t* p = packed + (static_cast<size_t>(row) * (K / 2) + g * 8);
    for (int j = 0; j < 8; ++j) {
      out[g * 16 + 2 * j] = E2m1ToFloat(p[j] & 0xF) * gs;
      out[g * 16 + 2 * j + 1] = E2m1ToFloat((p[j] >> 4) & 0xF) * gs;
    }
  }
  return out;
}

// Dequant an entire expert's gate/up [2*moe_is, hs] and down [hs, moe_is]
// weights to float (cached per unique expert to bound CPU cost).
struct ExpertDequant {
  std::vector<float> gu;  // [2*moe_is, hs]
  std::vector<float> dn;  // [hs, moe_is]
};

}  // namespace

Q4T_TEST(moe_forward_end_to_end) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  WeightIndex* idx = nullptr;
  WeightLoader* loader = nullptr;
  if (!FileExists(kIndex) || !WeightIndex::Open(kIndex, &idx).ok() ||
      !WeightLoader::Create(kModelDir, *idx, 16, &loader).ok()) {
    Q4T_SKIP("(skipped: real model not present)");
  }

  // Load routed NVFP4 experts + BF16 router/shared weights.
  MoEWeightLayout routed;
  Status s = LoadMoEWeights(*loader, kLayer, kE, kHs, kMoeIs, &routed, 0);
  if (!s.ok()) {
    std::printf("  routed load failed: %s\n", s.message().c_str());
    return false;
  }
  MoEExtraWeights extra;
  s = LoadMoEExtra(*loader, kMlpPrefix, kE, kHs, kSharedIs, &extra, 0);
  if (!s.ok()) {
    std::printf("  extra load failed: %s\n", s.message().c_str());
    return false;
  }

  // Host copies of the BF16 router/shared weights for the CPU reference.
  auto read_host = [&](const std::string& name, size_t n) {
    std::vector<uint16_t> raw(n);
    Status ls = loader->ReadTensor(name, raw.data());
    if (!ls.ok()) return std::vector<float>();
    std::vector<float> f(n);
    for (size_t i = 0; i < n; ++i) f[i] = Bf16ToFloat(raw[i]);
    return f;
  };
  std::vector<float> gate_h =
      read_host(kMlpPrefix + ".gate.weight", static_cast<size_t>(kE) * kHs);
  // Shared gate/up: read gate_proj and up_proj separately and concatenate
  // (gate rows first, then up rows) to match the device shared_gu layout.
  // Reading a single tensor into a 2*shared_is*hs buffer would leave the up
  // half uninitialized (ReadTensor only writes the tensor's actual size).
  std::vector<float> shared_gate_proj_h = read_host(
      kMlpPrefix + ".shared_expert.gate_proj.weight",
      static_cast<size_t>(kSharedIs) * kHs);
  std::vector<float> shared_up_proj_h = read_host(
      kMlpPrefix + ".shared_expert.up_proj.weight",
      static_cast<size_t>(kSharedIs) * kHs);
  std::vector<float> shared_gu_h(static_cast<size_t>(2 * kSharedIs) * kHs);
  std::copy(shared_gate_proj_h.begin(), shared_gate_proj_h.end(),
            shared_gu_h.begin());
  std::copy(shared_up_proj_h.begin(), shared_up_proj_h.end(),
            shared_gu_h.begin() + static_cast<size_t>(kSharedIs) * kHs);
  std::vector<float> shared_down_h =
      read_host(kMlpPrefix + ".shared_expert.down_proj.weight",
                static_cast<size_t>(kHs) * kSharedIs);
  std::vector<float> shared_gate_scalar_h =
      read_host(kMlpPrefix + ".shared_expert_gate.weight", kHs);
  if (gate_h.empty() || shared_gate_proj_h.empty() || shared_up_proj_h.empty() ||
      shared_down_h.empty() || shared_gate_scalar_h.empty()) {
    std::printf("  host weight read failed\n");
    return false;
  }

  // Random BF16 activations [T, hs].
  std::mt19937 rng(777);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  std::vector<float> x_f(static_cast<size_t>(kT) * kHs);
  for (auto& v : x_f) v = dist(rng);
  std::vector<uint16_t> x_bf16(x_f.size());
  for (size_t i = 0; i < x_f.size(); ++i) {
    const __nv_bfloat16 b = __float2bfloat16(x_f[i]);
    x_bf16[i] = *reinterpret_cast<const uint16_t*>(&b);
    x_f[i] = __bfloat162float(b);  // what the GPU actually sees
  }

  // --- Device setup ---
  uint16_t* d_x = nullptr;
  uint16_t* d_y = nullptr;
  cudaMalloc(&d_x, x_bf16.size() * 2);
  cudaMalloc(&d_y, static_cast<size_t>(kT) * kHs * 2);
  cudaMemcpy(d_x, x_bf16.data(), x_bf16.size() * 2, cudaMemcpyHostToDevice);

  const size_t ws_bytes = q4t::model::MoEForwardWorkspaceBytes(
      kT, kTopK, kHs, kMoeIs, kSharedIs, kE);
  uint8_t* d_ws = nullptr;
  cudaMalloc(&d_ws, ws_bytes);
  const size_t gemm_ws = 32 * 1024 * 1024;
  void* d_gemm_ws = nullptr;
  cudaMalloc(&d_gemm_ws, gemm_ws);

  s = MoEForward(d_x, routed, extra, d_y, kT, kTopK, d_ws, ws_bytes,
                 d_gemm_ws, gemm_ws, 0);
  if (!s.ok()) {
    std::printf("  MoEForward failed: %s\n", s.message().c_str());
    return false;
  }
  if (cudaDeviceSynchronize() != cudaSuccess) return false;
  std::vector<uint16_t> y_bf(static_cast<size_t>(kT) * kHs);
  cudaMemcpy(y_bf.data(), d_y, y_bf.size() * 2, cudaMemcpyDeviceToHost);

  // --- CPU reference ---
  // 1. Router logits + top-k + softmax-over-topk.
  std::vector<std::vector<int32_t>> ids(kT, std::vector<int32_t>(kTopK));
  std::vector<std::vector<float>> rw(kT, std::vector<float>(kTopK));
  for (int t = 0; t < kT; ++t) {
    std::vector<float> logits(kE);
    for (int e = 0; e < kE; ++e) {
      float acc = 0.0f;
      for (int c = 0; c < kHs; ++c)
        acc += x_f[static_cast<size_t>(t) * kHs + c] *
               gate_h[static_cast<size_t>(e) * kHs + c];
      logits[e] = Bf16Round(acc);  // match CUDA's bf16-stored logits
    }
    // top-k by value.
    std::vector<int> order(kE);
    for (int e = 0; e < kE; ++e) order[e] = e;
    std::sort(order.begin(), order.end(),
              [&](int a, int b) { return logits[a] > logits[b]; });
    float maxv = logits[order[0]];
    float sum = 0.0f;
    for (int i = 0; i < kTopK; ++i)
      sum += std::exp(logits[order[i]] - maxv);
    for (int i = 0; i < kTopK; ++i) {
      ids[t][i] = order[i];
      rw[t][i] = std::exp(logits[order[i]] - maxv) / sum;
    }
  }

  // 2. Routed experts (NVFP4), caching each unique expert's dequant weights.
  const int num_gu_tiles = SfNumGtiles(kHs);
  const int num_dn_tiles = SfNumGtiles(kMoeIs);
  std::map<int, ExpertDequant> cache;
  auto get_expert = [&](int e) -> const ExpertDequant& {
    auto it = cache.find(e);
    if (it != cache.end()) return it->second;
    ExpertDequant ed;
    ed.gu.assign(static_cast<size_t>(2 * kMoeIs) * kHs, 0.0f);
    ed.dn.assign(static_cast<size_t>(kHs) * kMoeIs, 0.0f);
    std::vector<uint8_t> gu_host(static_cast<size_t>(2 * kMoeIs) * (kHs / 2));
    std::vector<uint8_t> gu_sf_host(routed.gu_sf_block());
    std::vector<uint8_t> dn_host(static_cast<size_t>(kHs) * (kMoeIs / 2));
    std::vector<uint8_t> dn_sf_host(routed.dn_sf_block());
    cudaMemcpy(gu_host.data(), routed.gu_packed_expert(e), gu_host.size(),
               cudaMemcpyDeviceToHost);
    cudaMemcpy(gu_sf_host.data(), routed.gu_sf_expert(e), gu_sf_host.size(),
               cudaMemcpyDeviceToHost);
    cudaMemcpy(dn_host.data(), routed.dn_packed_expert(e), dn_host.size(),
               cudaMemcpyDeviceToHost);
    cudaMemcpy(dn_sf_host.data(), routed.dn_sf_expert(e), dn_sf_host.size(),
               cudaMemcpyDeviceToHost);
    const float gu_ws2 = routed.gu_w_scale2_h[e];
    const float dn_ws2 = routed.dn_w_scale2_h[e];
    for (int n = 0; n < 2 * kMoeIs; ++n) {
      std::vector<float> wrow = DequantWeightRow(gu_host.data(),
                                                 gu_sf_host.data(), n, kHs,
                                                 num_gu_tiles, gu_ws2);
      for (int c = 0; c < kHs; ++c)
        ed.gu[static_cast<size_t>(n) * kHs + c] = wrow[c];
    }
    for (int c = 0; c < kHs; ++c) {
      std::vector<float> wrow = DequantWeightRow(dn_host.data(),
                                                 dn_sf_host.data(), c, kMoeIs,
                                                 num_dn_tiles, dn_ws2);
      for (int kk = 0; kk < kMoeIs; ++kk)
        ed.dn[static_cast<size_t>(c) * kMoeIs + kk] = wrow[kk];
    }
    return cache.emplace(e, std::move(ed)).first->second;
  };

  std::vector<float> routed_ref(static_cast<size_t>(kT) * kHs, 0.0f);
  for (int t = 0; t < kT; ++t) {
    for (int slot = 0; slot < kTopK; ++slot) {
      const int e = ids[t][slot];
      const float w = rw[t][slot];
      const float gu_in = routed.gu_input_scale_h[e];
      const float dn_in = routed.dn_input_scale_h[e];
      const ExpertDequant& ed = get_expert(e);
      // Activation row -> A_real.
      std::vector<float> a_row(x_f.begin() + static_cast<size_t>(t) * kHs,
                               x_f.begin() + static_cast<size_t>(t) * kHs + kHs);
      HostFp4 aq = HostQuantFp4(a_row, 1, kHs, gu_in);
      std::vector<float> a_dq = HostDequantFp4(aq, 1, kHs, gu_in);
      // gate/up.
      std::vector<float> h(2 * kMoeIs, 0.0f);
      for (int n = 0; n < 2 * kMoeIs; ++n) {
        float acc = 0.0f;
        const float* wrow = &ed.gu[static_cast<size_t>(n) * kHs];
        for (int c = 0; c < kHs; ++c) acc += a_dq[c] * wrow[c];
        h[n] = acc;
      }
      std::vector<float> inter(kMoeIs);
      for (int c = 0; c < kMoeIs; ++c) {
        const float g = h[c], u = h[kMoeIs + c];
        inter[c] = (g / (1.0f + std::exp(-g))) * u;
      }
      HostFp4 iq = HostQuantFp4(inter, 1, kMoeIs, dn_in);
      std::vector<float> i_dq = HostDequantFp4(iq, 1, kMoeIs, dn_in);
      for (int c = 0; c < kHs; ++c) {
        float acc = 0.0f;
        const float* wrow = &ed.dn[static_cast<size_t>(c) * kMoeIs];
        for (int kk = 0; kk < kMoeIs; ++kk) acc += i_dq[kk] * wrow[kk];
        routed_ref[static_cast<size_t>(t) * kHs + c] += w * acc;
      }
    }
  }

  // 3. Shared expert (BF16) + gated combine.
  std::vector<float> y_ref(static_cast<size_t>(kT) * kHs);
  for (int t = 0; t < kT; ++t) {
    // gate/up.
    std::vector<float> gu(2 * kSharedIs, 0.0f);
    for (int n = 0; n < 2 * kSharedIs; ++n) {
      float acc = 0.0f;
      const float* wrow = &shared_gu_h[static_cast<size_t>(n) * kHs];
      for (int c = 0; c < kHs; ++c)
        acc += x_f[static_cast<size_t>(t) * kHs + c] * wrow[c];
      gu[n] = Bf16Round(acc);
    }
    std::vector<float> swiglu(kSharedIs);
    for (int c = 0; c < kSharedIs; ++c) {
      const float g = gu[c], u = gu[kSharedIs + c];
      swiglu[c] = Bf16Round((g / (1.0f + std::exp(-g))) * u);
    }
    std::vector<float> shared_down(kHs, 0.0f);
    for (int c = 0; c < kHs; ++c) {
      float acc = 0.0f;
      const float* wrow = &shared_down_h[static_cast<size_t>(c) * kSharedIs];
      for (int kk = 0; kk < kSharedIs; ++kk) acc += swiglu[kk] * wrow[kk];
      shared_down[c] = Bf16Round(acc);
    }
    float dot = 0.0f;
    for (int c = 0; c < kHs; ++c)
      dot += x_f[static_cast<size_t>(t) * kHs + c] * shared_gate_scalar_h[c];
    const float gate = 1.0f / (1.0f + std::exp(-dot));
    for (int c = 0; c < kHs; ++c)
      y_ref[static_cast<size_t>(t) * kHs + c] =
          routed_ref[static_cast<size_t>(t) * kHs + c] +
          gate * shared_down[c];
  }

  // Compare (L2 relative error, robust to near-zero).
  double num = 0.0, den = 0.0;
  for (size_t i = 0; i < y_ref.size(); ++i) {
    const double d = Bf16ToFloat(y_bf[i]) - y_ref[i];
    num += d * d;
    den += y_ref[i] * y_ref[i];
  }
  const float l2 = float(std::sqrt(num) / (std::sqrt(den) + 1e-6));
  std::printf("  MoE forward T=%d k=%d l2_rel=%.5g\n", kT, kTopK, l2);

  // Cleanup.
  cudaFree(d_x);
  cudaFree(d_y);
  cudaFree(d_ws);
  cudaFree(d_gemm_ws);
  extra.Free();
  if (routed.gu_packed) cudaFree(routed.gu_packed);
  if (routed.gu_sf) cudaFree(routed.gu_sf);
  if (routed.dn_packed) cudaFree(routed.dn_packed);
  if (routed.dn_sf) cudaFree(routed.dn_sf);
  if (routed.gu_w_scale2) cudaFree(routed.gu_w_scale2);
  if (routed.gu_input_scale) cudaFree(routed.gu_input_scale);
  if (routed.dn_w_scale2) cudaFree(routed.dn_w_scale2);
  if (routed.dn_input_scale) cudaFree(routed.dn_input_scale);

  Q4T_CHECK(l2 < 2e-2f);
  return true;
}

// Differential contract: slot-mode residency forward must be BIT-EXACT to the
// all-experts-resident forward for the same input (same experts, same weight
// bytes, same router). Runs both the prefill path (T>1, MoERoutedForward) and
// the decode path (T=1, MoEDeviceDecode). C=64 < distinct experts selected,
// so on-demand loads + LRU evictions are exercised.
Q4T_TEST(moe_forward_residency_bitexact) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  WeightIndex* idx = nullptr;
  WeightLoader* loader = nullptr;
  if (!FileExists(kIndex) || !WeightIndex::Open(kIndex, &idx).ok() ||
      !WeightLoader::Create(kModelDir, *idx, 16, &loader).ok()) {
    Q4T_SKIP("(skipped: real model not present)");
  }

  MoEWeightLayout routed;
  Status s = LoadMoEWeights(*loader, kLayer, kE, kHs, kMoeIs, &routed, 0);
  if (!s.ok()) {
    std::printf("  routed load failed: %s\n", s.message().c_str());
    return false;
  }
  MoEExtraWeights extra;
  s = LoadMoEExtra(*loader, kMlpPrefix, kE, kHs, kSharedIs, &extra, 0);
  if (!s.ok()) {
    std::printf("  extra load failed: %s\n", s.message().c_str());
    return false;
  }

  const int C = 64;
  q4t::quant::MoEResidency res;
  s = res.Init(*loader, kLayer, kE, kHs, kMoeIs, C, 0);
  if (!s.ok()) {
    std::printf("  residency init failed: %s\n", s.message().c_str());
    return false;
  }

  const size_t gemm_ws = 32 * 1024 * 1024;
  void* d_gemm_ws = nullptr;
  cudaMalloc(&d_gemm_ws, gemm_ws);

  auto run_case = [&](int T) -> bool {
    std::mt19937 rng(777 + T);
    std::normal_distribution<float> dist(0.0f, 1.0f);
    std::vector<float> x_f(static_cast<size_t>(T) * kHs);
    for (auto& v : x_f) v = dist(rng);
    std::vector<uint16_t> x_bf16(x_f.size());
    for (size_t i = 0; i < x_f.size(); ++i) {
      const __nv_bfloat16 b = __float2bfloat16(x_f[i]);
      x_bf16[i] = *reinterpret_cast<const uint16_t*>(&b);
    }

    const size_t ws_bytes = q4t::model::MoEForwardWorkspaceBytes(
        T, kTopK, kHs, kMoeIs, kSharedIs, kE);
    uint8_t* d_ws = nullptr;
    uint16_t* d_x = nullptr;
    uint16_t* d_y = nullptr;
    cudaMalloc(&d_ws, ws_bytes);
    cudaMalloc(&d_x, x_bf16.size() * 2);
    cudaMalloc(&d_y, static_cast<size_t>(T) * kHs * 2);
    cudaMemcpy(d_x, x_bf16.data(), x_bf16.size() * 2, cudaMemcpyHostToDevice);

    s = MoEForward(d_x, routed, extra, d_y, T, kTopK, d_ws, ws_bytes,
                   d_gemm_ws, gemm_ws, 0);
    if (!s.ok()) {
      std::printf("  T=%d baseline MoEForward failed: %s\n", T,
                  s.message().c_str());
      return false;
    }
    if (cudaDeviceSynchronize() != cudaSuccess) return false;
    std::vector<uint16_t> y_base(static_cast<size_t>(T) * kHs);
    cudaMemcpy(y_base.data(), d_y, y_base.size() * 2,
               cudaMemcpyDeviceToHost);

    s = MoEForward(d_x, res.Layout(), extra, d_y, T, kTopK, d_ws, ws_bytes,
                   d_gemm_ws, gemm_ws, 0, nullptr, kLayer, &res);
    if (!s.ok()) {
      std::printf("  T=%d residency MoEForward failed: %s\n", T,
                  s.message().c_str());
      return false;
    }
    if (cudaDeviceSynchronize() != cudaSuccess) return false;
    std::vector<uint16_t> y_slot(static_cast<size_t>(T) * kHs);
    cudaMemcpy(y_slot.data(), d_y, y_slot.size() * 2,
               cudaMemcpyDeviceToHost);

    bool same = (y_base == y_slot);
    size_t first_diff = y_base.size();
    for (size_t i = 0; i < y_base.size(); ++i) {
      if (y_base[i] != y_slot[i]) {
        first_diff = i;
        break;
      }
    }
    std::printf("  T=%d C=%d: BIT-EXACT=%s loads=%llu misses=%llu\n", T, C,
                same ? "true" : "false",
                (unsigned long long)res.GetStats().loads,
                (unsigned long long)res.GetStats().misses);
    if (!same) {
      const size_t t = first_diff / kHs;
      const size_t c = first_diff % kHs;
      std::printf("  first diff at token=%zu col=%zu base=%f slot=%f\n", t, c,
                  Bf16ToFloat(y_base[first_diff]),
                  Bf16ToFloat(y_slot[first_diff]));
    }
    cudaFree(d_ws);
    cudaFree(d_x);
    cudaFree(d_y);
    return same;
  };

  const bool prefill_ok = run_case(32);
  const bool decode_ok = run_case(1);

  cudaFree(d_gemm_ws);
  res.Free();
  extra.Free();
  if (routed.gu_packed) cudaFree(routed.gu_packed);
  if (routed.gu_sf) cudaFree(routed.gu_sf);
  if (routed.dn_packed) cudaFree(routed.dn_packed);
  if (routed.dn_sf) cudaFree(routed.dn_sf);
  if (routed.gu_w_scale2) cudaFree(routed.gu_w_scale2);
  if (routed.gu_input_scale) cudaFree(routed.gu_input_scale);
  if (routed.dn_w_scale2) cudaFree(routed.dn_w_scale2);
  if (routed.dn_input_scale) cudaFree(routed.dn_input_scale);
  delete loader;
  delete idx;

  Q4T_CHECK(prefill_ok);
  Q4T_CHECK(decode_ok);
  return true;
}

namespace {

// The acceptance runner fixes process environment before any MoE call. These
// tests never toggle a mode that the runtime may have cached in a static.
bool PartitionEnvironmentMatches() {
  for (const auto& [name, expected] :
       {std::pair{"Q4T_MOE_PARTITION", "1"},
        std::pair{"Q4T_MOE_CHUNK_ORDER", "0"},
        std::pair{"Q4T_MOE_STREAMS", "1"},
        std::pair{"Q4T_MOE_EVICT_WEIGHT", "0"}}) {
    const char* actual = std::getenv(name);
    if (!actual || std::strcmp(actual, expected) != 0) {
      std::printf("  required environment: %s=%s\n", name, expected);
      return false;
    }
  }
  return true;
}

template <typename T>
struct PartitionDeviceBuffer {
  T* data = nullptr;
  ~PartitionDeviceBuffer() {
    if (data) cudaFree(data);
  }
  bool Allocate(size_t count) {
    return cudaMalloc(&data, count * sizeof(T)) == cudaSuccess;
  }
};

struct PartitionRealWeights {
  WeightIndex* index = nullptr;
  WeightLoader* loader = nullptr;
  MoEWeightLayout routed;
  MoEExtraWeights extra;

  ~PartitionRealWeights() {
    extra.Free();
    for (void* pointer :
         {static_cast<void*>(routed.gu_packed),
          static_cast<void*>(routed.gu_sf),
          static_cast<void*>(routed.dn_packed),
          static_cast<void*>(routed.dn_sf),
          static_cast<void*>(routed.gu_w_scale2),
          static_cast<void*>(routed.gu_input_scale),
          static_cast<void*>(routed.dn_w_scale2),
          static_cast<void*>(routed.dn_input_scale)}) {
      if (pointer) cudaFree(pointer);
    }
    delete loader;
    delete index;
  }

  bool Init() {
    Q4T_CHECK(WeightIndex::Open(kIndex, &index).ok());
    Q4T_CHECK(WeightLoader::Create(kModelDir, *index, 16, &loader).ok());
    Q4T_CHECK(LoadMoEWeights(*loader, kLayer, kE, kHs, kMoeIs,
                            &routed, 0).ok());
    Q4T_CHECK(LoadMoEExtra(*loader, kMlpPrefix, kE, kHs, kSharedIs,
                          &extra, 0).ok());
    return true;
  }
};

struct PartitionForwardSnapshot {
  std::vector<uint16_t> output;
  std::vector<int32_t> ids;
  std::vector<float> router_weights;
  std::vector<float> routed;
};

std::vector<uint16_t> PartitionInputRows(int tokens, uint32_t seed) {
  std::mt19937 random(seed);
  std::normal_distribution<float> normal(0.0f, 1.0f);
  std::vector<uint16_t> input(static_cast<size_t>(tokens) * kHs);
  for (auto& value : input) value = FloatToBf16(normal(random));
  return input;
}

bool CapturePartitionForward(const uint8_t* workspace, const uint16_t* output,
                             int tokens, PartitionForwardSnapshot* result) {
  const size_t routed_bytes = q4t::quant::MoEWorkspace::RequiredBytes(
      tokens, kTopK, kHs, kMoeIs);
  // This is the MoEForward scratch layout, not the slot IDs
  // uploaded to the separate per-chunk staging buffers.
  const uint8_t* scratch = workspace + ((routed_bytes + 7) & ~size_t{7});
  scratch += static_cast<size_t>(tokens) * kE * sizeof(uint16_t);
  result->ids.resize(static_cast<size_t>(tokens) * kTopK);
  const size_t ids_bytes = result->ids.size() * sizeof(int32_t);
  Q4T_CHECK(cudaMemcpy(result->ids.data(), scratch, ids_bytes,
                        cudaMemcpyDeviceToHost) == cudaSuccess);
  scratch += ids_bytes;
  result->router_weights.resize(result->ids.size());
  const size_t weights_bytes = result->router_weights.size() * sizeof(float);
  Q4T_CHECK(cudaMemcpy(result->router_weights.data(), scratch, weights_bytes,
                        cudaMemcpyDeviceToHost) == cudaSuccess);
  scratch += weights_bytes;
  result->routed.resize(static_cast<size_t>(tokens) * kHs);
  Q4T_CHECK(cudaMemcpy(result->routed.data(), scratch,
                        result->routed.size() * sizeof(float),
                        cudaMemcpyDeviceToHost) == cudaSuccess);
  result->output.resize(result->routed.size());
  Q4T_CHECK(cudaMemcpy(result->output.data(), output,
                        result->output.size() * sizeof(uint16_t),
                        cudaMemcpyDeviceToHost) == cudaSuccess);
  return true;
}

bool ComparePartitionForward(const PartitionForwardSnapshot& baseline,
                             const PartitionForwardSnapshot& candidate) {
  Q4T_CHECK(baseline.ids == candidate.ids);
  Q4T_CHECK(baseline.router_weights.size() == candidate.router_weights.size());
  Q4T_CHECK(std::memcmp(baseline.router_weights.data(),
                         candidate.router_weights.data(),
                         baseline.router_weights.size() * sizeof(float)) == 0);
  for (float weight : candidate.router_weights) {
    Q4T_CHECK(std::isfinite(weight));
  }
  Q4T_CHECK(baseline.routed.size() == candidate.routed.size());
  size_t routed_diffs = 0;
  size_t first_routed = baseline.routed.size();
  float max_abs = 0.0f;
  for (size_t i = 0; i < baseline.routed.size(); ++i) {
    Q4T_CHECK(std::isfinite(baseline.routed[i]));
    Q4T_CHECK(std::isfinite(candidate.routed[i]));
    if (std::bit_cast<uint32_t>(baseline.routed[i]) !=
        std::bit_cast<uint32_t>(candidate.routed[i])) {
      if (routed_diffs++ == 0) first_routed = i;
      max_abs = std::max(max_abs,
                         std::fabs(baseline.routed[i] - candidate.routed[i]));
    }
  }
  // FP32 equality is reported for attribution; the pre-frozen acceptance
  // assertion remains complete BF16 output equality, without tolerance.
  std::printf("  routed_fp32: bit_differences=%zu max_abs=%.9g\n",
              routed_diffs, max_abs);
  if (routed_diffs) {
    std::printf("  routed_fp32 first token=%zu col=%zu base=%.9g slot=%.9g\n",
                first_routed / kHs, first_routed % kHs,
                baseline.routed[first_routed], candidate.routed[first_routed]);
  }
  Q4T_CHECK(baseline.output.size() == candidate.output.size());
  for (size_t i = 0; i < baseline.output.size(); ++i) {
    Q4T_CHECK(std::isfinite(Bf16ToFloat(baseline.output[i])));
    Q4T_CHECK(std::isfinite(Bf16ToFloat(candidate.output[i])));
    if (baseline.output[i] != candidate.output[i]) {
      std::printf("  BF16 first token=%zu col=%zu base=%.9g slot=%.9g\n",
                  i / kHs, i % kHs, Bf16ToFloat(baseline.output[i]),
                  Bf16ToFloat(candidate.output[i]));
      return false;
    }
  }
  return true;
}

bool RunPartitionNumericalCase(
    const PartitionRealWeights& weights, q4t::quant::MoEResidency* residency,
    const std::vector<uint16_t>& input,
    const std::vector<int32_t>& expected_ids,
    const std::vector<int32_t>& expected_order, size_t expected_chunks,
    size_t expected_singletons) {
  const int tokens = static_cast<int>(input.size() / kHs);
  // This helper's one-row fixture is the decode-bypass case after prefill.
  // Request-policy singleton prefill uses a separate helper below.
  const auto log_phase = tokens == 1
      ? q4t::model::MoEPartitionLogPhase::kSingleDecode
      : q4t::model::MoEPartitionLogPhase::kUnknown;
  std::printf("  partition_numerical log_phase=%s T=%d\n",
              tokens == 1 ? "explicit_single_decode" : "unknown", tokens);
  const size_t workspace_bytes = q4t::model::MoEForwardWorkspaceBytes(
      tokens, kTopK, kHs, kMoeIs, kSharedIs, kE);
  constexpr size_t kGemmBytes = 32 * 1024 * 1024;
  PartitionDeviceBuffer<uint8_t> workspace, gemm;
  PartitionDeviceBuffer<uint16_t> x, y;
  Q4T_CHECK(workspace.Allocate(workspace_bytes));
  Q4T_CHECK(gemm.Allocate(kGemmBytes));
  Q4T_CHECK(x.Allocate(input.size()));
  Q4T_CHECK(y.Allocate(input.size()));
  Q4T_CHECK(cudaMemcpy(x.data, input.data(), input.size() * sizeof(uint16_t),
                        cudaMemcpyHostToDevice) == cudaSuccess);
  auto forward = [&](const MoEWeightLayout& routed,
                     const q4t::quant::MoEResidency* slots,
                     PartitionForwardSnapshot* result,
                     q4t::model::MoEForwardDiagnostics* diagnostics) {
    // A missed scatter must not inherit the full-resident reference output.
    Q4T_CHECK(cudaMemset(workspace.data, 0xff, workspace_bytes) == cudaSuccess);
    Q4T_CHECK(cudaMemset(y.data, 0xff, input.size() * sizeof(uint16_t)) ==
               cudaSuccess);
    const Status status = MoEForward(
        x.data, routed, weights.extra, y.data, tokens, kTopK, workspace.data,
        workspace_bytes, gemm.data, kGemmBytes, 0, nullptr, kLayer, slots,
        diagnostics, {}, log_phase);
    if (!status.ok()) {
      std::printf("  MoEForward failed: %s\n", status.message().c_str());
      return false;
    }
    Q4T_CHECK(cudaDeviceSynchronize() == cudaSuccess);
    return CapturePartitionForward(workspace.data, y.data, tokens, result);
  };
  PartitionForwardSnapshot baseline, candidate;
  q4t::model::MoEForwardDiagnostics diagnostics;
  Q4T_CHECK(forward(weights.routed, nullptr, &baseline, nullptr));
  Q4T_CHECK(forward(residency->Layout(), residency, &candidate, &diagnostics));
  Q4T_CHECK(diagnostics.partition_requested);
  Q4T_CHECK(diagnostics.partition_applied == (tokens > 1));
  Q4T_CHECK(!diagnostics.partition_fallback);
  Q4T_CHECK(diagnostics.chunks == diagnostics.actual_executed_chunks);
  Q4T_CHECK(diagnostics.singleton_chunks ==
             diagnostics.actual_singleton_dispatches);
  Q4T_CHECK(diagnostics.executed_token_rows.size() ==
             static_cast<size_t>(tokens));
  std::vector<int32_t> visits = diagnostics.executed_token_rows;
  std::sort(visits.begin(), visits.end());
  for (int token = 0; token < tokens; ++token) {
    Q4T_CHECK(visits[token] == token);
  }
  if (tokens == 1) {
    Q4T_CHECK(diagnostics.chunks == 1);
    Q4T_CHECK(diagnostics.actual_singleton_dispatches == 1);
  } else {
    Q4T_CHECK(diagnostics.chunks > 1);
  }
  if (!expected_ids.empty()) Q4T_CHECK(candidate.ids == expected_ids);
  if (!expected_order.empty()) {
    Q4T_CHECK(diagnostics.executed_token_rows == expected_order);
    Q4T_CHECK(diagnostics.chunks == expected_chunks);
    Q4T_CHECK(diagnostics.singleton_chunks == expected_singletons);
  }
  std::printf("  layer=%d C=%d T=%d applied=%d chunks=%zu singleton=%zu "
              "executed=%zu singleton_dispatches=%zu\n",
              kLayer, residency->Slots(), tokens,
              static_cast<int>(diagnostics.partition_applied),
              diagnostics.chunks, diagnostics.singleton_chunks,
              diagnostics.actual_executed_chunks,
              diagnostics.actual_singleton_dispatches);
  const bool same = ComparePartitionForward(baseline, candidate);
  std::printf("  layer=%d C=%d T=%d BF16_BIT_EXACT=%s\n", kLayer,
              residency->Slots(), tokens, same ? "true" : "false");
  return same;
}

}  // namespace

Q4T_TEST(moe_partition_runtime_numerical_contract) {
  Q4T_CHECK(PartitionEnvironmentMatches());
  if (!CudaAvailable()) {
    Q4T_SKIP("no CUDA device; required runner rejects skip");
  }
  if (!FileExists(kIndex)) {
    Q4T_SKIP("model absent; required runner rejects skip");
  }
  PartitionRealWeights weights;
  Q4T_CHECK(weights.Init());
  {
    q4t::quant::MoEResidency residency;
    Q4T_CHECK(residency.Init(*weights.loader, kLayer, kE, kHs, kMoeIs,
                             256, 0).ok());
    Q4T_CHECK(RunPartitionNumericalCase(
        weights, &residency, PartitionInputRows(1024, 1801), {}, {}, 0, 0));
    // Keep the preceding prefill's cache state when checking decode bypass.
    Q4T_CHECK(RunPartitionNumericalCase(
        weights, &residency, PartitionInputRows(1, 778), {}, {}, 0, 0));
  }

  // Real routed/shared weights, with a controlled in-memory router. The first
  // four input columns select exact per-token logits; all other columns keep
  // seeded nonzero activations. No checkpoint file is modified.
  constexpr int kTokens = 4;
  const std::array<std::array<int32_t, kTopK>, 3> expert_sets{{
      {{0, 1, 2, 3, 4, 5, 6, 7, 8, 9}},
      {{0, 10, 11, 12, 13, 14, 15, 16, 17, 18}},
      {{1, 2, 3, 4, 5, 6, 7, 8, 9, 19}},
  }};
  const std::array<int, kTokens> row_sets{1, 0, 2, 0};  // B, A, C, A
  std::vector<uint16_t> gate(static_cast<size_t>(kE) * kHs, 0);
  std::vector<int32_t> ids(kTokens * kTopK);
  auto input = PartitionInputRows(kTokens, 24301);
  for (int token = 0; token < kTokens; ++token) {
    for (int column = 0; column < kTokens; ++column) {
      input[static_cast<size_t>(token) * kHs + column] =
          FloatToBf16(column == token ? 1.0f : 0.0f);
    }
    for (int rank = 0; rank < kTopK; ++rank) {
      const int expert = expert_sets[row_sets[token]][(3 * rank + token) % 10];
      ids[token * kTopK + rank] = expert;
      gate[static_cast<size_t>(expert) * kHs + token] =
          FloatToBf16(4.0f - 0.25f * rank);
    }
  }
  Q4T_CHECK(cudaMemcpy(weights.extra.gate, gate.data(),
                        gate.size() * sizeof(uint16_t),
                        cudaMemcpyHostToDevice) == cudaSuccess);
  q4t::quant::MoEResidency residency;
  Q4T_CHECK(residency.Init(*weights.loader, kLayer, kE, kHs, kMoeIs,
                           15, 0).ok());
  // Lex packing creates [1,3], [0], [2]. Min-new must execute [1,3,2], [0],
  // exercising changed expert M_e, noncontiguous gather/scatter and a T>1
  // forward's actual single-row decode dispatch with full-resident reference.
  Q4T_CHECK(RunPartitionNumericalCase(
      weights, &residency, input, ids, {1, 3, 2, 0}, 2, 1));
  return true;
}

namespace {

// The small activation shape verifies policy consumption inside MoE only.
// full input length is a context label; this is not an 8193-token model run.
bool RunRequestPolicyNumericalCase(
    const PartitionRealWeights& weights, q4t::quant::MoEResidency* residency,
    const std::vector<uint16_t>& input,
    const q4t::model::MoERequestPartition& request,
    const std::vector<int32_t>& expected_ids = {},
    const std::vector<int32_t>& expected_order = {}, size_t expected_chunks = 0,
    size_t expected_singletons = 0) {
  const int tokens = static_cast<int>(input.size() / kHs);
  Q4T_CHECK(request.Enabled());
  Q4T_CHECK(request.ValidForward(tokens));
  const size_t workspace_bytes = q4t::model::MoEForwardWorkspaceBytes(
      tokens, kTopK, kHs, kMoeIs, kSharedIs, kE);
  constexpr size_t kGemmBytes = 32 * 1024 * 1024;
  PartitionDeviceBuffer<uint8_t> workspace, gemm;
  PartitionDeviceBuffer<uint16_t> x, y;
  Q4T_CHECK(workspace.Allocate(workspace_bytes));
  Q4T_CHECK(gemm.Allocate(kGemmBytes));
  Q4T_CHECK(x.Allocate(input.size()));
  Q4T_CHECK(y.Allocate(input.size()));
  Q4T_CHECK(cudaMemcpy(x.data, input.data(), input.size() * sizeof(uint16_t),
                        cudaMemcpyHostToDevice) == cudaSuccess);
  auto forward = [&](const MoEWeightLayout& routed,
                     const q4t::quant::MoEResidency* slots,
                     const q4t::model::MoERequestPartition& context,
                     PartitionForwardSnapshot* result,
                     q4t::model::MoEForwardDiagnostics* diagnostics) {
    Q4T_CHECK(cudaMemset(workspace.data, 0xff, workspace_bytes) == cudaSuccess);
    Q4T_CHECK(cudaMemset(y.data, 0xff, input.size() * sizeof(uint16_t)) ==
               cudaSuccess);
    const Status status = MoEForward(
        x.data, routed, weights.extra, y.data, tokens, kTopK, workspace.data,
        workspace_bytes, gemm.data, kGemmBytes, 0, nullptr, kLayer, slots,
        diagnostics, context);
    if (!status.ok()) {
      std::printf("  request-policy MoEForward failed: %s\n",
                  status.message().c_str());
      return false;
    }
    Q4T_CHECK(cudaDeviceSynchronize() == cudaSuccess);
    return CapturePartitionForward(workspace.data, y.data, tokens, result);
  };
  PartitionForwardSnapshot reference, selected;
  q4t::model::MoEForwardDiagnostics diagnostics;
  Q4T_CHECK(forward(weights.routed, nullptr, {}, &reference, nullptr));
  Q4T_CHECK(forward(residency->Layout(), residency, request, &selected,
                     &diagnostics));
  const bool requested = request.RequestTokens() > 8192;
  Q4T_CHECK(diagnostics.partition_requested == requested);
  Q4T_CHECK(diagnostics.partition_applied == (requested && tokens > 1));
  Q4T_CHECK(!diagnostics.partition_fallback);
  Q4T_CHECK(diagnostics.chunks > 0);
  Q4T_CHECK(diagnostics.chunks == diagnostics.actual_executed_chunks);
  Q4T_CHECK(diagnostics.singleton_chunks ==
             diagnostics.actual_singleton_dispatches);
  auto visits = diagnostics.executed_token_rows;
  Q4T_CHECK(visits.size() == static_cast<size_t>(tokens));
  std::sort(visits.begin(), visits.end());
  for (int token = 0; token < tokens; ++token) {
    Q4T_CHECK(visits[token] == token);
  }
  if (tokens == 1) {
    Q4T_CHECK(diagnostics.chunks == 1);
    Q4T_CHECK(diagnostics.actual_singleton_dispatches == 1);
  }
  if (!expected_ids.empty()) Q4T_CHECK(selected.ids == expected_ids);
  if (!expected_order.empty()) {
    Q4T_CHECK(diagnostics.executed_token_rows == expected_order);
    Q4T_CHECK(diagnostics.chunks == expected_chunks);
    Q4T_CHECK(diagnostics.singleton_chunks == expected_singletons);
  }
  const bool same = ComparePartitionForward(reference, selected);
  std::printf("  request_policy layer=%d C=%d input_tokens=%d base=%d T=%d "
              "mode=%d applied=%d chunks=%zu singleton=%zu "
              "BF16_BIT_EXACT=%s\n",
              kLayer, residency->Slots(), request.RequestTokens(),
              request.Base(), tokens, request.PartitionMode(1),
              diagnostics.partition_applied, diagnostics.chunks,
              diagnostics.singleton_chunks, same ? "true" : "false");
  return same;
}

}  // namespace

Q4T_TEST(moe_request_policy_numerical_contract) {
  Q4T_CHECK(PartitionEnvironmentMatches());
  if (!CudaAvailable()) {
    Q4T_SKIP("no CUDA device; required runner rejects skip");
  }
  if (!FileExists(kIndex)) {
    Q4T_SKIP("model absent; required runner rejects skip");
  }
  using q4t::model::MoERequestPartition;
  PartitionRealWeights weights;
  Q4T_CHECK(weights.Init());
  {
    // Real router, routed and shared weights. A long request's continuation
    // still selects min-new for a small T; a short request selects legacy.
    q4t::quant::MoEResidency residency;
    Q4T_CHECK(residency.Init(*weights.loader, kLayer, kE, kHs, kMoeIs,
                             64, 0).ok());
    const auto input = PartitionInputRows(32, 809);
    Q4T_CHECK(RunRequestPolicyNumericalCase(
        weights, &residency, input,
        MoERequestPartition(8192, "numeric-short")));
    Q4T_CHECK(RunRequestPolicyNumericalCase(
        weights, &residency, input,
        MoERequestPartition(8193, "numeric-long")));
    Q4T_CHECK(RunRequestPolicyNumericalCase(
        weights, &residency, input,
        MoERequestPartition(16385, "numeric-continuation").WithBase(8192)));
    Q4T_CHECK(RunRequestPolicyNumericalCase(
        weights, &residency, PartitionInputRows(1, 778),
        MoERequestPartition(8193, "numeric-singleton").WithBase(8192)));
  }

  // Controlled in-memory router, unchanged real expert/shared weights. This
  // fixture makes legacy and min-new membership differ, so a default argument
  // accidentally bypassing request policy cannot pass by chance.
  constexpr int kTokens = 4;
  const std::array<std::array<int32_t, kTopK>, 3> expert_sets{{
      {{0, 1, 2, 3, 4, 5, 6, 7, 8, 9}},
      {{0, 10, 11, 12, 13, 14, 15, 16, 17, 18}},
      {{1, 2, 3, 4, 5, 6, 7, 8, 9, 19}},
  }};
  const std::array<int, kTokens> row_sets{1, 0, 2, 0};
  std::vector<uint16_t> gate(static_cast<size_t>(kE) * kHs, 0);
  std::vector<int32_t> ids(kTokens * kTopK);
  auto input = PartitionInputRows(kTokens, 24301);
  for (int token = 0; token < kTokens; ++token) {
    for (int column = 0; column < kTokens; ++column) {
      input[static_cast<size_t>(token) * kHs + column] =
          FloatToBf16(column == token ? 1.0f : 0.0f);
    }
    for (int rank = 0; rank < kTopK; ++rank) {
      const int expert = expert_sets[row_sets[token]][(3 * rank + token) % 10];
      ids[token * kTopK + rank] = expert;
      gate[static_cast<size_t>(expert) * kHs + token] =
          FloatToBf16(4.0f - 0.25f * rank);
    }
  }
  Q4T_CHECK(cudaMemcpy(weights.extra.gate, gate.data(),
                        gate.size() * sizeof(uint16_t),
                        cudaMemcpyHostToDevice) == cudaSuccess);
  q4t::quant::MoEResidency residency;
  Q4T_CHECK(residency.Init(*weights.loader, kLayer, kE, kHs, kMoeIs,
                           15, 0).ok());
  Q4T_CHECK(RunRequestPolicyNumericalCase(
      weights, &residency, input,
      MoERequestPartition(8192, "controlled-short"), ids,
      {1, 3, 0, 2}, 3, 2));
  Q4T_CHECK(RunRequestPolicyNumericalCase(
      weights, &residency, input,
      MoERequestPartition(16385, "controlled-long").WithBase(8192), ids,
      {1, 3, 2, 0}, 2, 1));
  const std::vector<uint16_t> singleton(input.begin(), input.begin() + kHs);
  const std::vector<int32_t> singleton_ids(ids.begin(), ids.begin() + kTopK);
  Q4T_CHECK(RunRequestPolicyNumericalCase(
      weights, &residency, singleton,
      MoERequestPartition(16385, "controlled-tail").WithBase(16384),
      singleton_ids, {0}, 1, 1));
  // The same residency object retains its cache history. Request selection
  // must return to legacy after the preceding long-policy contexts.
  Q4T_CHECK(RunRequestPolicyNumericalCase(
      weights, &residency, input,
      MoERequestPartition(8192, "controlled-short-after-long"), ids,
      {1, 3, 0, 2}, 3, 2));
  return true;
}

namespace {

// These knobs are read per residency Init, unlike the process-cached MoE
// partition/stream modes, which the runner fixes before selecting this test.
class MirrorRecycleEnvGuard {
 public:
  MirrorRecycleEnvGuard(const char* name, const char* value) : name_(name) {
    if (const char* previous = std::getenv(name)) {
      had_value_ = true;
      previous_ = previous;
    }
    setenv(name, value, 1);
  }
  ~MirrorRecycleEnvGuard() {
    if (had_value_)
      setenv(name_.c_str(), previous_.c_str(), 1);
    else
      unsetenv(name_.c_str());
  }

 private:
  std::string name_;
  std::string previous_;
  bool had_value_ = false;
};

// Residency has an explicit Free API, not an owning destructor. Register this
// before either Init so every return joins initialized workers before their
// residency objects die. Resolve/forward return after the worker barrier;
// stream0 may still have copies queued, so drain it before releasing buffers.
class MirrorRecycleResidencyGuard {
 public:
  MirrorRecycleResidencyGuard(q4t::quant::MoEResidency& off,
                              q4t::quant::MoEResidency& on)
      : off_(off), on_(on) {}
  MirrorRecycleResidencyGuard(const MirrorRecycleResidencyGuard&) = delete;
  MirrorRecycleResidencyGuard& operator=(const MirrorRecycleResidencyGuard&) =
      delete;
  ~MirrorRecycleResidencyGuard() {
    cudaStreamSynchronize(0);
    on_.Free();
    off_.Free();
  }

 private:
  q4t::quant::MoEResidency& off_;
  q4t::quant::MoEResidency& on_;
};

bool MirrorRecycleCounters(const q4t::quant::MoEResidency::Stats& stats,
                           uint64_t plans, uint64_t preferred, uint64_t changed,
                           uint64_t fallback) {
  Q4T_CHECK(stats.mirror_gpu_recycle_plans == plans);
  Q4T_CHECK(stats.mirror_gpu_recycle_attempts == preferred + fallback);
  Q4T_CHECK(stats.mirror_gpu_recycle_preferred == preferred);
  Q4T_CHECK(stats.mirror_gpu_recycle_changed == changed);
  Q4T_CHECK(stats.mirror_gpu_recycle_fallback == fallback);
  Q4T_CHECK(stats.mirror_gpu_recycle_unavailable == 0);
  Q4T_CHECK(stats.mirror_gpu_recycle_published == preferred);
  return true;
}

bool PrepareMirrorRecycleState(const PartitionRealWeights& weights,
                               q4t::quant::MoEResidency* residency,
                               bool enabled) {
  const MirrorRecycleEnvGuard mode("Q4T_MOE_MIRROR_GPU_RECYCLE",
                                   enabled ? "1" : "0");
  Q4T_CHECK(
      residency->Init(*weights.loader, kLayer, kE, kHs, kMoeIs, 16, 0).ok());
  auto resolve = [&](const std::vector<int32_t>& needed) {
    std::vector<int32_t> slots(needed.size(), -1);
    Q4T_CHECK(residency
                  ->Resolve(needed.data(), static_cast<int>(needed.size()),
                            slots.data(), 0)
                  .ok());
    Q4T_CHECK(cudaStreamSynchronize(0) == cudaSuccess);
    return true;
  };
  Q4T_CHECK(resolve({0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}));
  Q4T_CHECK(resolve({16, 17, 18, 19, 20, 21, 22, 23}));
  Q4T_CHECK(resolve({2}));
  // Resolve deliberately lacks the explicit decode contract: setup must
  // retain the legacy policy even in the enabled instance.
  Q4T_CHECK(MirrorRecycleCounters(residency->GetStats(), 0, 0, 0, 0));
  const auto state = residency->CopyDiagnosticState();
  const std::vector<int> expected_slots{16, 17, 18, 19, 20, 21, 22, 23,
                                        2,  9,  10, 11, 12, 13, 14, 15};
  const std::vector<int> expected_ring{8, 1, 2, 3, 4, 5, 6, 7};
  Q4T_CHECK(state.slot_experts == expected_slots);
  Q4T_CHECK(state.l2_experts.size() == 8);
  Q4T_CHECK(state.mirror_experts == expected_ring);
  Q4T_CHECK(state.mirror_cursor == 1);
  Q4T_CHECK(residency->GetStats().mirror_hits == 1);
  Q4T_CHECK(residency->GetStats().mirror_skips == 0);
  return true;
}

bool RunMirrorRecycleCase(const PartitionRealWeights& weights,
                          const char* label,
                          q4t::model::MoEPartitionLogPhase phase,
                          const q4t::model::MoERequestPartition& request,
                          bool active) {
  const size_t workspace_bytes = q4t::model::MoEForwardWorkspaceBytes(
      1, kTopK, kHs, kMoeIs, kSharedIs, kE);
  constexpr size_t kGemmBytes = 32 * 1024 * 1024;
  PartitionDeviceBuffer<uint8_t> workspace, gemm;
  PartitionDeviceBuffer<uint16_t> x, y;
  q4t::quant::MoEResidency off, on;
  // Declared after scratch owners: teardown drains stream0 and frees both
  // residencies before either their objects or any scratch buffer is freed.
  const MirrorRecycleResidencyGuard residency_guard(off, on);
  Q4T_CHECK(PrepareMirrorRecycleState(weights, &off, false));
  Q4T_CHECK(PrepareMirrorRecycleState(weights, &on, true));
  Q4T_CHECK(workspace.Allocate(workspace_bytes));
  Q4T_CHECK(gemm.Allocate(kGemmBytes));
  Q4T_CHECK(x.Allocate(kHs));
  Q4T_CHECK(y.Allocate(kHs));
  auto input = PartitionInputRows(1, 24302);
  input[0] = FloatToBf16(1.0f);
  Q4T_CHECK(cudaMemcpy(x.data, input.data(), input.size() * sizeof(uint16_t),
                       cudaMemcpyHostToDevice) == cudaSuccess);
  const std::array<std::array<int32_t, kTopK>, 3> routes{{
      {{2, 16, 17, 18, 19, 20, 21, 22, 23, 24}},
      {{9, 2, 16, 17, 18, 19, 20, 21, 22, 23}},
      {{1, 2, 16, 17, 18, 19, 20, 21, 22, 23}},
  }};
  auto forward = [&](const MoEWeightLayout& layout,
                     const q4t::quant::MoEResidency* residency,
                     PartitionForwardSnapshot* result) {
    Q4T_CHECK(cudaMemset(workspace.data, 0xff, workspace_bytes) == cudaSuccess);
    Q4T_CHECK(cudaMemset(y.data, 0xff, kHs * sizeof(uint16_t)) == cudaSuccess);
    const Status status =
        MoEForward(x.data, layout, weights.extra, y.data, 1, kTopK,
                   workspace.data, workspace_bytes, gemm.data, kGemmBytes, 0,
                   nullptr, kLayer, residency, nullptr, request, phase);
    if (!status.ok()) {
      std::printf("  mirror recycle forward failed: %s\n",
                  status.message().c_str());
      return false;
    }
    Q4T_CHECK(cudaDeviceSynchronize() == cudaSuccess);
    return CapturePartitionForward(workspace.data, y.data, 1, result);
  };
  for (int step = 0; step < (active ? 3 : 1); ++step) {
    // Only the in-memory router changes. The complete routed/shared weight
    // payload and input are identical for reference, OFF, and ON forwards.
    std::vector<uint16_t> gate(static_cast<size_t>(kE) * kHs, 0);
    for (int rank = 0; rank < kTopK; ++rank) {
      gate[static_cast<size_t>(routes[step][rank]) * kHs] =
          FloatToBf16(4.0f - 0.25f * rank);
    }
    Q4T_CHECK(cudaMemcpy(weights.extra.gate, gate.data(),
                         gate.size() * sizeof(uint16_t),
                         cudaMemcpyHostToDevice) == cudaSuccess);
    const auto off_before = off.GetStats();
    const auto on_before = on.GetStats();
    PartitionForwardSnapshot reference, legacy, selected;
    Q4T_CHECK(forward(weights.routed, nullptr, &reference));
    Q4T_CHECK(forward(off.Layout(), &off, &legacy));
    Q4T_CHECK(forward(on.Layout(), &on, &selected));
    const std::vector<int32_t> expected_ids(routes[step].begin(),
                                            routes[step].end());
    Q4T_CHECK(reference.ids == expected_ids);
    Q4T_CHECK(ComparePartitionForward(reference, legacy));
    Q4T_CHECK(ComparePartitionForward(reference, selected));
    Q4T_CHECK(ComparePartitionForward(legacy, selected));
    Q4T_CHECK(std::memcmp(legacy.routed.data(), selected.routed.data(),
                          legacy.routed.size() * sizeof(float)) == 0);
    const auto off_state = off.CopyDiagnosticState();
    const auto on_state = on.CopyDiagnosticState();
    Q4T_CHECK(off_state.slot_experts == on_state.slot_experts);
    Q4T_CHECK(off_state.slot_ticks == on_state.slot_ticks);
    Q4T_CHECK(off.GetStats().loads == off_before.loads + 1);
    Q4T_CHECK(on.GetStats().loads == on_before.loads + 1);
    Q4T_CHECK(off.GetStats().mirror_writebacks ==
              off_before.mirror_writebacks + 1);
    Q4T_CHECK(on.GetStats().mirror_writebacks ==
              on_before.mirror_writebacks + 1);
    Q4T_CHECK(off.GetStats().mirror_skips == 0);
    Q4T_CHECK(on.GetStats().mirror_skips == 0);
    Q4T_CHECK(MirrorRecycleCounters(off.GetStats(), 0, 0, 0, 0));
    Q4T_CHECK(MirrorRecycleCounters(on.GetStats(), active ? step + 1 : 0,
                                    active ? 1 : 0, active ? 1 : 0,
                                    active ? step : 0));
    if (step == 0) {
      const std::vector<int> legacy_ring{8, 9, 2, 3, 4, 5, 6, 7};
      const std::vector<int> recycle_ring{8, 1, 9, 3, 4, 5, 6, 7};
      Q4T_CHECK(off_state.mirror_experts == legacy_ring);
      Q4T_CHECK(on_state.mirror_experts ==
                (active ? recycle_ring : legacy_ring));
      Q4T_CHECK(off_state.mirror_cursor == 2);
      Q4T_CHECK(on_state.mirror_cursor == (active ? 3 : 2));
      // Expert2 is a needed GPU hit, not a planned victim or incoming
      // expert. Its mirror is redundant for the complete active plan.
      Q4T_CHECK(on_state.slot_experts[8] == 2);
      Q4T_CHECK(on_state.slot_experts[9] == 24);
    } else if (step == 1) {
      // Read the expert9 payload written to the different target slots.
      Q4T_CHECK(off.GetStats().mirror_hits == off_before.mirror_hits + 1);
      Q4T_CHECK(on.GetStats().mirror_hits == on_before.mirror_hits + 1);
      // Make expert9 the next plan's victim. Its still-present mirror must
      // NOT count as GPU-covered after the complete plan reserves that slot.
      // These are all GPU hits, so no payload or ring content changes.
      const std::vector<int32_t> refresh{16, 17, 18, 19, 20, 21, 22, 23,
                                         2,  24, 11, 12, 13, 14, 15};
      for (auto* residency : {&off, &on}) {
        std::vector<int32_t> slots(refresh.size(), -1);
        const auto before = residency->GetStats();
        Q4T_CHECK(residency
                      ->Resolve(refresh.data(),
                                static_cast<int>(refresh.size()), slots.data(),
                                0)
                      .ok());
        Q4T_CHECK(cudaStreamSynchronize(0) == cudaSuccess);
        Q4T_CHECK(residency->GetStats().loads == before.loads);
        Q4T_CHECK(residency->SlotExpert(10) == 9);
      }
    } else {
      // The sole mirror of expert1 survived only in the candidate. Both
      // the mirror and disk source must reproduce the same expert output.
      Q4T_CHECK(off.GetStats().mirror_hits == off_before.mirror_hits);
      Q4T_CHECK(off.GetStats().l2_misses == off_before.l2_misses + 1);
      Q4T_CHECK(on.GetStats().mirror_hits == on_before.mirror_hits + 1);
      Q4T_CHECK(on.GetStats().l2_misses == on_before.l2_misses);
      Q4T_CHECK(on_state.slot_experts[10] == 1);
    }
    std::printf(
        "  mirror_recycle case=%s step=%d layer=%d C=16 T=1 "
        "active=%d changed=%llu BF16_BIT_EXACT=true "
        "OFF_ON_FP32_BIT_EXACT=true\n",
        label, step, kLayer, active,
        static_cast<unsigned long long>(
            on.GetStats().mirror_gpu_recycle_changed));
  }
  return true;
}

}  // namespace

// Bounded real-weight contract, not an all-layer or concurrent stress proof.
// Select alone in a fresh process after the HTTP-first performance gate.
Q4T_TEST(moe_mirror_gpu_recycle_numerical_contract) {
  for (const auto& [name, expected] :
       {std::pair{"Q4T_MOE_PARTITION", "0"},
        std::pair{"Q4T_MOE_CHUNK_ORDER", "0"},
        std::pair{"Q4T_MOE_STREAMS", "1"},
        std::pair{"Q4T_MOE_EVICT_WEIGHT", "0"}}) {
    const char* actual = std::getenv(name);
    if (!actual || std::strcmp(actual, expected) != 0) {
      std::printf("  required environment: %s=%s\n", name, expected);
      return false;
    }
  }
  if (!CudaAvailable()) Q4T_SKIP("no CUDA; required runner rejects skip");
  if (!FileExists(kIndex)) Q4T_SKIP("no model; required runner rejects skip");
  const MirrorRecycleEnvGuard workers("Q4T_MOE_LOAD_THREADS", "1");
  const MirrorRecycleEnvGuard l2("Q4T_MOE_L2_SLOTS", "8");
  const MirrorRecycleEnvGuard mirror("Q4T_MOE_MIRROR_K", "8");
  const MirrorRecycleEnvGuard inline_limit("Q4T_MOE_INLINE_MISS_LIMIT", "0");
  const MirrorRecycleEnvGuard observer("Q4T_MOE_SUPPLY_OBSERVER", "0");
  PartitionRealWeights weights;
  Q4T_CHECK(weights.Init());
  using q4t::model::MoEPartitionLogPhase;
  using q4t::model::MoERequestPartition;
  Q4T_CHECK(RunMirrorRecycleCase(
      weights, "single-decode", MoEPartitionLogPhase::kSingleDecode, {}, true));
  Q4T_CHECK(RunMirrorRecycleCase(weights, "unknown-singleton",
                                 MoEPartitionLogPhase::kUnknown, {}, false));
  // An inactive request identity still identifies prefill. Even an explicit
  // decode phase cannot override HasRequest; no partition mode is changed.
  Q4T_CHECK(RunMirrorRecycleCase(
      weights, "prefill-singleton", MoEPartitionLogPhase::kSingleDecode,
      MoERequestPartition(8193, "mirror-prefill", false).WithBase(8192),
      false));
  return true;
}
