// Test for the complete model forward (model/model.h). Loads the head + the
// first two decoder layers (layer 0 linear_attention + layer 1 linear+PLE,
// which exercises the PLE SSD-stream path against the real 51 GB sidecar) and
// runs a short prefill (T=4) end to end:
//   EmbedLookup -> ExpandTrunk -> [layer 0] -> [layer 1 + PLE gather] ->
//   HeadForward (mixer.mix + lm_head) -> logits [T, vocab].
//
// There is no CPU reference for the full stack yet (per-token verification vs
// the SGLang reference is deferred until the whole architecture is built), so
// this test is a smoke check: the forward completes, the logits are finite and
// non-trivially non-zero, and two runs from the same input are bit-identical
// (determinism). Skipped when CUDA or the model is absent.
#include "q4t/model/model.h"
#include "q4t/test.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
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

}  // namespace

Q4T_TEST(model_forward_e2e) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar)) {
    std::printf("  (skipped: model or PLE sidecar not found)\n");
    return true;
  }

  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = 2;  // layer 0 (linear) + layer 1 (linear + PLE)
  cfg.max_prefill = 8;
  cfg.ple_sidecar = kPleSidecar;

  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }
  std::printf("  model loaded: %d layers, ple_weight_scale=%.4f\n",
              cfg.num_layers, m.ple_weight_scale);

  const int T = 4;
  const int32_t ids[] = {846, 25, 1203, 321};  // arbitrary in-vocab tokens
  uint16_t* d_logits = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits),
                 static_cast<size_t>(T) * cfg.vocab * 2) != cudaSuccess) {
    std::printf("  cudaMalloc logits failed\n");
    m.Free();
    return false;
  }

  s = ModelForward(m, ids, T, d_logits, nullptr);
  if (!s.ok()) {
    std::printf("  forward failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  std::vector<uint16_t> logits(static_cast<size_t>(T) * cfg.vocab);
  cudaMemcpy(logits.data(), d_logits, logits.size() * 2,
             cudaMemcpyDeviceToHost);

  // Smoke checks: finite, non-trivially non-zero, argmax in vocab.
  double max_abs = 0.0;
  int argmax = -1;
  bool all_finite = true;
  for (int t = 0; t < T; ++t) {
    double best = -1e300;
    for (int v = 0; v < cfg.vocab; ++v) {
      const float x = Bf16ToFloat(logits[static_cast<size_t>(t) * cfg.vocab + v]);
      if (!std::isfinite(x)) all_finite = false;
      const double a = std::fabs(x);
      if (a > max_abs) max_abs = a;
      if (x > best) {
        best = x;
        argmax = v;
      }
    }
  }
  std::printf("  logits: max_abs=%.3f argmax(last)=%d finite=%d\n", max_abs,
              argmax, all_finite ? 1 : 0);
  Q4T_CHECK(all_finite);
  Q4T_CHECK(max_abs > 1e-3);  // non-trivial output

  // Determinism: a second run from the same input must be bit-identical.
  uint16_t* d_logits2 = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits2),
                 static_cast<size_t>(T) * cfg.vocab * 2) != cudaSuccess) {
    std::printf("  cudaMalloc logits2 failed\n");
    m.Free();
    return false;
  }
  s = ModelForward(m, ids, T, d_logits2, nullptr);
  Q4T_CHECK(s.ok());
  std::vector<uint16_t> logits2(static_cast<size_t>(T) * cfg.vocab);
  cudaMemcpy(logits2.data(), d_logits2, logits2.size() * 2,
             cudaMemcpyDeviceToHost);
  bool identical = logits == logits2;
  std::printf("  determinism: %s\n", identical ? "identical" : "DIFFER");
  Q4T_CHECK(identical);

  cudaFree(d_logits);
  cudaFree(d_logits2);
  m.Free();
  return true;
}
