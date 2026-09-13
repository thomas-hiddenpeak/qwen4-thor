// Test for ModelVerifyMulti (Phase 2 MTP multi-sequence verify). Loads a small
// main model (2 layers = 1 linear + 1 linear/PLE, so the PLE conv checkpoint
// path is exercised) and verifies that packing B sequences x T tokens into ONE
// forward (sequence-major) produces the same per-sequence logits as a single-
// sequence PREFILL of the full sequence, and that the per-sequence checkpoint
// rollback (ModelRestoreCheckpoint) restores each sequence's recurrent state
// (linear SSM/conv + PLE conv) to its accepted-prefix boundary.
//
//   1. Multi-seq verify logits (B=2, T=3) match the single-seq prefill
//      reference (l2_rel < 0.01: the multi-seq causal kernel is a per-sequence
//      copy of the single-seq prefill kernel, so the match is near bit-exact).
//   2. No cross-sequence contamination: the two sequences use DIFFERENT prompts
//      and their verify logits match their OWN reference (not each other's).
//   3. Partial-accept rollback: after accepting only the first verify token of
//      a sequence, ModelRestoreCheckpoint(a=0) rolls the recurrent state back;
//      the next decode step then matches the reference state at that boundary
//      (this also exercises the PLE conv restore, which the single-seq path
//      previously skipped).
//
// NOTE on the reference: the multi-seq causal kernel (GatedDeltaNetMultiSeq
// CausalKernel) uses PREFILL math (it serializes each sequence's tokens with
// the in-place S recurrence), so the reference is a single-seq PREFILL
// (GatedDeltaNetKernel), NOT the incremental decode path. The decode kernel
// (GatedDeltaNetDecodeKernel) has slightly different per-token math, and the
// batch-vs-decode residual is a known MoE-routing-boundary sensitivity (not a
// state bug) — see docs/LOG.md. The recurrent state is chunk-invariant, so
// prefill([prompt]) + continue([verify]) == prefill([prompt+verify]).
//
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
using q4t::model::ModelSequence;
using q4t::model::ModelBeginSequence;
using q4t::model::ModelPrefill;
using q4t::model::ModelDecodeStepSeq;
using q4t::model::ModelEndSequence;
using q4t::model::ModelReserveVerifyCheckpoints;
using q4t::model::ModelRestoreCheckpoint;
using q4t::model::ModelVerifyMulti;

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
float L2Rel(const std::vector<uint16_t>& a, const std::vector<uint16_t>& b) {
  double num = 0.0, den = 0.0;
  for (size_t i = 0; i < a.size(); ++i) {
    const float x = Bf16ToFloat(a[i]);
    const float y = Bf16ToFloat(b[i]);
    num += static_cast<double>(x - y) * (x - y);
    den += static_cast<double>(y) * y;
  }
  return den > 0.0 ? static_cast<float>(std::sqrt(num / den)) : 0.0f;
}

}  // namespace

