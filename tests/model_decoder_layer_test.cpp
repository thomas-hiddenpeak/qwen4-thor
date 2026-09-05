// Test for the complete decoder layer (model/decoder_layer.h) against the
// real checkpoint. Loads layer-0 (linear_attention, no PLE) and verifies the
// orchestration (HC mix -> attn -> HC combine -> HC mix -> MoE -> HC combine)
// by two independent routes from the same zero-initialized state:
//   A: DecoderLayerForward (the production path)
//   B: manual step-by-step calls to the submodules with an independently
//      allocated workspace layout
// Both routes use the same kernels + weights + input, so their outputs must
// match (validates the layer's wiring, workspace carving, and ordering). The
// individual submodule numerics are covered by their own tests
// (hyperconnection / linear_attention / moe). Skipped when CUDA or the model
// is absent.
#include "q4t/io/weight_loader.h"
#include "q4t/model/decoder_layer.h"
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
using q4t::model::DecoderLayer;
using q4t::model::DecoderLayerForward;
using q4t::model::DecoderLayerWorkspaceBytes;
using q4t::model::HyperConnectionCombine;
using q4t::model::HyperConnectionMix;
using q4t::model::LinearAttentionForward;
using q4t::model::LoadDecoderLayer;
using q4t::model::MoEForward;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const int kLayer = 0;  // linear_attention, no PLE
const int kHs = 2560;
const int kHc = 4;
const int kLowrank = 320;
const float kEps = 1e-6f;
const int kE = 512;
const int kMoeIs = 640;
const int kSharedIs = 640;
const int kTopK = 10;
const int kT = 4;
const int kMaxLen = 256;

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
float L2RelErr(const std::vector<float>& a, const std::vector<float>& b) {
  double num = 0.0, den = 0.0;
  for (size_t i = 0; i < a.size(); ++i) {
    const double d = double(a[i]) - double(b[i]);
    num += d * d;
    den += double(b[i]) * double(b[i]);
  }
  return float(std::sqrt(num) / (std::sqrt(den) + 1e-6));
}
std::vector<float> FromBf16(const std::vector<uint16_t>& v) {
  std::vector<float> out(v.size());
  for (size_t i = 0; i < v.size(); ++i) out[i] = Bf16ToFloat(v[i]);
  return out;
}

// Reset the linear-attention persistent state to zero (fresh sequence).
void ResetLinearState(DecoderLayer& layer, cudaStream_t stream) {
  if (layer.ssm_state)
    cudaMemsetAsync(layer.ssm_state, 0,
                    static_cast<size_t>(48) * 128 * 128 * 2, stream);
  if (layer.conv_state)
    cudaMemsetAsync(layer.conv_state, 0,
                    static_cast<size_t>(10240) * 3 * 2, stream);
}

}  // namespace

