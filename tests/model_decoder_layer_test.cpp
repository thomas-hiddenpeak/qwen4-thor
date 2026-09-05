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
using q4t::model::PleLayerForward;

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
uint16_t FloatToBf16(float f) {
  const __nv_bfloat16 b = __float2bfloat16_rn(f);
  return *reinterpret_cast<const uint16_t*>(&b);
}

// Reset the linear-attention persistent state to zero (fresh sequence).
void ResetLinearState(DecoderLayer& layer, cudaStream_t stream) {
  if (layer.ssm_state)
    cudaMemsetAsync(layer.ssm_state, 0,
                    static_cast<size_t>(48) * 128 * 128 * 4, stream);
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
  const size_t wsA = DecoderLayerWorkspaceBytes(kT, false, false, kHs, kE,
                                                kMoeIs, kSharedIs, kTopK);
  void* d_wsA = nullptr;
  if (cudaMalloc(&d_wsA, wsA) != cudaSuccess) {
    std::printf("  cudaMalloc wsA failed\n");
    layer.Free();
    return false;
  }
  ResetLinearState(layer, nullptr);
  s = DecoderLayerForward(layer, d_hyper, nullptr, d_outA, nullptr, kT, d_wsA,
                          wsA, nullptr);
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

// Test PLE injection in the decoder layer (model/decoder_layer.h). Loads
// layer-1 (linear_attention + PLE, checkpoint ple_layer_ids=[2] is 1-indexed)
// and verifies the PLE injection wiring (hyper_input += ple(emb, hyper_input)
// before the attention HC mix) by two independent routes from the same
// zero-initialized state:
//   A: DecoderLayerForward (production, with ple_embeddings)
//   B: manual PleLayerForward + elementwise add + step-by-step submodules
// Both use the same kernels/weights/input, so outputs must match (validates
// the PLE injection wiring, workspace carving, and trunk plumbing). The PLE
// layer numerics are covered by model_ple_layer_test. Skipped when CUDA or the
// model is absent.
Q4T_TEST(decoder_layer_ple_injection) {
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

  const int kPleLayer = 1;  // linear_attention + PLE
  const int hc_dim = kHc * kHs;
  DecoderLayer layer;
  s = LoadDecoderLayer(*loader, kPleLayer, kHs, kHc, kLowrank, kEps, kE,
                       kMoeIs, kSharedIs, kTopK, kMaxLen, &layer, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }
  if (!layer.has_ple) {
    std::printf("  (unexpected: layer 1 has no PLE)\n");
    layer.Free();
    return false;
  }

  // Random inputs: hyper_input [T, hc*hs], ple_embeddings [T, ple_embed_dim].
  std::mt19937 rng(777);
  std::normal_distribution<float> dist(0.0f, 0.5f);
  const int pe = 2560;
  std::vector<float> hyper(kT * hc_dim), emb(kT * pe);
  for (auto& v : hyper) v = dist(rng);
  for (auto& v : emb) v = dist(rng);
  std::vector<uint16_t> hyper_bf(hyper.size()), emb_bf(emb.size());
  for (size_t i = 0; i < hyper.size(); ++i) hyper_bf[i] = FloatToBf16(hyper[i]);
  for (size_t i = 0; i < emb.size(); ++i) emb_bf[i] = FloatToBf16(emb[i]);

  uint16_t* d_hyper = nullptr, *d_emb = nullptr;
  uint16_t* d_outA = nullptr, *d_outB = nullptr;
  void* d_wsA = nullptr;
  if (cudaMalloc(&d_hyper, hyper_bf.size() * 2) != cudaSuccess ||
      cudaMalloc(&d_emb, emb_bf.size() * 2) != cudaSuccess ||
      cudaMalloc(&d_outA, static_cast<size_t>(kT) * hc_dim * 2) != cudaSuccess ||
      cudaMalloc(&d_outB, static_cast<size_t>(kT) * hc_dim * 2) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    layer.Free();
    return false;
  }
  cudaMemcpy(d_hyper, hyper_bf.data(), hyper_bf.size() * 2,
             cudaMemcpyHostToDevice);
  cudaMemcpy(d_emb, emb_bf.data(), emb_bf.size() * 2, cudaMemcpyHostToDevice);

  // ---- Route A: DecoderLayerForward (production, with PLE) ----
  const size_t wsA = DecoderLayerWorkspaceBytes(kT, false, true, kHs, kE, kMoeIs,
                                                kSharedIs, kTopK);
  if (cudaMalloc(&d_wsA, wsA) != cudaSuccess) {
    std::printf("  cudaMalloc wsA failed\n");
    layer.Free();
    return false;
  }
  ResetLinearState(layer, nullptr);
  s = DecoderLayerForward(layer, d_hyper, d_emb, d_outA, nullptr, kT, d_wsA,
                          wsA, nullptr);
  if (!s.ok()) {
    std::printf("  route A failed: %s\n", s.message().c_str());
    layer.Free();
    return false;
  }

  // ---- Route B: manual PLE + add + step-by-step submodules ----
  ResetLinearState(layer, nullptr);
  const size_t kGemm = 32u * 1024u * 1024u;
  const size_t attn_ws = kGemm;  // linear: GEMM scratch only
  const size_t moe_carve = q4t::model::MoEForwardWorkspaceBytes(
      kT, kTopK, kHs, kMoeIs, kSharedIs, kE);
  // PLE workspace (mirrors PleLayerForward's carve).
  const size_t ple_ws = q4t::model::PleLayerWorkspaceBytes(kT, kHc, kHs);
  void* d_hc_ws = nullptr;
  void* d_attn_ws = nullptr;
  void* d_moe_ws = nullptr;
  void* d_moe_gemm = nullptr;
  void* d_ple_ws = nullptr;
  uint16_t* d_mixed = nullptr;
  uint16_t* d_block = nullptr;
  uint16_t* d_res_a = nullptr;
  uint16_t* d_res_m = nullptr;
  uint16_t* d_combined = nullptr;
  uint16_t* d_ple_out = nullptr;
  uint16_t* d_trunk = nullptr;
  if (cudaMalloc(&d_hc_ws, kGemm) != cudaSuccess ||
      cudaMalloc(&d_attn_ws, attn_ws) != cudaSuccess ||
      cudaMalloc(&d_moe_ws, moe_carve) != cudaSuccess ||
      cudaMalloc(&d_moe_gemm, kGemm) != cudaSuccess ||
      cudaMalloc(&d_ple_ws, ple_ws) != cudaSuccess ||
      cudaMalloc(&d_mixed, static_cast<size_t>(kT) * kHs * 2) != cudaSuccess ||
      cudaMalloc(&d_block, static_cast<size_t>(kT) * kHs * 2) != cudaSuccess ||
      cudaMalloc(&d_res_a, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess ||
      cudaMalloc(&d_res_m, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess ||
      cudaMalloc(&d_combined, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess ||
      cudaMalloc(&d_ple_out, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess ||
      cudaMalloc(&d_trunk, static_cast<size_t>(kT) * hc_dim * 2) !=
          cudaSuccess) {
    std::printf("  cudaMalloc route B failed\n");
    layer.Free();
    return false;
  }
  // PLE: d_ple_out = ple(emb, hyper).
  s = PleLayerForward(layer.ple, d_emb, d_hyper, d_ple_out, kT, d_ple_ws,
                      ple_ws, nullptr);
  if (!s.ok()) {
    std::printf("  B ple: %s\n", s.message().c_str());
    return false;
  }
  // trunk = hyper + ple_out (elementwise BF16 add). T is tiny (kT=4), so do
  // the add on the CPU to keep this test self-contained (no extra kernel).
  {
    std::vector<uint16_t> h_hyper(hyper_bf),
        h_ple(static_cast<size_t>(kT) * hc_dim);
    cudaMemcpy(h_ple.data(), d_ple_out, h_ple.size() * 2,
               cudaMemcpyDeviceToHost);
    std::vector<uint16_t> h_trunk(h_ple.size());
    for (size_t i = 0; i < h_trunk.size(); ++i) {
      uint32_t ba = static_cast<uint32_t>(h_hyper[i]) << 16;
      uint32_t bb = static_cast<uint32_t>(h_ple[i]) << 16;
      float fa, fb;
      std::memcpy(&fa, &ba, sizeof(fa));
      std::memcpy(&fb, &bb, sizeof(fb));
      const __nv_bfloat16 r = __float2bfloat16_rn(fa + fb);
      h_trunk[i] = *reinterpret_cast<const uint16_t*>(&r);
    }
    cudaMemcpy(d_trunk, h_trunk.data(), h_trunk.size() * 2,
               cudaMemcpyHostToDevice);
  }
  s = HyperConnectionMix(layer.attn_hc, d_trunk, d_mixed, d_res_a, kT, d_hc_ws,
                         kGemm, nullptr);
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
  s = HyperConnectionCombine(layer.attn_hc, d_block, d_trunk, d_res_a,
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
  std::printf("  decoder_layer PLE A-vs-B l2_rel_err = %.3e\n", err);

  cudaFree(d_hyper);
  cudaFree(d_emb);
  cudaFree(d_outA);
  cudaFree(d_outB);
  cudaFree(d_wsA);
  cudaFree(d_hc_ws);
  cudaFree(d_attn_ws);
  cudaFree(d_moe_ws);
  cudaFree(d_moe_gemm);
  cudaFree(d_ple_ws);
  cudaFree(d_mixed);
  cudaFree(d_block);
  cudaFree(d_res_a);
  cudaFree(d_res_m);
  cudaFree(d_combined);
  cudaFree(d_ple_out);
  cudaFree(d_trunk);
  layer.Free();

  Q4T_CHECK(err < 1e-3f);
  return true;
}
