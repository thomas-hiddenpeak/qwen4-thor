// Multi-sequence speculative step (Phase 2 Stage 2b).
//
// Two sequences, each with a different prompt, run ONE batched
// MtpSpeculativeStepMulti (batched draft loop + ModelVerifyMulti + batched
// extend). Per-sequence observables are compared against the ground truth:
//   1. accepted_tokens[0..a_b-1] == the main model's greedy tokens at
//      positions P_b..P_b+a_b-1 (the bonus + accepted drafts).
//   2. next_b[b] == the main model's greedy token at position P_b+a_b
//      (the correction / next bonus).
//   3. next_d0[b] is a valid vocab token (the DRAFT model's prediction for
//      t_{P_b+a_b+1}; not compared to the main model — the draft is a
//      different model).
//
// The ground truth is a PREFILL-semantics greedy decode: at each step the
// main model re-prefills the full prefix (prompt + accepted so far) and takes
// the last-row argmax. This matches ModelVerifyMulti's prefill kernel
// (GatedDeltaNetKernel), so the comparison is apples-to-apples (a decode-kernel
// ground truth would differ by the known MoE routing-boundary sensitivity).
//
// The batched path must equal the per-sequence single-seq path (Stage 1's
// MtpForward d_seq_id + Stage 2a's ModelVerifyMulti are both bit-exact vs
// single-seq), so any cross-sequence contamination shows up as a mismatch.
#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"
#include "q4t/test.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

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
using q4t::model::ModelReserveVerifyCheckpoints;
using q4t::mtp::LoadMtp;
using q4t::mtp::MtpConfig;
using q4t::mtp::MtpModel;
using q4t::mtp::MtpForward;
using q4t::mtp::MtpResetState;
using q4t::mtp::MtpReserveScratch;
using q4t::mtp::MtpSpeculativeStepMulti;

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
bool Mal(void** p, size_t bytes) {
  return cudaMalloc(p, bytes) == cudaSuccess;
}
float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}
int ArgmaxBf16(const uint16_t* lg, int vocab) {
  int best = 0;
  float bestv = -1e30f;
  for (int v = 0; v < vocab; ++v) {
    const float x = Bf16ToFloat(lg[v]);
    if (x > bestv) {
      bestv = x;
      best = v;
    }
  }
  return best;
}

// Prefill-semantics greedy decode: at each step re-prefill the full prefix
// (prompt + accepted so far) and take the last-row argmax. Returns n tokens
// (token[i] = the main model's greedy token at position T+i). Uses a throwaway
// ModelSequence so the caller's sequence state is untouched.
std::vector<int32_t> GreedyPrefill(const Model& m,
                                   const std::vector<int32_t>& prompt, int n) {
  const int vocab = m.cfg.vocab;
  std::vector<int32_t> prefix = prompt;
  std::vector<int32_t> out;
  uint16_t* d_logits = nullptr;
  const int cap = static_cast<int>(prompt.size()) + n;
  if (!Mal(reinterpret_cast<void**>(&d_logits),
           static_cast<size_t>(cap) * vocab * 2))
    return out;
  for (int i = 0; i < n; ++i) {
    const int T = static_cast<int>(prefix.size());
    ModelSequence seq;
    ModelBeginSequence(m, &seq, nullptr);
    ModelPrefill(m, &seq, prefix.data(), T, d_logits, nullptr, nullptr);
    std::vector<uint16_t> lg(vocab);
    cudaMemcpy(lg.data(), d_logits + static_cast<size_t>(T - 1) * vocab,
               vocab * 2, cudaMemcpyDeviceToHost);
    const int tok = ArgmaxBf16(lg.data(), vocab);
    out.push_back(tok);
    prefix.push_back(tok);
  }
  cudaFree(d_logits);
  return out;
}

}  // namespace

