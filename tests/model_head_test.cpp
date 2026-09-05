// Test for the model head/tail (model/model_head.h): embedding lookup, trunk
// expansion, and the closing mixer.mix + lm_head.
//
// Two tests:
//   model_head_load     : LoadModelHead on the real checkpoint succeeds
//                         (validates tensor names + shapes).
//   model_head_forward  : synthetic small weights; verify EmbedLookup,
//                         ExpandTrunk, and HeadForward (mixer.mix + lm_head)
//                         against a CPU reference. Synthetic weights keep this
//                         fast and avoid reading the 1.27 GB embed/lm_head to
//                         host; the Bf16Gemm and mixer.mix numerics are already
//                         covered by the HC / MoE tests.
// Skipped when CUDA is absent (load test also skips when the model is absent).
#include "q4t/io/weight_loader.h"
#include "q4t/model/model_head.h"
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
using q4t::model::EmbedLookup;
using q4t::model::ExpandTrunk;
using q4t::model::HeadForward;
using q4t::model::LoadModelHead;
using q4t::model::ModelHeadWeights;
using q4t::model::ModelHeadWorkspaceBytes;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

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
float Silu(float v) { return v / (1.0f + std::exp(-v)); }

// Allocate a device buffer and copy a host float vector into it as BF16.
uint16_t* AllocBf16(const std::vector<float>& h) {
  std::vector<uint16_t> bf(h.size());
  for (size_t i = 0; i < h.size(); ++i) bf[i] = FloatToBf16(h[i]);
  uint16_t* d = nullptr;
  cudaMalloc(reinterpret_cast<void**>(&d), h.size() * sizeof(uint16_t));
  cudaMemcpy(d, bf.data(), h.size() * sizeof(uint16_t),
             cudaMemcpyHostToDevice);
  return d;
}

}  // namespace

Q4T_TEST(model_head_load) {
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
  ModelHeadWeights w;
  s = LoadModelHead(*loader, 248320, 2560, 4, 320, 1e-6f, &w, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }
  std::printf("  model_head loaded (vocab=%d hs=%d hc=%d)\n", w.vocab, w.hs,
              w.hc);
  w.Free();
  return true;
}

