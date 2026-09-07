// Test for multimodal vision-feature injection into the main model prefill.
//
// The vision tower (q4t_vision) emits [num_image_tokens, hs] BF16 features;
// the main model replaces every input_ids position equal to image_token_id
// with the next feature row (vllm `_merge_multimodal_embeddings`). This test
// exercises that injection path end to end against the real model (2 layers,
// including the PLE layer):
//   1. count mismatch: providing the wrong number of features must fail.
//   2. injection effect: injecting features must change the logits (the image
//      token embeddings are actually replaced), while a pure-text run
//      (vision=nullptr) leaves them at the baseline.
//   3. determinism: two identical injection runs are bit-identical.
//
// There is no full-stack CPU reference yet, so this is a mechanism smoke test
// (finite / non-trivial / deterministic / injection actually takes effect).
// Skipped when CUDA or the model is absent.
#include "q4t/model/model.h"
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
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::model::LoadModel;
using q4t::model::Model;
using q4t::model::ModelConfig;
using q4t::model::ModelForward;
using q4t::model::VisionFeatures;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";
const char* kPleSidecar =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "ple/qwen3.8-flash-next-ple-fp8.bin";

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
// Max absolute difference between two BF16 logit buffers (host).
double MaxAbsDiff(const std::vector<uint16_t>& a, const std::vector<uint16_t>& b) {
  double m = 0.0;
  for (size_t i = 0; i < a.size(); ++i) {
    m = std::max(m,
                 static_cast<double>(
                     std::fabs(Bf16ToFloat(a[i]) - Bf16ToFloat(b[i]))));
  }
  return m;
}

}  // namespace

Q4T_TEST(model_vision_inject) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar)) {
    std::printf("  (skipped: model or PLE sidecar not found)\n");
    return true;
  }

  const int num_layers = [] {
    const char* e = std::getenv("Q4T_MODEL_LAYERS");
    return e ? std::atoi(e) : 2;
  }();

  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = num_layers;
  cfg.max_prefill = 16;
  cfg.ple_sidecar = kPleSidecar;

  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  // Sequence with two <image> placeholders (positions 1 and 3).
  const int T = 6;
  const int32_t ids[] = {846, 248056, 25, 248056, 1203, 321};
  const int num_img = 2;

  uint16_t* d_logits = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits),
                 static_cast<size_t>(T) * cfg.vocab * 2) != cudaSuccess) {
    std::printf("  cudaMalloc logits failed\n");
    m.Free();
    return false;
  }
  std::vector<uint16_t> logits(static_cast<size_t>(T) * cfg.vocab);

  // --- 1. count mismatch must fail (provide 3 features for 2 image tokens).
  {
    std::vector<uint16_t> bad(static_cast<size_t>(3) * cfg.hs, 0x3c00);
    uint16_t* d_bad = nullptr;
    cudaMalloc(reinterpret_cast<void**>(&d_bad), bad.size() * 2);
    cudaMemcpy(d_bad, bad.data(), bad.size() * 2, cudaMemcpyHostToDevice);
    VisionFeatures vf;
    vf.device = d_bad;
    vf.num_tokens = 3;
    Status sm = ModelForward(m, ids, T, d_logits, nullptr, &vf);
    Q4T_CHECK(!sm.ok());  // mismatch -> error
    cudaFree(d_bad);
  }

  // --- 2. baseline (no vision) vs injected (must differ).
  std::vector<uint16_t> base;
  {
    s = ModelForward(m, ids, T, d_logits, nullptr, nullptr);
    Q4T_CHECK(s.ok());
    cudaMemcpy(logits.data(), d_logits, logits.size() * 2,
               cudaMemcpyDeviceToHost);
    base = logits;
  }

  // Deterministic synthetic features (a ramp, distinct per row).
  std::vector<uint16_t> feats(static_cast<size_t>(num_img) * cfg.hs);
  for (int i = 0; i < num_img; ++i) {
    for (int j = 0; j < cfg.hs; ++j) {
      const float v = 0.01f * (static_cast<float>(i * 1000 + j) - 500.0f);
      const __nv_bfloat16 b = __float2bfloat16_rn(v);
      feats[static_cast<size_t>(i) * cfg.hs + j] =
          *reinterpret_cast<const uint16_t*>(&b);
    }
  }
  uint16_t* d_feats = nullptr;
  cudaMalloc(reinterpret_cast<void**>(&d_feats), feats.size() * 2);
  cudaMemcpy(d_feats, feats.data(), feats.size() * 2, cudaMemcpyHostToDevice);
  VisionFeatures vf;
  vf.device = d_feats;
  vf.num_tokens = num_img;

  std::vector<uint16_t> inj1;
  s = ModelForward(m, ids, T, d_logits, nullptr, &vf);
  Q4T_CHECK(s.ok());
  cudaMemcpy(logits.data(), d_logits, logits.size() * 2,
             cudaMemcpyDeviceToHost);
  inj1 = logits;

  // Finite + non-trivial on the injected run.
  double max_abs = 0.0;
  bool all_finite = true;
  for (uint16_t b : inj1) {
    const float f = Bf16ToFloat(b);
    if (!std::isfinite(f)) all_finite = false;
    max_abs = std::max(max_abs, static_cast<double>(std::fabs(f)));
  }
  Q4T_CHECK(all_finite);
  Q4T_CHECK(max_abs > 1e-3);

  // Injection must actually change the logits (features replaced the image
  // token embeddings). The difference should be non-trivial.
  const double diff = MaxAbsDiff(base, inj1);
  std::printf("  baseline vs injected max_abs_diff = %.6f\n", diff);
  Q4T_CHECK(diff > 1e-3);

  // --- 3. determinism: a second identical injection run is bit-identical.
  std::vector<uint16_t> inj2;
  s = ModelForward(m, ids, T, d_logits, nullptr, &vf);
  Q4T_CHECK(s.ok());
  cudaMemcpy(logits.data(), d_logits, logits.size() * 2,
             cudaMemcpyDeviceToHost);
  inj2 = logits;
  Q4T_CHECK(MaxAbsDiff(inj1, inj2) == 0.0);

  cudaFree(d_feats);
  cudaFree(d_logits);
  m.Free();
  return true;
}
