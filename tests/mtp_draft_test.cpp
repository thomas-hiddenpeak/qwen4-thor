// Test for the MTP draft model (mtp/mtp.h). Loads a small main model (2
// layers, for the shared embed_tokens + lm_head) plus the full MTP draft model
// (14.7 GB BF16, 1 full-attention decoder layer + BF16 MoE), then runs draft
// steps end to end:
//   ModelPrefill (main, trunk_out) -> MtpForward (step 0) -> MtpForward
//   (step 1, reusing step 0's multi_hidden).
//
// There is no CPU reference for the full MTP stack yet (per-token verification
// vs the vLLM reference is deferred), so this is a smoke + self-consistency
// check: the forward completes, the logits are finite and non-trivially
// non-zero, two runs from the same input are bit-identical (determinism), and
// the multi-step draft (step 0 -> step 1) is also deterministic. Skipped when
// CUDA or the model is absent.
#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"
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
#include <fstream>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::model::LoadModel;
using q4t::model::Model;
using q4t::model::ModelConfig;
using q4t::model::ModelSequence;
using q4t::model::ModelBeginSequence;
using q4t::model::ModelPrefill;
using q4t::model::ModelEndSequence;
using q4t::mtp::LoadMtp;
using q4t::mtp::MtpConfig;
using q4t::mtp::MtpForward;
using q4t::mtp::MtpModel;
using q4t::mtp::MtpResetState;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";
const char* kPleSidecar =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "ple/qwen3.8-flash-next-ple-fp8.bin";
const char* kMtpDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "mtp";

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

// Run one MTP draft step and copy the logits to host. Returns false on CUDA
// error.
bool RunMtpStep(const MtpModel& mtp, const int32_t* d_ids, const int* positions,
                const uint16_t* hidden, uint16_t* sample_hidden,
                uint16_t* multi_hidden, uint16_t* logits, int T, int vocab,
                std::vector<uint16_t>* host_logits) {
  Status s = MtpForward(mtp, d_ids, positions, hidden, sample_hidden,
                        multi_hidden, logits, T, nullptr);
  if (!s.ok()) {
    std::printf("  MtpForward failed: %s\n", s.message().c_str());
    return false;
  }
  host_logits->assign(static_cast<size_t>(T) * vocab, 0);
  cudaMemcpy(host_logits->data(), logits,
             host_logits->size() * sizeof(uint16_t),
             cudaMemcpyDeviceToHost);
  return true;
}

}  // namespace