Q4T_TEST(decoder_layer_forward) {
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

  DecoderLayer layer;
  s = LoadDecoderLayer(*loader, kLayer, kHs, kHc, kLowrank, kEps, kE, kMoeIs,
                       kSharedIs, kTopK, kMaxLen, &layer, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }
  if (layer.is_full_attention) {
    std::printf("  (unexpected: layer 0 is full_attention)\n");
    layer.Free();
    return false;
  }

  // Random hyper_input [T, hc*hs] (the trunk residual).
  const int hc_dim = kHc * kHs;
  std::mt19937 rng(31337);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  std::vector<uint16_t> hyper_in(static_cast<size_t>(kT) * hc_dim);
  for (auto& v : hyper_in) {
    const __nv_bfloat16 b = __float2bfloat16(dist(rng));
    v = *reinterpret_cast<const uint16_t*>(&b);
  }
  uint16_t* d_hyper = nullptr;
  uint16_t* d_outA = nullptr;
  uint16_t* d_outB = nullptr;
  if (cudaMalloc(&d_hyper, hyper_in.size() * 2) != cudaSuccess ||
      cudaMalloc(&d_outA, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess ||
      cudaMalloc(&d_outB, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    layer.Free();
    return false;
  }
  cudaMemcpy(d_hyper, hyper_in.data(), hyper_in.size() * 2,
             cudaMemcpyHostToDevice);

  // ---- Route A: DecoderLayerForward (production) ----
  const size_t wsA = DecoderLayerWorkspaceBytes(kT, false, kHs, kE, kMoeIs,
                                                kSharedIs, kTopK);
  void* d_wsA = nullptr;
  if (cudaMalloc(&d_wsA, wsA) != cudaSuccess) {
    std::printf("  cudaMalloc wsA failed\n");
    layer.Free();
    return false;
  }
  ResetLinearState(layer, nullptr);
  s = DecoderLayerForward(layer, d_hyper, d_outA, nullptr, kT, d_wsA, wsA,
                          nullptr);
  if (!s.ok()) {
    std::printf("  route A failed: %s\n", s.message().c_str());
    layer.Free();
    return false;
  }

  // ---- Route B: manual step-by-step (independent workspace layout) ----
  ResetLinearState(layer, nullptr);
  const size_t kGemm = 32u * 1024u * 1024u;
  const size_t attn_ws = kGemm;  // linear: GEMM scratch only
  const size_t moe_carve = q4t::model::MoEForwardWorkspaceBytes(
      kT, kTopK, kHs, kMoeIs, kSharedIs, kE);
  void* d_hc_ws = nullptr;
  void* d_attn_ws = nullptr;
  void* d_moe_ws = nullptr;
  void* d_moe_gemm = nullptr;
  uint16_t* d_mixed = nullptr;
  uint16_t* d_block = nullptr;
  uint16_t* d_res_a = nullptr;
  uint16_t* d_res_m = nullptr;
  uint16_t* d_combined = nullptr;
  if (cudaMalloc(&d_hc_ws, kGemm) != cudaSuccess ||
      cudaMalloc(&d_attn_ws, attn_ws) != cudaSuccess ||
      cudaMalloc(&d_moe_ws, moe_carve) != cudaSuccess ||
      cudaMalloc(&d_moe_gemm, kGemm) != cudaSuccess ||
      cudaMalloc(&d_mixed, static_cast<size_t>(kT) * kHs * 2) !=
          cudaSuccess ||
      cudaMalloc(&d_block, static_cast<size_t>(kT) * kHs * 2) !=
          cudaSuccess ||
      cudaMalloc(&d_res_a, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess ||
      cudaMalloc(&d_res_m, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess ||
      cudaMalloc(&d_combined, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess) {
    std::printf("  cudaMalloc route B failed\n");
    layer.Free();
    return false;
  }
  s = HyperConnectionMix(layer.attn_hc, d_hyper, d_mixed, d_res_a, kT,
                         d_hc_ws, kGemm, nullptr);
  if (!s.ok()) {
    std::printf("  B mix1: %s\n", s.message().c_str());
    return false;
  }
  s = LinearAttentionForward(layer.linear, d_mixed, d_block, layer.ssm_state,
                             layer.conv_state, kT, d_attn_ws, attn_ws, nullptr);
  if (!s.ok()) {
    std::printf("  B attn: %s\n", s.message().c_str());
    return false;
  }
  s = HyperConnectionCombine(layer.attn_hc, d_block, d_hyper, d_res_a,
                             d_combined, kT, d_hc_ws, kGemm, nullptr);
  if (!s.ok()) {
    std::printf("  B combine1: %s\n", s.message().c_str());
    return false;
  }
  s = HyperConnectionMix(layer.mlp_hc, d_combined, d_mixed, d_res_m, kT,
                         d_hc_ws, kGemm, nullptr);
  if (!s.ok()) {
    std::printf("  B mix2: %s\n", s.message().c_str());
    return false;
  }
  s = MoEForward(d_mixed, layer.routed, layer.mlp, d_block, kT, kTopK,
                 d_moe_ws, moe_carve, d_moe_gemm, kGemm, nullptr);
  if (!s.ok()) {
    std::printf("  B moe: %s\n", s.message().c_str());
    return false;
  }
  s = HyperConnectionCombine(layer.mlp_hc, d_block, d_combined, d_res_m,
                             d_outB, kT, d_hc_ws, kGemm, nullptr);
  if (!s.ok()) {
    std::printf("  B combine2: %s\n", s.message().c_str());
    return false;
  }

  // ---- Compare A vs B ----
  std::vector<uint16_t> outA(static_cast<size_t>(kT) * hc_dim);
  std::vector<uint16_t> outB(static_cast<size_t>(kT) * hc_dim);
  cudaMemcpy(outA.data(), d_outA, outA.size() * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(outB.data(), d_outB, outB.size() * 2, cudaMemcpyDeviceToHost);
  const float err = L2RelErr(FromBf16(outA), FromBf16(outB));
  std::printf("  decoder_layer A-vs-B l2_rel_err = %.3e\n", err);

  // Cleanup.
  cudaFree(d_hyper);
  cudaFree(d_outA);
  cudaFree(d_outB);
  cudaFree(d_wsA);
  cudaFree(d_hc_ws);
  cudaFree(d_attn_ws);
  cudaFree(d_moe_ws);
  cudaFree(d_moe_gemm);
  cudaFree(d_mixed);
  cudaFree(d_block);
  cudaFree(d_res_a);
  cudaFree(d_res_m);
  cudaFree(d_combined);
  layer.Free();

  // Both routes use identical kernels/weights/input from the same state, so
  // the outputs must match to BF16 rounding (essentially exact).
  Q4T_CHECK(err < 1e-3f);
  return true;
}
