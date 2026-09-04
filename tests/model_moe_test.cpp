// Test for the complete MoE MLP (model/moe.h), against the real checkpoint.
// Loads the layer-2 routed NVFP4 experts + BF16 router/shared-expert weights,
// runs MoEForward on random [T, hs] input, and compares against a full CPU
// reference that mirrors the router top-k (softmax over the selected k), the
// NVFP4 routed experts (dequant + SwiGLU), the BF16 shared expert, and the
// gated combine. Skipped (reported as pass) when CUDA or the model is absent.
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
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <map>
#include <random>
#include <string>
#include <unistd.h>
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
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  WeightIndex* idx = nullptr;
  WeightLoader* loader = nullptr;
  if (!FileExists(kIndex) || !WeightIndex::Open(kIndex, &idx).ok() ||
      !WeightLoader::Create(kModelDir, *idx, 16, &loader).ok()) {
    std::printf("  (skipped: real model not present)\n");
    return true;
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