Q4T_TEST(model_verify_multi) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar)) {
    std::printf("  (skipped: model not found)\n");
    return true;
  }

  // 2 layers: layer 0 = linear attention, layer 1 = linear attention + PLE
  // (has_ple = (layer_id == 1)). This exercises the linear SSM/conv AND the PLE
  // conv checkpoint paths.
  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = 2;
  cfg.max_prefill = 8;
  cfg.max_seq = 2;
  cfg.ple_sidecar = kPleSidecar;
  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  main model load failed: %s\n", s.message().c_str());
    return false;
  }
  const int vocab = m.cfg.vocab;

  // Checkpoint capacity: T-1 = 2 per-token checkpoints per sequence.
  s = ModelReserveVerifyCheckpoints(m, 2);
  if (!s.ok()) {
    std::printf("  reserve checkpoints failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }

  const int B = 2;          // sequences
  const int T = 3;          // verify tokens per sequence (bonus + 2 drafts)
  const int Ttot = B * T;   // packed rows
  const int Tprompt = 4;    // prompt length per sequence

  // Two DIFFERENT prompts so the sequences diverge (cross-seq contamination
  // would show up as a large l2_rel against the wrong reference).
  const int32_t prompt0[] = {846, 25, 1203, 321};
  const int32_t prompt1[] = {1024, 55, 9001, 77};
  // Verify tokens per sequence (bonus + 2 drafts).
  const int32_t verify0[] = {236644, 11, 22};
  const int32_t verify1[] = {50000, 33, 44};

  // Device buffers.
  uint16_t* d_mlogits = nullptr;  // [Ttot, vocab] multi verify logits
  uint16_t* d_plogits = nullptr;  // [Tprompt+T, vocab] prefill logits
  uint16_t* d_step_logits = nullptr;  // [1, vocab] single decode step
  auto mal = [&](void** p, size_t bytes) {
    return cudaMalloc(p, bytes) == cudaSuccess;
  };
  if (!mal(reinterpret_cast<void**>(&d_mlogits),
           static_cast<size_t>(Ttot) * vocab * 2) ||
      !mal(reinterpret_cast<void**>(&d_plogits),
           static_cast<size_t>(Tprompt + T) * vocab * 2) ||
      !mal(reinterpret_cast<void**>(&d_step_logits),
           static_cast<size_t>(vocab) * 2)) {
    std::printf("  cudaMalloc failed\n");
    m.Free();
    return false;
  }

  // ---- Single-sequence prefill reference ---------------------------------
  // For each sequence: prefill the FULL sequence (prompt + verify tokens) in
  // one forward. The logits at the verify-token positions are the ground truth
  // the multi-seq causal kernel must match (same prefill math, per sequence).
  // The recurrent state is chunk-invariant, so prefill([prompt+verify]) gives
  // the same verify-token logits as prefill([prompt]) + continue([verify]).
  std::vector<std::vector<uint16_t>> ref(B);  // [B][T*vocab] verify rows
  for (int b = 0; b < B; ++b) {
    std::vector<int32_t> full(Tprompt + T);
    for (int t = 0; t < Tprompt; ++t)
      full[t] = (b == 0 ? prompt0[t] : prompt1[t]);
    for (int t = 0; t < T; ++t)
      full[Tprompt + t] = (b == 0 ? verify0[t] : verify1[t]);
    ModelSequence rseq;
    s = ModelBeginSequence(m, &rseq, nullptr, b);
    if (!s.ok()) {
      std::printf("  ref begin seq %d failed: %s\n", b, s.message().c_str());
      m.Free();
      return false;
    }
    s = ModelPrefill(m, &rseq, full.data(), Tprompt + T, d_plogits, nullptr,
                     nullptr, nullptr, b);
    if (!s.ok()) {
      std::printf("  ref prefill seq %d failed: %s\n", b, s.message().c_str());
      m.Free();
      return false;
    }
    std::vector<uint16_t> plogits(static_cast<size_t>(Tprompt + T) * vocab);
    cudaMemcpy(plogits.data(), d_plogits,
               static_cast<size_t>(Tprompt + T) * vocab * 2,
               cudaMemcpyDeviceToHost);
    ref[b].resize(static_cast<size_t>(T) * vocab);
    for (int t = 0; t < T; ++t)
      std::memcpy(ref[b].data() + static_cast<size_t>(t) * vocab,
                  plogits.data() + static_cast<size_t>(Tprompt + t) * vocab,
                  static_cast<size_t>(vocab) * 2);
    ModelEndSequence(&rseq);
  }

  // ---- Multi-sequence verify path ----------------------------------------
  // Prefill both sequences with the prompt only (state at position Tprompt-1
  // for each), then run the packed multi-seq verify over the T verify tokens.
  ModelSequence seq[2];
  const int32_t* prompts[2] = {prompt0, prompt1};
  for (int b = 0; b < B; ++b) {
    s = ModelBeginSequence(m, &seq[b], nullptr, b);
    if (!s.ok()) {
      std::printf("  begin seq %d failed: %s\n", b, s.message().c_str());
      m.Free();
      return false;
    }
    s = ModelPrefill(m, &seq[b], prompts[b], Tprompt, d_plogits, nullptr,
                     nullptr, nullptr, b);
    if (!s.ok()) {
      std::printf("  prefill seq %d failed: %s\n", b, s.message().c_str());
      m.Free();
      return false;
    }
  }

  // Packed verify tokens (sequence-major: seq 0's T tokens, then seq 1's).
  std::vector<int32_t> tokens(Ttot);
  for (int b = 0; b < B; ++b)
    for (int t = 0; t < T; ++t)
      tokens[static_cast<size_t>(b) * T + t] =
          (b == 0 ? verify0[t] : verify1[t]);
  const int base_positions[2] = {Tprompt, Tprompt};
  const int seq_ids[2] = {0, 1};
  // PLE history per sequence = its prompt (positions < base).
  const int history_len = Tprompt;
  std::vector<int32_t> history(static_cast<size_t>(B) * history_len);
  for (int b = 0; b < B; ++b)
    for (int t = 0; t < history_len; ++t)
      history[static_cast<size_t>(b) * history_len + t] = prompts[b][t];

  s = ModelVerifyMulti(m, tokens.data(), base_positions, seq_ids, history.data(),
                       history_len, B, T, d_mlogits, nullptr);
  if (!s.ok()) {
    std::printf("  ModelVerifyMulti failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }

  // ---- 1+2. Multi-seq verify logits match the per-seq prefill reference --
  std::vector<uint16_t> mlogits(static_cast<size_t>(Ttot) * vocab);
  cudaMemcpy(mlogits.data(), d_mlogits,
             static_cast<size_t>(Ttot) * vocab * 2, cudaMemcpyDeviceToHost);
  bool ok = true;
  for (int b = 0; b < B; ++b) {
    for (int t = 0; t < T; ++t) {
      std::vector<uint16_t> row_a(mlogits.begin() +
                                      (static_cast<size_t>(b) * T + t) * vocab,
                                  mlogits.begin() +
                                      (static_cast<size_t>(b) * T + t + 1) *
                                          vocab);
      std::vector<uint16_t> row_b(ref[b].begin() + t * vocab,
                                  ref[b].begin() + (t + 1) * vocab);
      const float rel = L2Rel(row_a, row_b);
      std::printf("  seq %d tok %d: l2_rel=%.5f %s\n", b, t, rel,
                  rel < 0.01f ? "OK" : "MISMATCH");
      if (rel >= 0.01f) ok = false;
    }
  }

  // ---- 3. Partial-accept rollback (sequence 0, accept a=0) ---------------
  // The multi verify advanced seq 0's recurrent state over all T tokens.
  // Restore checkpoint[0] (state after the first verify token) and decode the
  // second verify token; it must match the reference state at that boundary
  // (the prefill logits at position Tprompt+1). This exercises the PLE conv
  // restore (the single-seq path previously skipped it). The decode step uses
  // the decode kernel, so the match carries the known batch-vs-decode residual
  // (hence the looser 0.02 threshold vs the 0.01 verify threshold).
  s = ModelRestoreCheckpoint(m, 0, nullptr, 0);
  if (!s.ok()) {
    std::printf("  restore checkpoint failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  // ModelVerifyMulti / ModelRestoreCheckpoint do NOT advance the ModelSequence
  // state machine (the real MTP flow updates it in the caller). Align seq[0]
  // to the "accepted verify0[0]" boundary: position = Tprompt+1, history +=
  // verify0[0]. Decoding verify0[1] from here must match the reference.
  seq[0].position = Tprompt + 1;
  seq[0].history.push_back(verify0[0]);
  s = ModelDecodeStepSeq(m, &seq[0], verify0[1], d_step_logits, nullptr, nullptr,
                         0);
  if (!s.ok()) {
    std::printf("  post-rollback decode failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  std::vector<uint16_t> rb(vocab);
  cudaMemcpy(rb.data(), d_step_logits, static_cast<size_t>(vocab) * 2,
             cudaMemcpyDeviceToHost);
  std::vector<uint16_t> rb_ref(ref[0].begin() + vocab, ref[0].begin() + 2 * vocab);
  const float rb_rel = L2Rel(rb, rb_ref);
  std::printf("  rollback seq 0 (a=0) then decode verify0[1]: l2_rel=%.5f %s\n",
              rb_rel, rb_rel < 0.02f ? "OK" : "MISMATCH");
  if (rb_rel >= 0.02f) ok = false;

  cudaFree(d_mlogits);
  cudaFree(d_plogits);
  cudaFree(d_step_logits);
  m.Free();
  return ok;
}