Q4T_TEST(mtp_spec_multi_step) {
  // Force the shared-state GDN kernel: the single-seq ModelPrefill ground truth
  // must use the same kernel as the (shared-state) MTP verify path (the
  // register-state kernel is ON by default). See q4t::test::GdnRegOff.
  q4t::test::GdnRegOff gdn_reg_off;
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex) || !FileExists(kMtpDir) || !FileExists(kPleSidecar)) {
    std::printf("  (skipped: model or MTP dir not found)\n");
    return true;
  }

  const int B = 2;
  const int k = 3;
  const int T0 = 4, T1 = 5;  // two different prompt lengths.

  // 1. Load a small main model (2 layers) pooled for B sequences.
  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = 2;
  cfg.max_prefill = 16;
  cfg.max_seq = B;
  cfg.ple_sidecar = kPleSidecar;
  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  main model load failed: %s\n", s.message().c_str());
    return false;
  }

  // 2. Load the MTP draft model (pooled for B sequences).
  MtpConfig mcfg;
  mcfg.mtp_dir = kMtpDir;
  mcfg.max_seq = B;
  MtpModel mtp;
  s = LoadMtp(mcfg, m.head.embed_tokens, m.head.lm_head, &mtp, nullptr);
  if (!s.ok()) {
    std::printf("  MTP load failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  const int vocab = mcfg.vocab;
  const int hs = mcfg.hs;
  const int hc_dim = mcfg.hc * mcfg.hs;
  std::printf("  MTP loaded (hs=%d hc_dim=%d max_seq=%d)\n", hs, hc_dim,
              mtp.max_seq);

  // Two distinct prompts (small ids to keep the 2-layer model stable).
  std::vector<int32_t> p0 = {846, 25, 1203, 321};
  std::vector<int32_t> p1 = {101, 202, 303, 404, 505};

  // 3. Ground truth (prefill-semantics greedy) for each sequence.
  std::vector<int32_t> gt0 = GreedyPrefill(m, p0, k + 2);
  std::vector<int32_t> gt1 = GreedyPrefill(m, p1, k + 2);
  std::printf("  gt0=%d %d %d %d  gt1=%d %d %d %d\n", gt0[0], gt0[1], gt0[2],
              gt0[3], gt1[0], gt1[1], gt1[2], gt1[3]);

  // Per-sequence buffers: full trunk (draft extend hidden gather) + single-row
  // g_in / next_g (the draft trunk).
  uint16_t* d_full[B], *g_in[B], *next_g[B];
  for (int b = 0; b < B; ++b) {
    const int T = (b == 0) ? T0 : T1;
    if (!Mal(reinterpret_cast<void**>(&d_full[b]),
             static_cast<size_t>(T) * hc_dim * 2) ||
        !Mal(reinterpret_cast<void**>(&g_in[b]),
             static_cast<size_t>(hc_dim) * 2) ||
        !Mal(reinterpret_cast<void**>(&next_g[b]),
             static_cast<size_t>(hc_dim) * 2)) {
      std::printf("  cudaMalloc failed\n");
      mtp.Free();
      m.Free();
      return false;
    }
  }

  // 4. Reset the MTP draft KV (all slices) and reserve scratch + checkpoints
  //    BEFORE building the draft KV (ResetState clears the pooled draft
  //    kv/idx, so it must precede the build, not follow it).
  MtpResetState(mtp, nullptr);
  s = ModelReserveVerifyCheckpoints(m, k);
  if (!s.ok()) {
    std::printf("  checkpoint reserve failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }
  s = MtpReserveScratch(mtp, k + 1);
  if (!s.ok()) {
    std::printf("  scratch reserve failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }

  // 5. Per-sequence draft KV build (EAGLE shift: [t_1..t_{T-1}, b0]).
  //    MtpDraftExtend always writes slice 0, so build each sequence's draft
  //    KV with MtpForward + d_seq_id (the pooled-slice path).
  //
  //    SWAPPED seq_id: sequence b uses pooled slice seq_id_of[b] (not b).
  //    This is the serve-path reality (seq_id comes from a free pool, not the
  //    batch index). If MtpSpeculativeStepMulti (or the draft build) misused
  //    the batch index b as the seq_id, it would read/write the wrong slice
  //    and the result would diverge from the ground truth.
  const int seq_id_of[B] = {1, 0};
  ModelSequence seqs[B];
  std::vector<int32_t> b_tok(B), d0(B);
  int32_t* d_ids = nullptr;
  int* d_pos = nullptr;
  int* d_seqid = nullptr;
  uint16_t* d_sample = nullptr;
  uint16_t* d_multi = nullptr;
  uint16_t* d_logits = nullptr;
  const int Tmx = T1;
  if (!Mal(reinterpret_cast<void**>(&d_ids),
           static_cast<size_t>(Tmx) * sizeof(int32_t)) ||
      !Mal(reinterpret_cast<void**>(&d_pos), static_cast<size_t>(Tmx) * sizeof(int)) ||
      !Mal(reinterpret_cast<void**>(&d_seqid),
           static_cast<size_t>(Tmx) * sizeof(int)) ||
      !Mal(reinterpret_cast<void**>(&d_sample),
           static_cast<size_t>(Tmx) * hs * 2) ||
      !Mal(reinterpret_cast<void**>(&d_multi),
           static_cast<size_t>(Tmx) * hc_dim * 2) ||
      !Mal(reinterpret_cast<void**>(&d_logits),
           static_cast<size_t>(Tmx) * vocab * 2)) {
    std::printf("  cudaMalloc draft failed\n");
    mtp.Free();
    m.Free();
    return false;
  }
  for (int b = 0; b < B; ++b) {
    const std::vector<int32_t>& p = (b == 0) ? p0 : p1;
    const int T = (b == 0) ? T0 : T1;
    s = ModelBeginSequence(m, &seqs[b], nullptr, seq_id_of[b]);
    if (!s.ok()) {
      std::printf("  begin seq %d failed: %s\n", b, s.message().c_str());
      mtp.Free();
      m.Free();
      return false;
    }
    s = ModelPrefill(m, &seqs[b], p.data(), T, nullptr, nullptr, d_full[b],
                     nullptr, seq_id_of[b]);
    if (!s.ok()) {
      std::printf("  prefill seq %d failed: %s\n", b, s.message().c_str());
      mtp.Free();
      m.Free();
      return false;
    }
    b_tok[b] = (b == 0) ? gt0[0] : gt1[0];  // bonus = main's first token.
    std::vector<int32_t> shifted(T);
    for (int i = 0; i < T - 1; ++i) shifted[i] = p[i + 1];
    shifted[T - 1] = b_tok[b];
    std::vector<int> pos(T), seqid(T, seq_id_of[b]);
    for (int i = 0; i < T; ++i) pos[i] = i;
    cudaMemcpy(d_ids, shifted.data(), T * sizeof(int32_t),
               cudaMemcpyHostToDevice);
    cudaMemcpy(d_pos, pos.data(), T * sizeof(int), cudaMemcpyHostToDevice);
    cudaMemcpy(d_seqid, seqid.data(), T * sizeof(int), cudaMemcpyHostToDevice);
    s = MtpForward(mtp, d_ids, d_pos, d_full[b], d_sample, d_multi, d_logits, T,
                   nullptr, d_seqid);
    if (!s.ok()) {
      std::printf("  draft extend[%d] failed: %s\n", b, s.message().c_str());
      mtp.Free();
      m.Free();
      return false;
    }
    // g_in[b] = last row's multi_hidden; d0[b] = last row's argmax.
    cudaMemcpy(g_in[b], d_multi + static_cast<size_t>(T - 1) * hc_dim,
               hc_dim * 2, cudaMemcpyDeviceToDevice);
    std::vector<uint16_t> lg(vocab);
    cudaMemcpy(lg.data(), d_logits + static_cast<size_t>(T - 1) * vocab,
               vocab * 2, cudaMemcpyDeviceToHost);
    d0[b] = ArgmaxBf16(lg.data(), vocab);
  }
  cudaFree(d_ids);
  cudaFree(d_pos);
  cudaFree(d_seqid);
  cudaFree(d_sample);
  cudaFree(d_multi);
  cudaFree(d_logits);

  // 6. The batched speculative step.
  std::vector<int32_t> accepted(B * (k + 1), -1);
  std::vector<int> accepted_count(B, -1);
  std::vector<int32_t> next_b(B, -1), next_d0(B, -1);
  const ModelSequence* seq_ptrs[B];
  for (int b = 0; b < B; ++b) seq_ptrs[b] = &seqs[b];
  s = MtpSpeculativeStepMulti(m, mtp, seq_ptrs, b_tok.data(), d0.data(), g_in,
                              B, k, accepted.data(), accepted_count.data(),
                              next_b.data(), next_d0.data(), next_g, nullptr);
  if (!s.ok()) {
    std::printf("  MtpSpeculativeStepMulti failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }
  // The step does NOT advance the sequences (Stage 2c contract); the caller
  // does. Advance each seq over its accepted prefix [b_b, d_0..d_{a_b-1}].
  for (int b = 0; b < B; ++b) {
    const int a = accepted_count[b];
    seqs[b].position += 1 + a;
    seqs[b].history.push_back(b_tok[b]);
    for (int i = 0; i < a; ++i)
      seqs[b].history.push_back(
          accepted[static_cast<size_t>(b) * (k + 1) + 1 + i]);
  }

  bool ok = true;
  for (int b = 0; b < B; ++b) {
    const std::vector<int32_t>& gt = (b == 0) ? gt0 : gt1;
    const int a = accepted_count[b];
    std::printf("  seq %d: accepted_count=%d next_b=%d next_d0=%d\n", b, a,
                next_b[b], next_d0[b]);
    if (a < 1 || a > k + 1) {
      std::printf("  FAIL: seq %d accepted_count %d out of range\n", b, a);
      ok = false;
      continue;
    }
    // accepted[0..a-1] == gt[0..a-1] (bonus + accepted drafts).
    for (int i = 0; i < a; ++i) {
      if (accepted[static_cast<size_t>(b) * (k + 1) + i] != gt[i]) {
        std::printf("  FAIL: seq %d accepted[%d]=%d gt=%d\n", b, i,
                    accepted[static_cast<size_t>(b) * (k + 1) + i], gt[i]);
        ok = false;
      }
    }
    // next_b == the main model's greedy token at position P_b+a (gt[a]).
    if (next_b[b] != gt[a]) {
      std::printf("  FAIL: seq %d next_b=%d gt[%d]=%d\n", b, next_b[b], a,
                  gt[a]);
      ok = false;
    }
    // next_d0 is a valid vocab token (draft model prediction).
    if (next_d0[b] < 0 || next_d0[b] >= vocab) {
      std::printf("  FAIL: seq %d next_d0=%d out of vocab\n", b, next_d0[b]);
      ok = false;
    }
  }
  if (ok) std::printf("  PASS: batched multi-seq step matches ground truth\n");
  for (int b = 0; b < B; ++b) {
    cudaFree(d_full[b]);
    cudaFree(g_in[b]);
    cudaFree(next_g[b]);
  }
  mtp.Free();
  m.Free();
  return ok;
}
