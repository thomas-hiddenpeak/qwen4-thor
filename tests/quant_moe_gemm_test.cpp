// Test for the grouped NVFP4 MoE routed-expert forward (moe_gemm.h), against
// the real checkpoint. Builds a routing that exercises M_e = 1/2/3 (including
// a shared expert), runs MoERoutedForward, and compares against a full CPU
// reference that mirrors the FP4 quantization, SwiGLU, and router-weighted
// sum. Skipped (reported as pass) when CUDA or the real model is absent.
#include "q4t/io/weight_loader.h"
#include "q4t/quant/fp4_gemm.h"
#include "q4t/quant/format.h"
#include "q4t/quant/moe_gemm.h"
#include "q4t/quant/moe_weights.h"
#include "q4t/quant/swizzle.h"
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
using q4t::quant::E2m1ToFloat;
using q4t::quant::E4m3ToFloat;
using q4t::quant::FloatToE2m1Code;
using q4t::quant::FloatToE4m3;
using q4t::quant::LoadMoEWeights;
using q4t::quant::MoEWeightLayout;
using q4t::quant::MoERoutedForward;
using q4t::quant::MoEWorkspace;
using q4t::quant::SfOffset;
using q4t::quant::SfNumGtiles;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const int kE = 512;
const int kHs = 2560;
const int kMoeIs = 640;
const int kLayer = 2;

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

// Host NVFP4 quantization of a [rows, K] float buffer under the activation
// convention (e4m3 = round(block_scale / global_scale), e2m1 rounded against
// e4m3*global_scale). Returns packed [rows, K/2] and row-major sf [rows, K/16].
struct HostFp4 {
  std::vector<uint8_t> packed;
  std::vector<uint8_t> sf;  // row-major
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
        const int c0 =
            FloatToE2m1Code(v[static_cast<size_t>(r) * K + g * 16 + 2 * j] * inv);
        const int c1 = FloatToE2m1Code(
            v[static_cast<size_t>(r) * K + g * 16 + 2 * j + 1] * inv);
        h.packed[static_cast<size_t>(r) * (K / 2) + g * 8 + j] =
            static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
      }
    }
  }
  return h;
}

// Dequantize a host-quantized [rows, K] buffer: recon = e2m1 * e4m3 * global.
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

// Dequantize a device-loaded expert weight row (swizzled SF) to float [K].
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

}  // namespace

