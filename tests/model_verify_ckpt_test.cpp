// Targeted regression for the two MTP verify checkpoint defects fixed on
// 2026-10-11 (single-sequence path, ModelDecodeBatch save_checkpoints=true):
//
//   (1) stride-contract mismatch: RunLayers saved per-token checkpoints with
//       layer stride num_ckpt (= T-1) while ModelRestoreCheckpoint /
//       ModelSnapshotCheckpoint read them with layer stride verify_ckpt_cap.
//       The two agree only when cap == T-1. Fixed by unifying the save to cap.
//       This test reserves cap > T-1 so the old layout would misplace every
//       layer >= 1 checkpoint and the post-rollback decode would diverge from
//       the single-seq prefill reference.
//
//   (2) single-seq PLE conv checkpoint gap: the single-seq verify path never
//       saved the PLE short-conv checkpoint (ModelDecodeBatch passed nullptr
//       for d_verify_ple_conv_ckpt), so a partial accept restored a stale PLE
//       conv state. Fixed by adding PleConvCheckpointSingleSeqKernel and
//       passing the buffer. This test uses a 2-layer model whose layer 1 has
//       PLE, so the post-rollback decode exercises the PLE conv restore.
//
// Method (single sequence, seq_id 0, same as serve):
//   - 2-layer model (layer 0 linear, layer 1 linear+PLE), cap=4, T=3 (T-1=2).
//   - prefill the prompt; run ModelDecodeBatch(verify, T=3,
//     save_checkpoints=true) -> checkpoint[a] = state after verify[a].
//   - for a in {0,1}: ModelRestoreCheckpoint(a), align the sequence to the
//     accepted boundary, decode verify[a+1]; the logits must match the
//     single-seq PREFILL reference at that position (the verify path uses the
//     shared-state GDN kernel, so the reference is a prefill with GdnRegOff).
//
// With either defect present the restored recurrent state is wrong, so the
// post-rollback decode diverges from the reference by far more than the
// batch-vs-decode residual (threshold 0.02, matching model_verify_multi).
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
using q4t::model::ModelDecodeBatch;
using q4t::model::ModelEndSequence;
using q4t::model::ModelReserveVerifyCheckpoints;
using q4t::model::ModelRestoreCheckpoint;

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

