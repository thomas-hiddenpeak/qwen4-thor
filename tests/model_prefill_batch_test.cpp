// Test for ModelPrefillBatch (Phase 2 ragged batched prefill). Loads a small
// main model (2 layers = 1 linear + 1 linear/PLE) and verifies that packing B
// FRESH, VARIABLE-LENGTH sequences into ONE ragged forward produces the same
// per-sequence logits as B independent single-sequence prefills.
//
//   1. Per-token logits of each packed sequence match its single-seq prefill
//      reference (l2_rel < 0.01: the ragged causal kernels are a per-sequence
//      copy of the single-seq prefill kernels, so the match is near bit-exact).
//   2. No cross-sequence contamination: the B sequences use DIFFERENT prompts
//      AND DIFFERENT lengths, so a packing bug (wrong cu_seqlens / token_local)
//      would show up as a large l2_rel against the wrong reference.
//   3. Last-token argmax of each packed sequence matches its reference (the
//      next-token distribution serve samples from).
//
// The ragged path exercises: cu_seqlens (seq_offset) for the update-state / GDN
// batch-grid kernels, token_local for the conv / PLE token-grid kernels, and
// per-token d_seq_id for the full-attention KV/indexer slices (length-agnostic).
//
// Skipped when CUDA or the model is absent.
#include "q4t/model/model.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
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
using q4t::model::ModelBeginSequence;
using q4t::model::ModelConfig;
using q4t::model::ModelEndSequence;
using q4t::model::ModelPrefill;
using q4t::model::ModelPrefillBatch;
using q4t::model::ModelSequence;

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
float L2Rel(const uint16_t* a, const uint16_t* b, int n) {
  double num = 0.0, den = 0.0;
  for (int i = 0; i < n; ++i) {
    const float x = Bf16ToFloat(a[i]);
    const float y = Bf16ToFloat(b[i]);
    num += static_cast<double>(x - y) * (x - y);
    den += static_cast<double>(y) * y;
  }
  return den > 0.0 ? static_cast<float>(std::sqrt(num / den)) : 0.0f;
}
int Argmax(const uint16_t* row, int n) {
  int best = 0;
  float bestv = Bf16ToFloat(row[0]);
  for (int i = 1; i < n; ++i) {
    const float v = Bf16ToFloat(row[i]);
    if (v > bestv) {
      bestv = v;
      best = i;
    }
  }
  return best;
}

}  // namespace

Q4T_TEST(model_prefill_batch) {
  // Force the shared-state GDN kernel: the single-seq ModelPrefill golden must
  // use the same kernel as the (shared-state) batched path it validates (the
  // register-state kernel is ON by default). See q4t::test::GdnRegOff.
  q4t::test::GdnRegOff gdn_reg_off;
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar)) {
    std::printf("  (skipped: model not found)\n");
    return true;
  }

  // 2 layers: layer 0 = linear attention, layer 1 = linear attention + PLE.
  // Exercises the linear SSM/conv AND the PLE conv ragged causal paths.
  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = 2;
  cfg.max_prefill = 32;
  cfg.max_seq = 3;
  cfg.ple_sidecar = kPleSidecar;
  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  main model load failed: %s\n", s.message().c_str());
    return false;
  }
  const int vocab = m.cfg.vocab;

  // B = 3 sequences, DIFFERENT lengths + DIFFERENT prompts.
  const int B = 3;
  const std::vector<std::vector<int32_t>> prompts = {
      {846, 25, 1203, 321, 44},           // len 5
      {1024, 55, 9001},                   // len 3
      {12, 3456, 789, 234, 5678, 90, 111}  // len 7
  };
  std::vector<int> lens(B), seq_ids(B);
  int Ttot = 0;
  for (int b = 0; b < B; ++b) {
    lens[b] = static_cast<int>(prompts[b].size());
    seq_ids[b] = b;
    Ttot += lens[b];
  }

  uint16_t* d_logits = nullptr;  // [Ttot, vocab]
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits),
                 static_cast<size_t>(Ttot) * vocab * 2) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    m.Free();
    return false;
  }

  // ---- Reference: B independent single-sequence prefills ------------------
  std::vector<std::vector<uint16_t>> ref(B);  // [B][len_b * vocab]
  for (int b = 0; b < B; ++b) {
    ModelSequence rseq;
    s = ModelBeginSequence(m, &rseq, nullptr, seq_ids[b]);
    if (!s.ok()) {
      std::printf("  ref begin seq %d failed: %s\n", b, s.message().c_str());
      cudaFree(d_logits);
      m.Free();
      return false;
    }
    s = ModelPrefill(m, &rseq, prompts[b].data(), lens[b], d_logits, nullptr,
                     nullptr, nullptr, seq_ids[b]);
    if (!s.ok()) {
      std::printf("  ref prefill seq %d failed: %s\n", b, s.message().c_str());
      cudaFree(d_logits);
      m.Free();
      return false;
    }
    ref[b].resize(static_cast<size_t>(lens[b]) * vocab);
    cudaMemcpy(ref[b].data(), d_logits,
               static_cast<size_t>(lens[b]) * vocab * 2, cudaMemcpyDeviceToHost);
    ModelEndSequence(&rseq);
  }

  // ---- Ragged batched prefill --------------------------------------------
  // Packed tokens, sequence-major (seq 0's len tokens, then seq 1's, ...).
  std::vector<int32_t> tokens;
  tokens.reserve(Ttot);
  for (int b = 0; b < B; ++b)
    tokens.insert(tokens.end(), prompts[b].begin(), prompts[b].end());

  s = ModelPrefillBatch(m, tokens.data(), lens.data(), seq_ids.data(), B,
                        d_logits, nullptr);
  if (!s.ok()) {
    std::printf("  ModelPrefillBatch failed: %s\n", s.message().c_str());
    cudaFree(d_logits);
    m.Free();
    return false;
  }
  std::vector<uint16_t> batched(static_cast<size_t>(Ttot) * vocab);
  cudaMemcpy(batched.data(), d_logits,
             static_cast<size_t>(Ttot) * vocab * 2, cudaMemcpyDeviceToHost);

  // ---- Compare per token + last-token argmax ------------------------------
  bool ok = true;
  int offset = 0;
  for (int b = 0; b < B; ++b) {
    float worst = 0.0f;
    for (int t = 0; t < lens[b]; ++t) {
      const float rel =
          L2Rel(batched.data() + static_cast<size_t>(offset + t) * vocab,
                ref[b].data() + static_cast<size_t>(t) * vocab, vocab);
      worst = std::max(worst, rel);
    }
    const int last = lens[b] - 1;
    const int am_batch =
        Argmax(batched.data() + static_cast<size_t>(offset + last) * vocab,
               vocab);
    const int am_ref =
        Argmax(ref[b].data() + static_cast<size_t>(last) * vocab, vocab);
    const bool seq_ok = (worst < 0.01f) && (am_batch == am_ref);
    std::printf("  seq %d (len=%d): worst l2_rel=%.5f  argmax batch=%d ref=%d "
                "%s\n",
                b, lens[b], worst, am_batch, am_ref, seq_ok ? "OK" : "MISMATCH");
    if (!seq_ok) ok = false;
    offset += lens[b];
  }

  cudaFree(d_logits);
  m.Free();
  if (!ok) {
    std::printf("  FAIL: batched prefill diverged from single-seq reference\n");
    return false;
  }
  std::printf("  PASS: ragged batched prefill matches per-seq prefill "
              "(B=%d, lens=5/3/7)\n", B);
  return true;
}