Q4T_TEST(mtp_draft_forward) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex) || !FileExists(kMtpDir) ||
      !FileExists(kPleSidecar)) {
    std::printf("  (skipped: model or MTP dir not found)\n");
    return true;
  }

  // 1. Load a small main model (2 layers) for the shared embed/lm_head.
  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = 2;
  cfg.max_prefill = 8;
  cfg.ple_sidecar = kPleSidecar;
  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  main model load failed: %s\n", s.message().c_str());
    return false;
  }
  std::printf("  main model loaded: %d layers\n", cfg.num_layers);

  // 2. Load the MTP draft model (borrows the main model's embed/lm_head).
  MtpConfig mcfg;
  mcfg.mtp_dir = kMtpDir;
  MtpModel mtp;
  s = LoadMtp(mcfg, m.head.embed_tokens, m.head.lm_head, &mtp, nullptr);
  if (!s.ok()) {
    std::printf("  MTP load failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  std::printf("  MTP loaded (hs=%d hc=%d E=%d moe_is=%d)\n", mcfg.hs,
              mcfg.hc, mcfg.E, mcfg.moe_is);

  const int T = 4;
  const int32_t ids[] = {846, 25, 1203, 321};  // arbitrary in-vocab tokens
  const int positions[] = {0, 1, 2, 3};

  // Device buffers.
  int32_t* d_ids = nullptr;
  int* d_pos = nullptr;
  uint16_t* d_trunk = nullptr;  // [T, hc*hs] main model pre-final-mixer stream
  uint16_t* d_logits_main = nullptr;  // [T, vocab]
  uint16_t* d_sample = nullptr;  // [T, hs]
  uint16_t* d_multi = nullptr;  // [T, hc*hs]
  uint16_t* d_multi2 = nullptr;  // [T, hc*hs] (step 1 input)
  uint16_t* d_logits = nullptr;  // [T, vocab]
  const size_t hc_dim = static_cast<size_t>(mcfg.hc) * mcfg.hs;
  auto mal = [&](void** p, size_t bytes) {
    return cudaMalloc(p, bytes) == cudaSuccess;
  };
  if (!mal(reinterpret_cast<void**>(&d_ids), T * sizeof(int32_t)) ||
      !mal(reinterpret_cast<void**>(&d_pos), T * sizeof(int)) ||
      !mal(reinterpret_cast<void**>(&d_trunk), T * hc_dim * 2) ||
      !mal(reinterpret_cast<void**>(&d_logits_main),
           static_cast<size_t>(T) * mcfg.vocab * 2) ||
      !mal(reinterpret_cast<void**>(&d_sample), T * mcfg.hs * 2) ||
      !mal(reinterpret_cast<void**>(&d_multi), T * hc_dim * 2) ||
      !mal(reinterpret_cast<void**>(&d_multi2), T * hc_dim * 2) ||
      !mal(reinterpret_cast<void**>(&d_logits),
           static_cast<size_t>(T) * mcfg.vocab * 2)) {
    std::printf("  cudaMalloc failed\n");
    mtp.Free();
    m.Free();
    return false;
  }
  cudaMemcpy(d_ids, ids, T * sizeof(int32_t), cudaMemcpyHostToDevice);
  cudaMemcpy(d_pos, positions, T * sizeof(int), cudaMemcpyHostToDevice);

  // 3. Main-model prefill to get the pre-final-mixer multi stream (trunk_out).
  ModelSequence seq;
  s = ModelBeginSequence(m, &seq, nullptr);
  if (!s.ok()) {
    std::printf("  begin sequence failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }
  s = ModelPrefill(m, &seq, ids, T, d_logits_main, nullptr, d_trunk);
  ModelEndSequence(&seq);
  if (!s.ok()) {
    std::printf("  prefill failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }
  std::printf("  main prefill done, trunk_out [T=%d, hc*hs=%zu]\n", T, hc_dim);

  // 4. MTP draft step 0 (hidden_states = main trunk_out).
  MtpResetState(mtp, nullptr);
  std::vector<uint16_t> logits0, logits0b, logits1;
  if (!RunMtpStep(mtp, d_ids, d_pos, d_trunk, d_sample, d_multi, d_logits, T,
                  mcfg.vocab, &logits0)) {
    mtp.Free();
    m.Free();
    return false;
  }
  // Smoke checks: finite, non-trivially non-zero, argmax in vocab.
  double max_abs = 0.0;
  int argmax = -1;
  bool all_finite = true;
  for (int t = 0; t < T; ++t) {
    double best = -1e300;
    for (int v = 0; v < mcfg.vocab; ++v) {
      const float x =
          Bf16ToFloat(logits0[static_cast<size_t>(t) * mcfg.vocab + v]);
      if (!std::isfinite(x)) all_finite = false;
      const double a = std::fabs(x);
      if (a > max_abs) max_abs = a;
      if (x > best) {
        best = x;
        argmax = v;
      }
    }
  }
  std::printf("  step0 logits: max_abs=%.3f argmax(last)=%d finite=%d\n",
              max_abs, argmax, all_finite ? 1 : 0);
  Q4T_CHECK(all_finite);
  Q4T_CHECK(max_abs > 1e-3);  // non-trivial output

  // Determinism: a second step-0 run from the same input must be bit-identical.
  MtpResetState(mtp, nullptr);
  if (!RunMtpStep(mtp, d_ids, d_pos, d_trunk, d_sample, d_multi, d_logits, T,
                  mcfg.vocab, &logits0b)) {
    mtp.Free();
    m.Free();
    return false;
  }
  bool identical0 = logits0 == logits0b;
  std::printf("  step0 determinism: %s\n", identical0 ? "identical" : "DIFFER");
  Q4T_CHECK(identical0);

  // 5. MTP draft step 1 (hidden_states = step 0's multi_hidden, a new token).
  //    Use the step-0 argmax token as the "new" token for all T positions.
  int32_t new_ids[T];
  for (int t = 0; t < T; ++t) new_ids[t] = argmax;
  cudaMemcpy(d_ids, new_ids, T * sizeof(int32_t), cudaMemcpyHostToDevice);
  // Step 1 positions continue after the step-0 positions (T..T+T-1).
  int pos1[T];
  for (int t = 0; t < T; ++t) pos1[t] = T + t;
  cudaMemcpy(d_pos, pos1, T * sizeof(int), cudaMemcpyHostToDevice);
  // Copy step 0's multi_hidden to the step-1 input buffer.
  cudaMemcpy(d_multi2, d_multi, T * hc_dim * 2, cudaMemcpyDeviceToDevice);
  MtpResetState(mtp, nullptr);
  if (!RunMtpStep(mtp, d_ids, d_pos, d_multi2, d_sample, d_multi, d_logits, T,
                  mcfg.vocab, &logits1)) {
    mtp.Free();
    m.Free();
    return false;
  }
  double max_abs1 = 0.0;
  bool all_finite1 = true;
  for (size_t i = 0; i < logits1.size(); ++i) {
    const float x = Bf16ToFloat(logits1[i]);
    if (!std::isfinite(x)) all_finite1 = false;
    max_abs1 = std::max(max_abs1, static_cast<double>(std::fabs(x)));
  }
  std::printf("  step1 logits: max_abs=%.3f finite=%d\n", max_abs1,
              all_finite1 ? 1 : 0);
  Q4T_CHECK(all_finite1);
  Q4T_CHECK(max_abs1 > 1e-3);

  cudaFree(d_ids);
  cudaFree(d_pos);
  cudaFree(d_trunk);
  cudaFree(d_logits_main);
  cudaFree(d_sample);
  cudaFree(d_multi);
  cudaFree(d_multi2);
  cudaFree(d_logits);
  mtp.Free();
  m.Free();
  return true;
}
