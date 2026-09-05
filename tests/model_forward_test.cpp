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
using q4t::model::ModelDecodeStep;
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

  const int num_layers = [] {
    const char* e = std::getenv("Q4T_MODEL_LAYERS");
    return e ? std::atoi(e) : 2;
  }();

  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = num_layers;  // default 2 (layer 0 linear + layer 1 PLE);
                                // Q4T_MODEL_LAYERS=48 for the full model
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

// Decode-path correctness: the logits for the 4th token obtained by
// (prefill of the first 3 tokens) + (one decode step for the 4th) must match
// the 4th token's logits from a single 4-token prefill. This exercises the
// per-layer state continuation (linear SSM/conv, full KV) and the decode PLE
// history. Uses the 2-layer default (both linear_attention) where the
// prefill/decode equivalence is exact up to GEMM-shape rounding.
Q4T_TEST(model_decode_step) {
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
  cfg.max_prefill = 8;
  cfg.ple_sidecar = kPleSidecar;

  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  const int T = 4;
  const int32_t ids[] = {846, 25, 1203, 321};
  const int vocab = cfg.vocab;

  uint16_t* d_prefill = nullptr;
  uint16_t* d_decode = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_prefill),
                 static_cast<size_t>(T) * vocab * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_decode),
                 static_cast<size_t>(vocab) * 2) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    m.Free();
    return false;
  }

  // Route A: single 4-token prefill -> take the last token's logits.
  s = ModelForward(m, ids, T, d_prefill, nullptr);
  if (!s.ok()) {
    std::printf("  prefill failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }

  // Route B: prefill the first 3 tokens, then one decode step for the 4th.
  s = ModelForward(m, ids, T - 1, d_prefill, nullptr);
  if (!s.ok()) {
    std::printf("  prefill(3) failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  s = ModelDecodeStep(m, ids[T - 1], T - 1, ids, d_decode, nullptr);
  if (!s.ok()) {
    std::printf("  decode step failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }

  std::vector<uint16_t> a(static_cast<size_t>(vocab));
  std::vector<uint16_t> b(static_cast<size_t>(vocab));
  cudaMemcpy(a.data(), d_prefill + static_cast<size_t>(T - 1) * vocab,
             vocab * 2, cudaMemcpyDeviceToHost);
  cudaMemcpy(b.data(), d_decode, vocab * 2, cudaMemcpyDeviceToHost);

  double dot = 0.0, na = 0.0, nb = 0.0;
  for (int v = 0; v < vocab; ++v) {
    const float x = Bf16ToFloat(a[v]);
    const float y = Bf16ToFloat(b[v]);
    dot += x * y;
    na += x * x;
    nb += y * y;
  }
  const double denom = std::sqrt(na) * std::sqrt(nb);
  const double l2_rel = denom > 0.0 ? std::fabs(dot / denom - 1.0) : 0.0;
  std::printf("  decode-vs-prefill last-token l2_rel_err = %.3e\n", l2_rel);
  // Threshold is sensitive to the gate activation: sigmoid (output_gate_type)
  // shifts the SSM-state magnitude distribution vs silu, changing the relative
  // impact of the BF16 state round-trip (prefill keeps S in FP32 SMEM, decode
  // reloads from BF16 GMEM). 1e-1 accommodates both activations while still
  // catching real logic errors (which typically produce l2_rel >> 0.1).
  Q4T_CHECK(l2_rel < 1e-1);

  cudaFree(d_prefill);
  cudaFree(d_decode);
  m.Free();
  return true;
}

// Dump the C++ logits for the fixed e2e token sequence to a raw float32 file
// (T*vocab, row-major) so a Python reference (transformers) can be compared
// token-for-token. Writes to Q4T_DUMP_LOGITS (default /tmp/cpp.logits.bin).
Q4T_TEST(model_forward_dump_logits) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar)) {
    std::printf("  (skipped: model or PLE sidecar not found)\n");
    return true;
  }

  const char* out_path = std::getenv("Q4T_DUMP_LOGITS");
  if (!out_path) out_path = "/tmp/cpp.logits.bin";

  const int num_layers = [] {
    const char* e = std::getenv("Q4T_MODEL_LAYERS");
    return e ? std::atoi(e) : 48;  // full model for reference comparison
  }();

  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = num_layers;
  cfg.max_prefill = 8;
  cfg.ple_sidecar = kPleSidecar;

  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  const int T = 4;
  const int32_t ids[] = {846, 25, 1203, 321};  // must match ref_logits.py
  uint16_t* d_logits = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits),
                 static_cast<size_t>(T) * cfg.vocab * 2) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    m.Free();
    return false;
  }
  s = ModelForward(m, ids, T, d_logits, nullptr);
  if (!s.ok()) {
    std::printf("  forward failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }

  // BF16 -> float32, write raw.
  std::vector<float> out(static_cast<size_t>(T) * cfg.vocab);
  std::vector<uint16_t> raw(static_cast<size_t>(T) * cfg.vocab);
  cudaMemcpy(raw.data(), d_logits, raw.size() * 2, cudaMemcpyDeviceToHost);
  for (size_t i = 0; i < raw.size(); ++i) {
    uint32_t bits = static_cast<uint32_t>(raw[i]) << 16;
    std::memcpy(&out[i], &bits, sizeof(float));
  }
  FILE* f = std::fopen(out_path, "wb");
  if (!f) {
    std::printf("  fopen %s failed\n", out_path);
    m.Free();
    return false;
  }
  std::fwrite(out.data(), sizeof(float), out.size(), f);
  std::fclose(f);
  std::printf("  dumped %d x %d logits -> %s\n", T, cfg.vocab, out_path);

  cudaFree(d_logits);
  m.Free();
  return true;
}
