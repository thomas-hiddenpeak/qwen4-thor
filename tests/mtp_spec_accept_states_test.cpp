// Contract update 2026-10-16: accepted-state outputs use independent prefix
// execution with exact equality. Historical cross-shape L2 remains reported;
// it cannot serve as a state-recovery oracle (projection rounding differs).
// S8: forced accept 0/partial/full output semantics for MtpSpeculativeStep
// (2026-10-11 contract review), on a controlled 2-layer model using the
// test-only Q4T_MTP_TEST_DRAFTS hook (overrides drafts[1..k-1]; drafts[0]
// stays the draft model's d0).
//
// The contract review left the accept/reject output boundary unverified
// beyond code review + 2-layer checkpoint regression (S3). This test drives
// MtpSpeculativeStep into each accept state and checks the emitted semantics
// against the SAME-PATH verify-batch argmax (NOT a plain-greedy assumption,
// per the review): for forced drafts [d0, f1, f2] and bonus b, the expected
// accept count a* is the longest prefix of [d0, f1, f2] matching m[0..2],
// where m[i] = argmax of the main model's verify batch [b, d0, f1, f2] row i
// (probed with the same ModelDecodeBatch call the step uses internally,
// save_checkpoints=false; S7 Part B proved save is bit-exact inert).
//
// Checks per case (a* in {0, 1, 3}):
//   - accepted_count == 1 + a*
//   - accepted_tokens == [b, d0, f1..f_{a*-1}]
//   - next_b == m[a*]
//   - seq advanced by exactly 1 + a* (position + history content)
//   - decode boundary: decode R[5+a*] at position 5+a* (R = the
//     non-speculative reference sequence [prompt, b, d0, f1, f2, probe])
//     vs independent accepted-prefix execution, bit-exact. Cross-shape
//     prefill L2 remains diagnostic, with its original threshold displayed.
//
// Realizing each accept state: d0 is a CALL PARAMETER of MtpSpeculativeStep,
// so it is CONSTRUCTED directly from the same-path verify reference (no bonus
// search, no favorable-sample picking; the 2-layer controlled draft rarely
// matches the 2-layer main model):
//   - a*=0: d0 = (m[0]+1) % vocab (d0 != m[0]); f1/f2 arbitrary.
//   - a*=1: d0 = m[0]; f1 = (m[1]+1) % vocab (f1 != m[1]); f2 arbitrary.
//   - a*=3: d0 = m[0]; f1 = m[1]; f2 = m[2].
// m[0] is independent of the drafts (verify row 0 sees only [prompt, b]);
// m[1] depends on d0; m[2] on d0 and f1 (probed sequentially). f1/f2 are
// still fed through the existing Q4T_MTP_TEST_DRAFTS hook. b/g/history/
// reference inputs are kept consistent across cases. This is a controlled
// API boundary test of MtpSpeculativeStep, NOT a draft-model quality test
// and NOT full-model acceptance. The model is a fixed checkpoint with
// deterministic greedy paths (no RNG, S4), so the realized states are stable
// across runs.
//
// GdnRegOff: the post-step state is verify-batch (shared-state GDN) derived;
// forcing the shared kernel on the reference prefill and the decode step
// keeps the comparison on one GDN kernel. Different projection shapes are
// still distinct arithmetic paths; they do not share a fixed L2 guarantee.
//
// Boundary: 2-layer controlled model; verifies the accept-state output
// boundary of MtpSpeculativeStep, NOT full-model MTP numerics and NOT
// plain-greedy equivalence.
//
// Skipped when CUDA or the model is absent.
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
using q4t::mtp::LoadMtp;
using q4t::mtp::MtpConfig;
using q4t::mtp::MtpModel;
using q4t::mtp::MtpResetState;
using q4t::mtp::MtpReserveScratch;
using q4t::mtp::MtpDraftExtend;
using q4t::mtp::MtpSpeculativeStep;

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
// Lowest-index tiebreak, matching ArgmaxBf16Rows inside MtpSpeculativeStep.
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