Q4T_TEST(moe_gemm_routed_forward_matches_reference) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  WeightIndex* idx = nullptr;
  WeightLoader* loader = nullptr;
  if (!FileExists(kIndex) ||
      !WeightIndex::Open(kIndex, &idx).ok() ||
      !WeightLoader::Create(kModelDir, *idx, 16, &loader).ok()) {
    std::printf("  (skipped: real model not present)\n");
    return true;
  }
  MoEWeightLayout w;
  Status s = LoadMoEWeights(*loader, kLayer, kE, kHs, kMoeIs, &w, 0);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  const int k = 3;  // top-k
  const int M = 4;
  // Routing: token -> expert. Counts: expert0={t0,t1} (M_e=2), expert1={t0,t2}
  // (M_e=2, shared with token0), expert2={t3} (M_e=1).
  const int32_t expert_ids[M * k] = {0, 1, 7,   // token 0
                                     0, 5, 9,   // token 1
                                     1, 3, 11,  // token 2
                                     2, 4, 13}; // token 3
  const float router_w[M * k] = {0.5f, 0.3f, 0.2f,
                                 0.6f, 0.25f, 0.15f,
                                 0.4f, 0.35f, 0.25f,
                                 0.7f, 0.15f, 0.15f};

  // Random BF16 activations [M, hs].
  std::mt19937 rng(2024);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  std::vector<float> x_f(static_cast<size_t>(M) * kHs);
  for (auto& v : x_f) v = dist(rng);
  std::vector<uint16_t> x_bf16(x_f.size());
  for (size_t i = 0; i < x_f.size(); ++i) {
    const __nv_bfloat16 b = __float2bfloat16(x_f[i]);
    x_bf16[i] = *reinterpret_cast<const uint16_t*>(&b);
    x_f[i] = __bfloat162float(b);  // what the GPU actually sees
  }

  // --- Device setup ---
  uint16_t* d_x = nullptr;
  int32_t* d_eid = nullptr;
  float* d_rw = nullptr;
  float* d_y = nullptr;
  cudaMalloc(&d_x, x_bf16.size() * 2);
  cudaMalloc(&d_eid, M * k * sizeof(int32_t));
  cudaMalloc(&d_rw, M * k * sizeof(float));
  cudaMalloc(&d_y, static_cast<size_t>(M) * kHs * sizeof(float));
  cudaMemcpy(d_x, x_bf16.data(), x_bf16.size() * 2, cudaMemcpyHostToDevice);
  cudaMemcpy(d_eid, expert_ids, M * k * sizeof(int32_t), cudaMemcpyHostToDevice);
  cudaMemcpy(d_rw, router_w, M * k * sizeof(float), cudaMemcpyHostToDevice);
  cudaMemset(d_y, 0, static_cast<size_t>(M) * kHs * sizeof(float));

  const size_t ws_bytes = MoEWorkspace::RequiredBytes(M, k, kHs, kMoeIs);
  uint8_t* d_ws = nullptr;
  cudaMalloc(&d_ws, ws_bytes);
  const size_t gemm_ws = 32 * 1024 * 1024;
  void* d_gemm_ws = nullptr;
  cudaMalloc(&d_gemm_ws, gemm_ws);

  s = MoERoutedForward(d_x, d_eid, d_rw, d_y, w, d_ws, d_gemm_ws, gemm_ws, M, k,
                       0);
  if (!s.ok()) {
    std::printf("  forward failed: %s\n", s.message().c_str());
    return false;
  }
  if (cudaDeviceSynchronize() != cudaSuccess) return false;
  std::vector<float> y(static_cast<size_t>(M) * kHs);
  cudaMemcpy(y.data(), d_y, y.size() * sizeof(float), cudaMemcpyDeviceToHost);

  // --- CPU reference ---
  const int num_gu_tiles = SfNumGtiles(kHs);
  const int num_dn_tiles = SfNumGtiles(kMoeIs);
  std::vector<uint8_t> gu_host(static_cast<size_t>(2 * kMoeIs) * (kHs / 2));
  std::vector<uint8_t> gu_sf_host(w.gu_sf_block());
  std::vector<uint8_t> dn_host(static_cast<size_t>(kHs) * (kMoeIs / 2));
  std::vector<uint8_t> dn_sf_host(w.dn_sf_block());

  // The GPU GEMM folds both global scales into alpha, which cancels: the
  // result is the true real-value matmul  W_real . A_real  where
  //   W_real = e2m1_w * e4m3_w * weight_scale_2
  //   A_real = e2m1_a * e4m3_a * input_scale
  // So the reference dequantizes weights with global_scale=weight_scale_2 and
  // activations with global_scale=input_scale, and does NOT apply any extra
  // alpha.
  std::vector<float> ref(static_cast<size_t>(M) * kHs, 0.0f);
  for (int t = 0; t < M; ++t) {
    for (int slot = 0; slot < k; ++slot) {
      const int e = expert_ids[t * k + slot];
      const float rw = router_w[t * k + slot];
      const float gu_in = w.gu_input_scale_h[e];
      const float dn_in = w.dn_input_scale_h[e];
      const float gu_ws2 = w.gu_w_scale2_h[e];
      const float dn_ws2 = w.dn_w_scale2_h[e];

      // Load this expert's weights.
      cudaMemcpy(gu_host.data(), w.gu_packed_expert(e), gu_host.size(),
                 cudaMemcpyDeviceToHost);
      cudaMemcpy(gu_sf_host.data(), w.gu_sf_expert(e), gu_sf_host.size(),
                 cudaMemcpyDeviceToHost);
      cudaMemcpy(dn_host.data(), w.dn_packed_expert(e), dn_host.size(),
                 cudaMemcpyDeviceToHost);
      cudaMemcpy(dn_sf_host.data(), w.dn_sf_expert(e), dn_sf_host.size(),
                 cudaMemcpyDeviceToHost);

      // Activation row t -> A_real.
      std::vector<float> a_row(x_f.begin() + static_cast<size_t>(t) * kHs,
                               x_f.begin() + static_cast<size_t>(t) * kHs + kHs);
      HostFp4 aq = HostQuantFp4(a_row, 1, kHs, gu_in);
      std::vector<float> a_dq = HostDequantFp4(aq, 1, kHs, gu_in);
      // h[n] = sum_k A_real[k] * W_gu_real[n, k].
      std::vector<float> h(2 * kMoeIs, 0.0f);
      for (int n = 0; n < 2 * kMoeIs; ++n) {
        std::vector<float> wrow = DequantWeightRow(gu_host.data(),
                                                   gu_sf_host.data(), n, kHs,
                                                   num_gu_tiles, gu_ws2);
        float acc = 0.0f;
        for (int kk = 0; kk < kHs; ++kk) acc += a_dq[kk] * wrow[kk];
        h[n] = acc;
      }
      // SwiGLU.
      std::vector<float> inter(kMoeIs);
      for (int c = 0; c < kMoeIs; ++c) {
        const float g = h[c];
        const float u = h[kMoeIs + c];
        const float sig = 1.0f / (1.0f + std::exp(-g));
        inter[c] = (g * sig) * u;
      }
      // down: out[c] = sum_kk inter_real[kk] * W_dn_real[c, kk].
      HostFp4 iq = HostQuantFp4(inter, 1, kMoeIs, dn_in);
      std::vector<float> i_dq = HostDequantFp4(iq, 1, kMoeIs, dn_in);
      for (int c = 0; c < kHs; ++c) {
        std::vector<float> wrow = DequantWeightRow(dn_host.data(),
                                                   dn_sf_host.data(), c, kMoeIs,
                                                   num_dn_tiles, dn_ws2);
        float acc = 0.0f;
        for (int kk = 0; kk < kMoeIs; ++kk) acc += i_dq[kk] * wrow[kk];
        ref[static_cast<size_t>(t) * kHs + c] += rw * acc;
      }
    }
  }

  // Compare.
  double max_abs = 0.0, max_rel = 0.0;
  for (int t = 0; t < M; ++t) {
    for (int c = 0; c < kHs; ++c) {
      const double got = y[static_cast<size_t>(t) * kHs + c];
      const double want = ref[static_cast<size_t>(t) * kHs + c];
      max_abs = std::fmax(max_abs, std::fabs(got - want));
      max_rel = std::fmax(max_rel,
                          std::fabs(got - want) / std::fmax(1.0, std::fabs(want)));
    }
  }
  std::printf("  MoE routed forward M=%d k=%d max_abs=%.5g max_rel=%.5g\n", M, k,
              max_abs, max_rel);
  const bool pass = max_rel < 0.05;

  cudaFree(d_x);
  cudaFree(d_eid);
  cudaFree(d_rw);
  cudaFree(d_y);
  cudaFree(d_ws);
  cudaFree(d_gemm_ws);
  if (w.gu_packed) cudaFree(w.gu_packed);
  if (w.gu_sf) cudaFree(w.gu_sf);
  if (w.dn_packed) cudaFree(w.dn_packed);
  if (w.dn_sf) cudaFree(w.dn_sf);
  if (w.gu_w_scale2) cudaFree(w.gu_w_scale2);
  if (w.gu_input_scale) cudaFree(w.gu_input_scale);
  if (w.dn_w_scale2) cudaFree(w.dn_w_scale2);
  if (w.dn_input_scale) cudaFree(w.dn_input_scale);
  delete loader;
  delete idx;
  return pass;
}