Q4T_TEST(model_verify_ckpt) {
  // Force the shared-state GDN kernel so the prefill reference uses the same
  // kernel as the (shared-state) verify path (the register-state kernel is ON
  // by default and is NOT used when ssm_ckpt is present). See GdnRegOff.
  q4t::test::GdnRegOff gdn_reg_off;
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar)) {
    Q4T_SKIP("(skipped: model or PLE sidecar not found)");
  }

  // 2 layers: layer 0 = linear attention, layer 1 = linear attention + PLE
  // (has_ple = (layer_id == 1)). Exercises the linear SSM/conv AND the PLE
  // conv checkpoint paths (defect 2).
  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = 2;
  cfg.max_prefill = 8;
  cfg.max_seq = 1;
  cfg.ple_sidecar = kPleSidecar;
  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  main model load failed: %s\n", s.message().c_str());
    return false;
  }
  const int vocab = m.cfg.vocab;

  // cap > T-1 (defect 1): reserve cap=4 while the verify batch is T=3
  // (num_ckpt = T-1 = 2). The old save stride (num_ckpt) would misplace every
  // layer >= 1 checkpoint relative to the cap-stride restore/snapshot.
  const int cap = 4;
  s = ModelReserveVerifyCheckpoints(m, cap);
  if (!s.ok()) {
    std::printf("  reserve checkpoints (cap=%d) failed: %s\n", cap,
                s.message().c_str());
    m.Free();
    return false;
  }
  std::printf("  reserve_cap=%d (T-1=2, cap > count)\n", cap);

  const int T = 3;        // verify tokens (num_ckpt = T-1 = 2 < cap)
  const int Tprompt = 4;  // prompt length
  const int32_t prompt[] = {846, 25, 1203, 321};
  const int32_t verify[] = {236644, 11, 22};

  uint16_t* d_blogits = nullptr;    // [T, vocab] batch verify logits
  uint16_t* d_plogits = nullptr;    // [Tprompt+T, vocab] prefill logits
  uint16_t* d_step_logits = nullptr;  // [1, vocab] single decode step
  auto mal = [&](void** p, size_t bytes) {
    return cudaMalloc(p, bytes) == cudaSuccess;
  };
  if (!mal(reinterpret_cast<void**>(&d_blogits),
           static_cast<size_t>(T) * vocab * 2) ||
      !mal(reinterpret_cast<void**>(&d_plogits),
           static_cast<size_t>(Tprompt + T) * vocab * 2) ||
      !mal(reinterpret_cast<void**>(&d_step_logits),
           static_cast<size_t>(vocab) * 2)) {
    std::printf("  cudaMalloc failed\n");
    m.Free();
    return false;
  }

  // ---- Single-sequence prefill reference ---------------------------------
  // Prefill the FULL sequence (prompt + verify tokens) in one forward. The
  // logits at the verify-token positions are the ground truth the post-
  // rollback decode must match (same shared-state GDN math, per sequence).
  std::vector<int32_t> full(Tprompt + T);
  for (int t = 0; t < Tprompt; ++t) full[t] = prompt[t];
  for (int t = 0; t < T; ++t) full[Tprompt + t] = verify[t];
  ModelSequence rseq;
  s = ModelBeginSequence(m, &rseq, nullptr, 0);
  if (!s.ok()) {
    std::printf("  ref begin seq failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  s = ModelPrefill(m, &rseq, full.data(), Tprompt + T, d_plogits, nullptr,
                   nullptr, nullptr, 0);
  if (!s.ok()) {
    std::printf("  ref prefill failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  std::vector<uint16_t> plogits(static_cast<size_t>(Tprompt + T) * vocab);
  cudaMemcpy(plogits.data(), d_plogits,
             static_cast<size_t>(Tprompt + T) * vocab * 2,
             cudaMemcpyDeviceToHost);
  ModelEndSequence(&rseq);

  // ---- Single-sequence verify path ----------------------------------------
  // Prefill the prompt only, then run the single-seq batch verify over the T
  // verify tokens with save_checkpoints=true (the production MTP verify shape).
  ModelSequence seq;
  s = ModelBeginSequence(m, &seq, nullptr, 0);
  if (!s.ok()) {
    std::printf("  begin seq failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  s = ModelPrefill(m, &seq, prompt, Tprompt, d_plogits, nullptr, nullptr,
                   nullptr, 0);
  if (!s.ok()) {
    std::printf("  prefill failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  const int base = Tprompt;  // absolute position of verify[0]
  std::vector<int32_t> history(Tprompt);
  for (int t = 0; t < Tprompt; ++t) history[t] = prompt[t];
  s = ModelDecodeBatch(m, verify, T, base, history.data(), history.size(),
                       d_blogits, nullptr, nullptr, /*save_checkpoints=*/true,
                       /*seq_id=*/0);
  if (!s.ok()) {
    std::printf("  ModelDecodeBatch failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }

  // ---- Post-rollback decode for each checkpoint ---------------------------
  // checkpoint[a] = recurrent state after verify[a] (position Tprompt+a).
  // Restore it, align the sequence to the accepted boundary, decode
  // verify[a+1]; the logits must match the prefill reference at that
  // position. (ModelRestoreCheckpoint does not advance the ModelSequence
  // state machine; the caller aligns it, as in the real MTP flow.)
  bool ok = true;
  for (int a = 0; a < T - 1; ++a) {
    // Restore stays on the batch stream; no caller wait should be needed.
    s = ModelRestoreCheckpoint(m, a, nullptr, /*seq_id=*/0);
    if (!s.ok()) {
      std::printf("  restore checkpoint %d failed: %s\n", a,
                  s.message().c_str());
      m.Free();
      return false;
    }
    // Align seq to the "accepted verify[0..a]" boundary: position =
    // Tprompt+a+1, history += verify[0..a]. Decoding verify[a+1] from here
    // must match the reference at position Tprompt+a+1.
    seq.position = Tprompt + a + 1;
    for (int k = 0; k <= a; ++k) seq.history.push_back(verify[k]);
    s = ModelDecodeStepSeq(m, &seq, verify[a + 1], d_step_logits, nullptr,
                           nullptr, /*seq_id=*/0);
    if (!s.ok()) {
      std::printf("  post-rollback decode (a=%d) failed: %s\n", a,
                  s.message().c_str());
      m.Free();
      return false;
    }
    std::vector<uint16_t> rb(vocab);
    cudaMemcpy(rb.data(), d_step_logits, static_cast<size_t>(vocab) * 2,
               cudaMemcpyDeviceToHost);
    const size_t ref_off =
        static_cast<size_t>(Tprompt + a + 1) * static_cast<size_t>(vocab);
    std::vector<uint16_t> rb_ref(plogits.begin() + ref_off,
                                 plogits.begin() + ref_off + vocab);
    const float rel = L2Rel(rb, rb_ref);
    std::printf("  cap>T-1 rollback (a=%d) then decode verify[%d]: l2_rel="
                "%.5f %s\n",
                a, a + 1, rel, rel < 0.02f ? "OK" : "MISMATCH");
    if (rel >= 0.02f) ok = false;
    // Reset the sequence state machine for the next iteration (the restore
    // does not touch it; re-align to the prompt boundary).
    s = ModelEndSequence(&seq);
    if (!s.ok()) {
      std::printf("  end seq (a=%d) failed: %s\n", a, s.message().c_str());
      m.Free();
      return false;
    }
    s = ModelBeginSequence(m, &seq, nullptr, 0);
    if (!s.ok()) {
      std::printf("  re-begin seq (a=%d) failed: %s\n", a, s.message().c_str());
      m.Free();
      return false;
    }
    s = ModelPrefill(m, &seq, prompt, Tprompt, d_plogits, nullptr, nullptr,
                     nullptr, 0);
    if (!s.ok()) {
      std::printf("  re-prefill (a=%d) failed: %s\n", a, s.message().c_str());
      m.Free();
      return false;
    }
    if (a + 1 < T - 1) {
      // Begin/prefill invalidates saved rows. Publish fresh checkpoints for
      // the next independent rollback case; never restore a stale row.
      s = ModelDecodeBatch(m, verify, T, base, history.data(), history.size(),
                           d_blogits, nullptr, nullptr, true, 0);
      if (!s.ok()) {
        std::printf("  re-verify failed: %s\n", s.message().c_str());
        m.Free();
        return false;
      }
    }
  }

  cudaFree(d_blogits);
  cudaFree(d_plogits);
  cudaFree(d_step_logits);
  m.Free();
  return ok;
}