// RAII: hold Q4T_MTP_TEST_DRAFTS for one speculative step, restoring the
// prior value on exit (including early returns).
struct DraftsEnv {
  std::string value;
  bool had_prev = false;
  std::string prev;
  explicit DraftsEnv(std::string v) : value(std::move(v)) {
    const char* p = std::getenv("Q4T_MTP_TEST_DRAFTS");
    if (p != nullptr) {
      had_prev = true;
      prev = p;
    }
    setenv("Q4T_MTP_TEST_DRAFTS", value.c_str(), 1);
  }
  ~DraftsEnv() {
    if (had_prev)
      setenv("Q4T_MTP_TEST_DRAFTS", prev.c_str(), 1);
    else
      unsetenv("Q4T_MTP_TEST_DRAFTS");
  }
};

}  // namespace

Q4T_TEST(mtp_spec_accept_states) {
  q4t::test::GdnRegOff gdn_reg_off;
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  if (!FileExists(kIndex) || !FileExists(kMtpDir) ||
      !FileExists(kPleSidecar)) {
    Q4T_SKIP("(skipped: model or MTP dir not found)");
  }

  // 2 layers (layer 1 has PLE), single sequence.
  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = 2;
  cfg.max_prefill = 16;  // reference prefill R is 9 tokens
  cfg.max_seq = 1;
  cfg.ple_sidecar = kPleSidecar;
  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  main model load failed: %s\n", s.message().c_str());
    return false;
  }
  const int vocab = m.cfg.vocab;

  MtpConfig mcfg;
  mcfg.mtp_dir = kMtpDir;
  if (const char* subset = std::getenv("Q4T_TEST_SUBSET_HEAD"))
    mcfg.subset_head_dir = subset;
  MtpModel mtp;
  s = LoadMtp(mcfg, m.head.embed_tokens, m.head.lm_head, &mtp, nullptr);
  if (!s.ok()) {
    std::printf("  MTP load failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }

  const int k = 3;  // speculative drafts (verify T = k+1 = 4)
  const int T = 4;  // prompt length
  const int32_t prompt[4] = {846, 25, 1203, 321};
  const int32_t probe = 15;  // extra token for the full-accept boundary
  const size_t hc_dim = static_cast<size_t>(mcfg.hc) * mcfg.hs;

  uint16_t* d_trunk_full = nullptr;  // [T, hc_dim] main prefill trunks
  uint16_t* d_reflogits = nullptr;   // [9, vocab]
  uint16_t* d_blogits = nullptr;     // [k+1, vocab]
  uint16_t* d_step_logits = nullptr;  // [vocab]
  uint16_t* d_g = nullptr;            // [hc_dim] draft trunk
  uint16_t* d_g_next = nullptr;       // [hc_dim] next draft trunk
  auto mal = [&](void** p, size_t bytes) {
    return cudaMalloc(p, bytes) == cudaSuccess;
  };
  if (!mal(reinterpret_cast<void**>(&d_trunk_full),
           static_cast<size_t>(T) * hc_dim * 2) ||
      !mal(reinterpret_cast<void**>(&d_reflogits),
           static_cast<size_t>(9) * vocab * 2) ||
      !mal(reinterpret_cast<void**>(&d_blogits),
           static_cast<size_t>(k + 1) * vocab * 2) ||
      !mal(reinterpret_cast<void**>(&d_step_logits),
           static_cast<size_t>(vocab) * 2) ||
      !mal(reinterpret_cast<void**>(&d_g), hc_dim * 2) ||
      !mal(reinterpret_cast<void**>(&d_g_next), hc_dim * 2)) {
    std::printf("  cudaMalloc failed\n");
    mtp.Free();
    m.Free();
    return false;
  }

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

  // ---- helpers -------------------------------------------------------------
  // Fresh main state after the prompt; trunk_out rows feed the draft extend.
  auto prefill_prompt = [&](ModelSequence* seq) -> Status {
    Status st = ModelBeginSequence(m, seq, nullptr, 0);
    if (!st.ok()) return st;
    return ModelPrefill(m, seq, prompt, T, d_reflogits, nullptr, d_trunk_full);
  };
  // Draft KV extend for bonus b (EAGLE shift [t_1..t_{T-1}, b]); out d0 + g.
  // Touches only the draft model (MtpResetState + MtpDraftExtend).
  auto extend_for = [&](int32_t b, int32_t* d0_out, uint16_t* g_out) -> Status {
    Status st = MtpResetState(mtp, nullptr);
    if (!st.ok()) return st;
    std::vector<int32_t> shifted(T);
    for (int i = 0; i < T - 1; ++i) shifted[i] = prompt[i + 1];
    shifted[T - 1] = b;
    std::vector<int> pos(T);
    for (int i = 0; i < T; ++i) pos[i] = i;
    return MtpDraftExtend(mtp, shifted.data(), d_trunk_full, pos.data(), T,
                          d0_out, g_out, nullptr);
  };
  // Same-path verify-batch probe: [b, d0, f1, f2] at positions T..T+k,
  // save_checkpoints=false (S7 Part B: save is bit-exact inert on this
  // model). Returns host argmax m[0..k] (lowest-index tiebreak).
  auto probe_batch = [&](int32_t b, int32_t d0, int32_t f1, int32_t f2,
                         int32_t* m_out) -> Status {
    ModelSequence pseq;
    Status st = prefill_prompt(&pseq);
    if (!st.ok()) return st;
    const int32_t vtok[k + 1] = {b, d0, f1, f2};
    st = ModelDecodeBatch(m, vtok, k + 1, T, prompt, T, d_blogits, nullptr,
                          nullptr, /*save_checkpoints=*/false, /*seq_id=*/0);
    if (!st.ok()) {
      ModelEndSequence(&pseq);
      return st;
    }
    std::vector<uint16_t> lg(static_cast<size_t>(k + 1) * vocab);
    if (cudaMemcpy(lg.data(), d_blogits, lg.size() * 2,
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      ModelEndSequence(&pseq);
      return Status::Fail("probe D2H");
    }
    for (int i = 0; i <= k; ++i)
      m_out[i] = ArgmaxBf16(lg.data() + static_cast<size_t>(i) * vocab, vocab);
    ModelEndSequence(&pseq);
    return Status();
  };
  // Non-speculative reference prefill for R = [prompt, b, d0, f1, f2, probe].
  auto ref_prefill = [&](int32_t b, int32_t d0, int32_t f1, int32_t f2,
                         std::vector<uint16_t>* ref) -> Status {
    const int32_t R[9] = {prompt[0], prompt[1], prompt[2], prompt[3], b, d0,
                          f1, f2, probe};
    ModelSequence rseq;
    Status st = ModelBeginSequence(m, &rseq, nullptr, 0);
    if (!st.ok()) return st;
    st = ModelPrefill(m, &rseq, R, 9, d_reflogits, nullptr, nullptr);
    if (!st.ok()) {
      ModelEndSequence(&rseq);
      return st;
    }
    ref->resize(static_cast<size_t>(9) * vocab);
    if (cudaMemcpy(ref->data(), d_reflogits, ref->size() * 2,
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      ModelEndSequence(&rseq);
      return Status::Fail("ref D2H");
    }
    ModelEndSequence(&rseq);
    return Status();
  };

  // ---- Phase 1/2: natural bonus b0, probe, classify ------------------------
  int32_t b0 = -1;
  {
    ModelSequence seq0;
    s = prefill_prompt(&seq0);
    if (!s.ok()) {
      std::printf("  prefill failed: %s\n", s.message().c_str());
      mtp.Free();
      m.Free();
      return false;
    }
    std::vector<uint16_t> last(vocab);
    cudaMemcpy(last.data(),
               d_reflogits + static_cast<size_t>(T - 1) * vocab, vocab * 2,
               cudaMemcpyDeviceToHost);
    b0 = ArgmaxBf16(last.data(), vocab);
    ModelEndSequence(&seq0);
  }
  int32_t d0_0 = -1;
  s = extend_for(b0, &d0_0, d_g);
  if (!s.ok()) {
    std::printf("  extend b0 failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }
  int32_t m0[4];
  s = probe_batch(b0, d0_0, 11, 22, m0);
  if (!s.ok()) {
    std::printf("  probe b0 failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }
  std::printf("  b0=%d d0(b0)=%d m[0]=%d\n", b0, d0_0, m0[0]);

  // ---- Phase 3: construct d0 directly (controlled API boundary) -----------
  // d0 is a call parameter of MtpSpeculativeStep, so the accept states are
  // realized by CONSTRUCTING d0 from the same-path verify reference instead
  // of searching for a bonus where the draft model happens to agree (the
  // 2-layer controlled draft rarely matches the 2-layer main model, and
  // widening the bonus search would pick favorable samples). Subsequent
  // drafts (f1, f2) stay under the existing Q4T_MTP_TEST_DRAFTS hook; b/g/
  // history/reference inputs are kept consistent across cases. This is a
  // controlled API boundary test of MtpSpeculativeStep, NOT a draft-model
  // quality test and NOT full-model acceptance.
  //
  // m[0] is independent of the drafts (verify row 0 sees only [prompt, b]),
  // so probe with placeholders. m[1] depends on d0; m[2] on d0 and f1.
  int32_t m_probe[4];
  s = probe_batch(b0, 11, 11, 22, m_probe);
  if (!s.ok()) {
    std::printf("  probe m[0] failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }
  const int32_t m0_val = m_probe[0];
  const int32_t d0_reject = (m0_val + 1) % vocab;  // a*=0: d0 != m[0]
  const int32_t d0_accept = m0_val;                // a*=1/3: d0 == m[0]
  int32_t m1_probe[4];
  s = probe_batch(b0, d0_accept, 11, 22, m1_probe);
  if (!s.ok()) {
    std::printf("  probe m[1] failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }
  const int32_t m1 = m1_probe[1];
  const int32_t f1_partial = (m1 + 1) % vocab;  // a*=1: f1 != m[1]
  const int32_t f1_full = m1;                   // a*=3: f1 == m[1]
  int32_t m2_probe[4];
  s = probe_batch(b0, d0_accept, f1_full, 22, m2_probe);
  if (!s.ok()) {
    std::printf("  probe m[2] failed: %s\n", s.message().c_str());
    mtp.Free();
    m.Free();
    return false;
  }
  const int32_t f2_full = m2_probe[2];  // a*=3: f2 == m[2]
  std::printf("  constructed: m[0]=%d m[1]=%d m[2]=%d d0_reject=%d "
              "d0_accept=%d f1_partial=%d f2_full=%d\n",
              m0_val, m1, m2_probe[2], d0_reject, d0_accept, f1_partial,
              f2_full);

  // ---- Phase 4: run the three accept cases ---------------------------------
  // One case: fresh state + extend + reference + authoritative probe + forced
  // step + semantics checks + post-step decode boundary.
  auto run_case = [&](const char* name, int32_t b, int32_t d0, int32_t f1,
                      int32_t f2, int expect_a) -> bool {
    // 1. trunk for the extend (throwaway seq; its state is clobbered by the
    //    reference/probe prefills below).
    ModelSequence tmp;
    Status st = prefill_prompt(&tmp);
    if (!st.ok()) {
      std::printf("  [%s] prefill failed\n", name);
      return false;
    }
    ModelEndSequence(&tmp);
    // d0 is the constructed call parameter (Phase 3); extend_for only
    // refreshes the trunk g (its own d0 output is unused here).
    int32_t d0_actual = -1;
    st = extend_for(b, &d0_actual, d_g);
    (void)d0_actual;  // trunk g is what we use; the draft's own d0 is unused
    if (!st.ok()) {
      std::printf("  [%s] extend failed: %s\n", name, st.message().c_str());
      return false;
    }
    // 2. non-speculative reference for R = [prompt, b, d0, f1, f2, probe].
    std::vector<uint16_t> ref;
    st = ref_prefill(b, d0, f1, f2, &ref);
    if (!st.ok()) {
      std::printf("  [%s] ref prefill failed\n", name);
      return false;
    }
    // 3. authoritative same-path probe (the step's expected values).
    int32_t mp[4];
    st = probe_batch(b, d0, f1, f2, mp);
    if (!st.ok()) {
      std::printf("  [%s] probe failed\n", name);
      return false;
    }
    const int32_t drafts[k] = {d0, f1, f2};
    int a_star = 0;
    for (int i = 0; i < k; ++i) {
      if (drafts[i] != mp[i]) break;
      a_star = i + 1;
    }
    if (a_star != expect_a) {
      std::printf("  [%s] expected a=%d but probe gives a*=%d (m=[%d %d %d "
                  "%d])\n",
                  name, expect_a, a_star, mp[0], mp[1], mp[2], mp[3]);
      return false;
    }
    // 4. forced speculative step (fresh main state right before the step).
    ModelSequence seq;
    st = prefill_prompt(&seq);
    if (!st.ok()) {
      std::printf("  [%s] step prefill failed\n", name);
      return false;
    }
    char envbuf[64];
    std::snprintf(envbuf, sizeof(envbuf), "%d,%d", f1, f2);
    DraftsEnv env(envbuf);
    int32_t accepted[k + 1];
    int acount = -1;
    int32_t next_b = -1, next_d0 = -1;
    st = MtpSpeculativeStep(m, mtp, &seq, b, d0, d_g, k, accepted, &acount,
                            &next_b, &next_d0, d_g_next, nullptr);
    if (!st.ok()) {
      std::printf("  [%s] step failed: %s\n", name, st.message().c_str());
      return false;
    }
    // 5. semantics checks (expected values from the same-path probe).
    bool c = true;
    if (acount != 1 + expect_a) {
      std::printf("  [%s] accepted_count=%d expect %d\n", name, acount,
                  1 + expect_a);
      c = false;
    }
    if (accepted[0] != b) {
      std::printf("  [%s] accepted[0]=%d expect b=%d\n", name, accepted[0], b);
      c = false;
    }
    for (int i = 0; i < expect_a; ++i) {
      if (accepted[1 + i] != drafts[i]) {
        std::printf("  [%s] accepted[%d]=%d expect %d\n", name, 1 + i,
                    accepted[1 + i], drafts[i]);
        c = false;
      }
    }
    if (next_b != mp[expect_a]) {
      std::printf("  [%s] next_b=%d expect m[%d]=%d\n", name, next_b,
                  expect_a, mp[expect_a]);
      c = false;
    }
    if (seq.position != T + 1 + expect_a) {
      std::printf("  [%s] position=%d expect %d\n", name, seq.position,
                  T + 1 + expect_a);
      c = false;
    }
    if (static_cast<int>(seq.history.size()) != T + 1 + expect_a) {
      std::printf("  [%s] history size=%d expect %d\n", name,
                  static_cast<int>(seq.history.size()), T + 1 + expect_a);
      c = false;
    }
    for (int i = 0; i < T && c; ++i) {
      if (seq.history[i] != prompt[i]) {
        std::printf("  [%s] history[%d]=%d expect %d\n", name, i,
                    seq.history[i], prompt[i]);
        c = false;
      }
    }
    if (c && seq.history[T] != b) {
      std::printf("  [%s] history[%d]=%d expect b=%d\n", name, T,
                  seq.history[T], b);
      c = false;
    }
    for (int i = 0; i < expect_a && c; ++i) {
      if (seq.history[T + 1 + i] != drafts[i]) {
        std::printf("  [%s] history[%d]=%d expect %d\n", name, T + 1 + i,
                    seq.history[T + 1 + i], drafts[i]);
        c = false;
      }
    }
    // 6. post-step decode boundary vs the non-speculative reference.
    const int32_t R[9] = {prompt[0], prompt[1], prompt[2], prompt[3], b, d0,
                          f1, f2, probe};
    const int dec_pos = T + 1 + expect_a;  // 5 + a
    st = ModelDecodeStepSeq(m, &seq, R[dec_pos], d_step_logits, nullptr,
                            nullptr, /*seq_id=*/0);
    if (!st.ok()) {
      std::printf("  [%s] decode boundary failed: %s\n", name,
                  st.message().c_str());
      ModelEndSequence(&seq);
      return false;
    }
    std::vector<uint16_t> rb(vocab);
    cudaMemcpy(rb.data(), d_step_logits, static_cast<size_t>(vocab) * 2,
               cudaMemcpyDeviceToHost);
    const size_t ref_off =
        static_cast<size_t>(dec_pos) * static_cast<size_t>(vocab);
    std::vector<uint16_t> rb_ref(ref.begin() + ref_off,
                                 ref.begin() + ref_off + vocab);
    const float l2 = L2Rel(rb, rb_ref);
    // Cross-shape diagnostic only; exact accepted-prefix oracle follows.
    std::printf("  [%s] a=%d accepted_count=%d next_b=%d pos=%d l2_rel="
                "%.5f old_0.02=%s semantics=%s\n",
                name, expect_a, acount, next_b, seq.position, l2,
                l2 < 0.02f ? "WITHIN" : "EXCEEDED",
                c ? "PASS" : "FAIL");
    ModelEndSequence(&seq);
    // Independent reference: execute only the accepted prefix, with no
    // draft model, speculative step, or checkpoint restore.
    ModelSequence prefix_seq;
    st = ModelBeginSequence(m, &prefix_seq, nullptr, 0);
    if (st.ok()) {
      st = ModelPrefill(m, &prefix_seq, prompt, T, d_reflogits, nullptr,
                       nullptr, nullptr, 0);
    }
    if (st.ok()) {
      st = ModelDecodeBatch(m, R + T, 1 + expect_a, T, prompt, T,
                           d_blogits, nullptr, nullptr, false, 0);
    }
    if (!st.ok() || cudaStreamSynchronize(nullptr) != cudaSuccess) {
      ModelEndSequence(&prefix_seq);
      return false;
    }
    prefix_seq.position = dec_pos;
    prefix_seq.history.assign(R, R + dec_pos);
    st = ModelDecodeStepSeq(m, &prefix_seq, R[dec_pos], d_step_logits,
                           nullptr, nullptr, 0);
    std::vector<uint16_t> independent(vocab);
    const bool read = st.ok() &&
        cudaMemcpy(independent.data(), d_step_logits, vocab * 2,
                   cudaMemcpyDeviceToHost) == cudaSuccess;
    const bool prefix_equal = read && rb == independent;
    std::printf("  [%s] independent accepted-prefix output: %s\n", name,
                prefix_equal ? "BIT-EXACT" : "MISMATCH");
    if (!prefix_equal) c = false;
    ModelEndSequence(&prefix_seq);
    return c;
  };

  bool ok = true;
  // a*=0 (full reject): d0 != m[0] by construction; f's arbitrary.
  ok &= run_case("reject", b0, d0_reject, 11, 22, 0);
  // a*=1 (partial): d0 == m[0] by construction; f1 != m[1] by construction.
  ok &= run_case("partial", b0, d0_accept, f1_partial, 22, 1);
  ok &= run_case("partial2", b0, d0_accept, f1_full,
                 (f2_full + 1) % vocab, 2);
  // a*=3 (full): d0 == m[0], f1 == m[1], f2 == m[2] by construction.
  ok &= run_case("full", b0, d0_accept, f1_full, f2_full, 3);

  cudaFree(d_trunk_full);
  cudaFree(d_reflogits);
  cudaFree(d_blogits);
  cudaFree(d_step_logits);
  cudaFree(d_g);
  cudaFree(d_g_next);
  mtp.Free();
  m.Free();
  return ok;
}