Q4T_TEST(model_head_forward) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }

  // Synthetic small dims.
  const int vocab = 64, hs = 32, hc = 4, lowrank = 8;
  const int hc_dim = hc * hs;
  const float eps = 1e-6f;
  const int T = 4;

  std::mt19937 rng(999);
  std::normal_distribution<float> dist(0.0f, 0.5f);
  auto randvec = [&](size_t n) {
    std::vector<float> v(n);
    for (auto& x : v) x = dist(rng);
    return v;
  };
  std::vector<float> h_embed = randvec(vocab * hs);
  std::vector<float> h_lm = randvec(vocab * hs);
  std::vector<float> h_hc_norm = randvec(hc_dim);
  std::vector<float> h_mix_down = randvec(lowrank * hc_dim);
  std::vector<float> h_mix_up = randvec(hc_dim * lowrank);

  ModelHeadWeights w;
  w.vocab = vocab;
  w.hs = hs;
  w.hc = hc;
  w.hc_dim = hc_dim;
  w.embed_tokens = AllocBf16(h_embed);
  w.lm_head = AllocBf16(h_lm);
  w.mixer.hc_count = hc;
  w.mixer.hidden_size = hs;
  w.mixer.lowrank = lowrank;
  w.mixer.eps = eps;
  w.mixer.use_combine = false;
  w.mixer.hc_norm = AllocBf16(h_hc_norm);
  w.mixer.mix_down = AllocBf16(h_mix_down);
  w.mixer.mix_up = AllocBf16(h_mix_up);
  w.mixer.block_inject = nullptr;

  // ---- EmbedLookup ----
  std::vector<int32_t> ids(T);
  for (int t = 0; t < T; ++t) ids[t] = (t * 13 + 5) % vocab;
  int32_t* d_ids = nullptr;
  uint16_t* d_emb = nullptr, *d_trunk = nullptr;
  uint16_t* d_logits = nullptr;
  void* d_ws = nullptr;
  cudaMalloc(reinterpret_cast<void**>(&d_ids), T * sizeof(int32_t));
  cudaMalloc(reinterpret_cast<void**>(&d_emb), size_t(T) * hs * 2);
  cudaMalloc(reinterpret_cast<void**>(&d_trunk), size_t(T) * hc_dim * 2);
  cudaMalloc(reinterpret_cast<void**>(&d_logits), size_t(T) * vocab * 2);
  cudaMalloc(reinterpret_cast<void**>(&d_ws),
             ModelHeadWorkspaceBytes(T, hs));
  cudaMemcpy(d_ids, ids.data(), T * sizeof(int32_t), cudaMemcpyHostToDevice);

  Status s = EmbedLookup(w, d_ids, d_emb, T, nullptr);
  if (!s.ok()) {
    std::printf("  embed failed: %s\n", s.message().c_str());
    return false;
  }
  std::vector<uint16_t> emb_bf(size_t(T) * hs);
  cudaMemcpy(emb_bf.data(), d_emb, emb_bf.size() * 2, cudaMemcpyDeviceToHost);
  std::vector<float> emb = FromBf16(emb_bf);
  // CPU reference: emb[t, c] = embed[ids[t], c].
  std::vector<float> emb_ref(size_t(T) * hs);
  for (int t = 0; t < T; ++t)
    for (int c = 0; c < hs; ++c)
      emb_ref[t * hs + c] = h_embed[ids[t] * hs + c];
  const float emb_err = L2RelErr(emb, emb_ref);
  std::printf("  embed l2_rel_err = %.3e\n", emb_err);

  // ---- ExpandTrunk ----
  s = ExpandTrunk(w, d_emb, d_trunk, T, nullptr);
  if (!s.ok()) {
    std::printf("  expand failed: %s\n", s.message().c_str());
    return false;
  }
  std::vector<uint16_t> trunk_bf(size_t(T) * hc_dim);
  cudaMemcpy(trunk_bf.data(), d_trunk, trunk_bf.size() * 2,
             cudaMemcpyDeviceToHost);
  std::vector<float> trunk = FromBf16(trunk_bf);
  // CPU reference: trunk[t, b*hs+c] = emb[t, c].
  std::vector<float> trunk_ref(size_t(T) * hc_dim);
  for (int t = 0; t < T; ++t)
    for (int b = 0; b < hc; ++b)
      for (int c = 0; c < hs; ++c) trunk_ref[t * hc_dim + b * hs + c] =
          emb_ref[t * hs + c];
  const float trunk_err = L2RelErr(trunk, trunk_ref);
  std::printf("  expand l2_rel_err = %.3e\n", trunk_err);

  // ---- HeadForward (mixer.mix + lm_head) ----
  // Use the expanded trunk as the HeadForward input.
  s = HeadForward(w, d_trunk, d_logits, T, d_ws, ModelHeadWorkspaceBytes(T, hs),
                  nullptr);
  if (!s.ok()) {
    std::printf("  head failed: %s\n", s.message().c_str());
    return false;
  }
  std::vector<uint16_t> logits_bf(size_t(T) * vocab);
  cudaMemcpy(logits_bf.data(), d_logits, logits_bf.size() * 2,
             cudaMemcpyDeviceToHost);
  std::vector<float> logits = FromBf16(logits_bf);

  // CPU reference: mixer.mix(trunk) -> mixed [T, hs], then logits = mixed @
  // lm_head^T.
  std::vector<float> normed(size_t(T) * hc_dim);
  for (int t = 0; t < T; ++t) {
    for (int b = 0; b < hc; ++b) {
      double acc = 0.0;
      for (int c = 0; c < hs; ++c) {
        const double v = trunk[t * hc_dim + b * hs + c];
        acc += v * v;
      }
      const float rs = 1.0f / float(std::sqrt(acc / hs + eps));
      for (int c = 0; c < hs; ++c)
        normed[t * hc_dim + b * hs + c] =
            trunk[t * hc_dim + b * hs + c] * rs *
            (1.0f + h_hc_norm[b * hs + c]);
    }
  }
  std::vector<float> down(size_t(T) * lowrank);
  for (int t = 0; t < T; ++t)
    for (int r = 0; r < lowrank; ++r) {
      double acc = 0.0;
      for (int d = 0; d < hc_dim; ++d)
        acc += normed[t * hc_dim + d] * h_mix_down[r * hc_dim + d];
      down[t * lowrank + r] = float(acc);
    }
  std::vector<float> silu(size_t(T) * lowrank);
  for (size_t i = 0; i < silu.size(); ++i)
    silu[i] = Silu(down[i] / hc);
  std::vector<float> up(size_t(T) * hc_dim);
  for (int t = 0; t < T; ++t)
    for (int d = 0; d < hc_dim; ++d) {
      double acc = 0.0;
      for (int r = 0; r < lowrank; ++r)
        acc += silu[t * lowrank + r] * h_mix_up[d * lowrank + r];
      up[t * hc_dim + d] = float(acc);
    }
  std::vector<float> mixed(size_t(T) * hs);
  for (int t = 0; t < T; ++t)
    for (int c = 0; c < hs; ++c) {
      double acc = 0.0;
      for (int b = 0; b < hc; ++b) {
        const float g = 1.0f / (1.0f + std::exp(-up[t * hc_dim + b * hs + c]));
        acc += g * normed[t * hc_dim + b * hs + c];
      }
      mixed[t * hs + c] = float(acc) / hc;
    }
  std::vector<float> logits_ref(size_t(T) * vocab);
  for (int t = 0; t < T; ++t)
    for (int v = 0; v < vocab; ++v) {
      double acc = 0.0;
      for (int c = 0; c < hs; ++c) acc += mixed[t * hs + c] * h_lm[v * hs + c];
      logits_ref[t * vocab + v] = float(acc);
    }
  const float logits_err = L2RelErr(logits, logits_ref);
  std::printf("  head logits l2_rel_err = %.3e\n", logits_err);

  cudaFree(d_ids);
  cudaFree(d_emb);
  cudaFree(d_trunk);
  cudaFree(d_logits);
  cudaFree(d_ws);
  w.Free();

  // embed/expand are exact gather/copy; their only error is the BF16 rounding
  // of the synthetic weights (~1e-3). logits add the mixer.mix + lm_head GEMM
  // (BF16, FP32 accumulate).
  Q4T_CHECK(emb_err < 1e-2f);
  Q4T_CHECK(trunk_err < 1e-2f);
  Q4T_CHECK(logits_err < 3e-2f);
  return true;
}
