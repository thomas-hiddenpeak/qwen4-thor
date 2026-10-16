// Checkpoint contracts on a controlled 2-layer model (linear + linear/PLE).
// A/B: saved-row copy round trips, save/no-save logits, and repeated saves
// must be bit-exact. Capacity exceeds the number of written rows.
// C/D/E: retain the historical cross-shape logits/state observations. Different
// projection reductions can cross a BF16 rounding midpoint and change routed
// experts; a fixed final-logit L2 threshold is not a checkpoint oracle.
// F: independently execute just the accepted prefix, without saving/restoring
// checkpoints. Its recurrent state and subsequent decode output must exactly
// match rollback (reject/partial) or the full batch-final state (full accept).
// Restoring a deliberately wrong row must change the output in each case.
// See docs/evidence/mtp-numerical-closure-20261016 for the original failures,
// projection-level high-precision reference and PLE upload race regression.
// This does not replace the full-model N=4 ULP/causality contract.
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
#include <memory>
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
using q4t::model::ModelSnapshotState;
using q4t::model::ModelSnapshotCheckpoint;
using q4t::model::ModelStateSnapshot;

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
// RAII: temporarily set Q4T_MLP_DUMP=<tag> so the decoder-layer boundary
// hooks (DumpTrunkBeforeMix / DumpMoeBoundary / DumpLayerOut /
// DumpPleEmbeddings) write per-layer [T, dim] BF16 dumps for the enclosed
// forward(s), and Q4T_LIN_DUMP=<tag> so the linear-attention intermediate
// hooks (DumpLinearIntermediates / DumpLinearGatedYssm: x, qkv_raw, qkv, z,
// a, beta, y_ssm, y_ssm_gated) dump into the same <tag>_m<idx> base. The
// hooks' static layer counter resets when the tag changes, so each distinct
// tag yields its own _m0/_m1/... index sequence.
struct MlpDumpScope {
  explicit MlpDumpScope(const std::string& tag) : tag_(tag) {
    const char* prev = std::getenv("Q4T_MLP_DUMP");
    had_prev_ = (prev != nullptr);
    if (had_prev_) prev_ = prev;
    setenv("Q4T_MLP_DUMP", tag_.c_str(), 1);
    const char* prev_lin = std::getenv("Q4T_LIN_DUMP");
    had_prev_lin_ = (prev_lin != nullptr);
    if (had_prev_lin_) prev_lin_ = prev_lin;
    setenv("Q4T_LIN_DUMP", tag_.c_str(), 1);
  }
  ~MlpDumpScope() {
    if (had_prev_) setenv("Q4T_MLP_DUMP", prev_.c_str(), 1);
    unsetenv("Q4T_MLP_DUMP");
    if (had_prev_lin_) setenv("Q4T_LIN_DUMP", prev_lin_.c_str(), 1);
    unsetenv("Q4T_LIN_DUMP");
  }
  std::string tag_;
  std::string prev_;
  bool had_prev_ = false;
  std::string prev_lin_;
  bool had_prev_lin_ = false;
};
bool SnapEqual(const ModelStateSnapshot& a, const ModelStateSnapshot& b,
               size_t* first_diff_off = nullptr) {
  if (!a.valid || !b.valid || a.bytes != b.bytes ||
      a.data.size() != b.data.size()) {
    return false;
  }
  for (size_t i = 0; i < a.data.size(); ++i) {
    if (a.data[i] != b.data[i]) {
      if (first_diff_off) *first_diff_off = i;
      return false;
    }
  }
  return true;
}
bool BytesEqual(const uint16_t* a, const uint16_t* b, size_t n) {
  return std::memcmp(a, b, n * sizeof(uint16_t)) == 0;
}

}  // namespace

Q4T_TEST(model_verify_ckpt_samepath) {
  // Force the shared-state GDN kernel everywhere (Q4T_GDN_REG=0): the
  // verify-batch path (ssm_ckpt present) always uses it, while the
  // no-checkpoint prefill/decode paths use the register-state kernel by
  // default. Save-inertness and independent-prefix comparisons require
  // both sides on one GDN kernel. Cross-shape Part C remains diagnostic;
  // its old 0.02 observation line does not establish chunk invariance.
  q4t::test::GdnRegOff gdn_reg_off;
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar)) {
    Q4T_SKIP("(skipped: model or PLE sidecar not found)");
  }

  // 2 layers: layer 0 linear attention, layer 1 linear attention + PLE
  // (exercises the SSM/conv AND PLE conv checkpoint paths).
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

  // cap=4 > T-1=2 (same cap>count shape as the stride-contract regression).
  const int cap = 4;
  s = ModelReserveVerifyCheckpoints(m, cap);
  if (!s.ok()) {
    std::printf("  reserve checkpoints (cap=%d) failed: %s\n", cap,
                s.message().c_str());
    m.Free();
    return false;
  }

  const int T = 3;        // verify tokens (num_ckpt = T-1 = 2 < cap)
  const int Tprompt = 4;  // prompt length
  const int32_t prompt[] = {846, 25, 1203, 321};
  const int32_t verify[] = {236644, 11, 22};
  const int32_t probe = 15;  // extra token for the full-accept boundary

  uint16_t* d_blogits = nullptr;
  uint16_t* d_reflogits = nullptr;  // [Tprompt+T+1, vocab]
  uint16_t* d_step_logits = nullptr;
  auto mal = [&](void** p, size_t bytes) {
    return cudaMalloc(p, bytes) == cudaSuccess;
  };
  if (!mal(reinterpret_cast<void**>(&d_blogits),
           static_cast<size_t>(T) * vocab * 2) ||
      !mal(reinterpret_cast<void**>(&d_reflogits),
           static_cast<size_t>(Tprompt + T + 1) * vocab * 2) ||
      !mal(reinterpret_cast<void**>(&d_step_logits),
           static_cast<size_t>(vocab) * 2)) {
    std::printf("  cudaMalloc failed\n");
    m.Free();
    return false;
  }

  // Opt-in per-layer boundary dump (Q4T_S7_LAYERDUMP=<dir>): capture the
  // T=8 prefill(8) reference and the T=1 full-accept decode probe so the
  // first diverging operator boundary can be located in true op order
  // (trunk_in -> attn -> moe_in -> MoE -> moe_out -> combine -> out).
  const char* ld_env = std::getenv("Q4T_S7_LAYERDUMP");
  const std::string ld_dir = ld_env ? ld_env : "";
  if (!ld_dir.empty()) {
    const int rc =
        std::system((std::string("mkdir -p '") + ld_dir + "'").c_str());
    (void)rc;
  }

  // ---- Non-speculative reference: prefill prompt+verify+probe -----------
  std::vector<int32_t> full(Tprompt + T + 1);
  for (int t = 0; t < Tprompt; ++t) full[t] = prompt[t];
  for (int t = 0; t < T; ++t) full[Tprompt + t] = verify[t];
  full[Tprompt + T] = probe;
  {
    std::unique_ptr<MlpDumpScope> dump_ref8;
    if (!ld_dir.empty()) dump_ref8.reset(new MlpDumpScope(ld_dir + "/ref8"));
    ModelSequence rseq;
    s = ModelBeginSequence(m, &rseq, nullptr, 0);
    if (!s.ok()) {
      std::printf("  ref begin seq failed: %s\n", s.message().c_str());
      m.Free();
      return false;
    }
    s = ModelPrefill(m, &rseq, full.data(), Tprompt + T + 1, d_reflogits,
                     nullptr, nullptr, nullptr, 0);
    if (!s.ok()) {
      std::printf("  ref prefill failed: %s\n", s.message().c_str());
      m.Free();
      return false;
    }
    ModelEndSequence(&rseq);
  }
  std::vector<uint16_t> ref(
      static_cast<size_t>(Tprompt + T + 1) * vocab);
  cudaMemcpy(ref.data(), d_reflogits,
             static_cast<size_t>(Tprompt + T + 1) * vocab * 2,
             cudaMemcpyDeviceToHost);

  auto run_batch = [&](bool save, std::vector<uint16_t>* out_logits,
                       ModelSequence* seq) -> Status {
    s = ModelBeginSequence(m, seq, nullptr, 0);
    if (!s.ok()) return s;
    s = ModelPrefill(m, seq, prompt, Tprompt, d_reflogits, nullptr, nullptr,
                     nullptr, 0);
    if (!s.ok()) return s;
    std::vector<int32_t> history(Tprompt);
    for (int t = 0; t < Tprompt; ++t) history[t] = prompt[t];
    s = ModelDecodeBatch(m, verify, T, Tprompt, history.data(),
                         history.size(), d_blogits, nullptr, nullptr, save,
                         /*seq_id=*/0);
    if (!s.ok()) return s;
    if (m.verify_ssm_record_fold != save)
      return Status::Fail("T=3 checkpoint record/fold dispatch mismatch");
    // Keep the normal asynchronous boundary: PLE owns its upload staging,
    // and subsequent device work on this stream must not corrupt it.
    if (out_logits) {
      out_logits->resize(static_cast<size_t>(T) * vocab);
      if (cudaMemcpy(out_logits->data(), d_blogits,
                     static_cast<size_t>(T) * vocab * 2,
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        return Status::Fail("D2H batch logits");
      }
    }
    return Status();
  };

  bool ok = true;
  const char* stage = "";

  // ---- Part A: round-trip losslessness (bit-exact, no kernel re-run) ----
  stage = "A round-trip";
  ModelStateSnapshot c0, c1, s0, s1;
  {
    ModelSequence seq;
    if (!run_batch(/*save=*/true, nullptr, &seq).ok()) {
      std::printf("  [A] batch(save) failed\n");
      m.Free();
      return false;
    }
    if (!ModelSnapshotCheckpoint(m, 0, &c0, nullptr, 0).ok() ||
        !ModelSnapshotCheckpoint(m, 1, &c1, nullptr, 0).ok()) {
      std::printf("  [A] snapshot checkpoint failed\n");
      m.Free();
      return false;
    }
    if (c0.bytes != c1.bytes || !SnapEqual(c0, c1)) {
      std::printf("  [A] sanity: ckpt0 != ckpt1 (distinct states) OK, bytes=%zu\n",
                  c0.bytes);
    } else {
      std::printf("  [A] sanity: ckpt0 == ckpt1 — UNEXPECTED (states should "
                  "differ)\n");
      ok = false;
    }
    if (!ModelRestoreCheckpoint(m, 1, nullptr, 0).ok()) {
      std::printf("  [A] restore ckpt1 failed\n");
      m.Free();
      return false;
    }
    if (!ModelSnapshotState(m, &s1, nullptr).ok()) {
      std::printf("  [A] snapshot state failed\n");
      m.Free();
      return false;
    }
    size_t off1 = 0;
    const bool rt1 = SnapEqual(s1, c1, &off1);
    std::printf("  [A] restore(1)->live == ckpt[1] direct: %s (bytes=%zu%s)\n",
                rt1 ? "BIT-EXACT" : "MISMATCH", c1.bytes,
                rt1 ? "" : ", first_diff_off");
    if (!rt1) {
      std::printf("        first_diff_off=%zu\n", off1);
      ok = false;
    }
    if (!ModelRestoreCheckpoint(m, 0, nullptr, 0).ok()) {
      std::printf("  [A] restore ckpt0 failed\n");
      m.Free();
      return false;
    }
    if (!ModelSnapshotState(m, &s0, nullptr).ok()) {
      std::printf("  [A] snapshot state failed\n");
      m.Free();
      return false;
    }
    size_t off0 = 0;
    const bool rt0 = SnapEqual(s0, c0, &off0);
    std::printf("  [A] restore(0)->live == ckpt[0] direct: %s (bytes=%zu)\n",
                rt0 ? "BIT-EXACT" : "MISMATCH", c0.bytes);
    if (!rt0) {
      std::printf("        first_diff_off=%zu\n", off0);
      ok = false;
    }
    ModelEndSequence(&seq);
  }

  // ---- Part B: save inertness + same-path determinism (bit-exact) -------
  stage = "B inert/determinism";
  {
    std::vector<uint16_t> l_nosave, l_save;
    ModelSequence seq;
    if (!run_batch(/*save=*/false, &l_nosave, &seq).ok()) {
      std::printf("  [B] batch(nosave) failed\n");
      m.Free();
      return false;
    }
    ModelEndSequence(&seq);
    if (!run_batch(/*save=*/true, &l_save, &seq).ok()) {
      std::printf("  [B] batch(save) failed\n");
      m.Free();
      return false;
    }
    const bool inert =
        BytesEqual(l_nosave.data(), l_save.data(), l_nosave.size());
    std::printf("  [B] batch logits save vs nosave: %s (T=%d, vocab=%d)\n",
                inert ? "BIT-EXACT" : "MISMATCH", T, vocab);
    if (!inert) ok = false;
    ModelStateSnapshot c1b;
    if (!ModelSnapshotCheckpoint(m, 1, &c1b, nullptr, 0).ok()) {
      std::printf("  [B] snapshot checkpoint failed\n");
      m.Free();
      return false;
    }
    size_t offd = 0;
    const bool det = SnapEqual(c1b, c1, &offd);
    std::printf("  [B] same-path re-run ckpt[1] deterministic: %s\n",
                det ? "BIT-EXACT" : "MISMATCH");
    if (!det) {
      std::printf("        first_diff_off=%zu\n", offd);
      ok = false;
    }
    ModelEndSequence(&seq);
  }

  // ---- Part C: accept 0/partial/full output boundary ---------------------
  stage = "C accept boundary";
  auto decode_after_restore = [&](int a, int32_t token, int pos,
                                  float* l2) -> bool {
    ModelSequence seq;
    if (!run_batch(/*save=*/true, nullptr, &seq).ok()) return false;
    if (!ModelRestoreCheckpoint(m, a, nullptr, 0).ok()) return false;
    // Align the seq state machine to the accepted boundary: position =
    // Tprompt+a+1, history += verify[0..a].
    seq.position = Tprompt + a + 1;
    for (int k = 0; k <= a; ++k) seq.history.push_back(verify[k]);
    std::unique_ptr<MlpDumpScope> dump_partial;
    if (!ld_dir.empty()) {
      dump_partial.reset(new MlpDumpScope(ld_dir + "/partial" +
                                         std::to_string(a)));
    }
    s = ModelDecodeStepSeq(m, &seq, token, d_step_logits, nullptr, nullptr,
                           /*seq_id=*/0);
    if (!s.ok()) return false;
    std::vector<uint16_t> rb(vocab);
    if (cudaMemcpy(rb.data(), d_step_logits, vocab * 2,
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      return false;
    }
    const size_t ref_off =
        static_cast<size_t>(pos) * static_cast<size_t>(vocab);
    std::vector<uint16_t> rb_ref(ref.begin() + ref_off,
                                 ref.begin() + ref_off + vocab);
    *l2 = L2Rel(rb, rb_ref);
    ModelEndSequence(&seq);
    return true;
  };
  {
    float l2a0 = 1e9f, l2a1 = 1e9f;
    if (decode_after_restore(/*a=*/0, verify[1], Tprompt + 1, &l2a0)) {
      std::printf("  [C] accept=0 (full reject): restore ckpt[0], decode "
                  "verify[1]: l2_rel=%.5f %s\n",
                  l2a0, l2a0 < 0.02f ? "OK" : "MISMATCH");
      // Cross-shape observation; Part F enforces exact independent boundaries.
    } else {
      std::printf("  [C] accept=0 path failed\n");
      ok = false;
    }
    if (decode_after_restore(/*a=*/1, verify[2], Tprompt + 2, &l2a1)) {
      std::printf("  [C] accept=1 (partial): restore ckpt[1], decode "
                  "verify[2]: l2_rel=%.5f %s\n",
                  l2a1, l2a1 < 0.02f ? "OK" : "MISMATCH");
      // Cross-shape observation; Part F enforces exact independent boundaries.
    } else {
      std::printf("  [C] accept=1 path failed\n");
      ok = false;
    }
    // Full accept (a=2): fresh batch final state (NO restore), decode the
    // probe at position Tprompt+3 vs the prefill reference row.
    //
    // No restore operation participates in this cross-shape diagnostic.
    // Preserve its original 0.02 observation line. Part F independently
    // checks the full accepted-prefix state/output with exact equality.
    float l2a2 = 1e9f;
    {
      // O0/O1 operator capture (opt-in via Q4T_S7_CAPTURE=<dir>): dump the
      // HC inject gate inputs/weight/outputs of the full-accept decode so the
      // operator-level comparison test (hc_inject_actual_s7) can run the
      // production operators on the exact failing inputs. Purely additive;
      // does not change the pass/fail outcome or exit code.
      const char* cap = std::getenv("Q4T_S7_CAPTURE");
      if (cap != nullptr && *cap != '\0') {
        const int mkdir_rc =
            std::system((std::string("mkdir -p '") + cap + "'").c_str());
        (void)mkdir_rc;
        setenv("Q4T_OP_CAPTURE", cap, 1);
      }
      ModelSequence seq;
      if (run_batch(/*save=*/true, nullptr, &seq).ok()) {
        seq.position = Tprompt + T;
        for (int k = 0; k < T; ++k) seq.history.push_back(verify[k]);
        std::unique_ptr<MlpDumpScope> dump_probe;
        if (!ld_dir.empty())
          dump_probe.reset(new MlpDumpScope(ld_dir + "/probe1"));
        s = ModelDecodeStepSeq(m, &seq, probe, d_step_logits, nullptr, nullptr,
                               /*seq_id=*/0);
        if (s.ok()) {
          std::vector<uint16_t> rb(vocab);
          if (cudaMemcpy(rb.data(), d_step_logits, vocab * 2,
                         cudaMemcpyDeviceToHost) == cudaSuccess) {
            const size_t ref_off =
                static_cast<size_t>(Tprompt + T) *
                static_cast<size_t>(vocab);
            std::vector<uint16_t> rb_ref(ref.begin() + ref_off,
                                         ref.begin() + ref_off + vocab);
            l2a2 = L2Rel(rb, rb_ref);
            std::printf("  [C] accept=2 (full): batch final state, decode "
                        "probe: l2_rel=%.5f %s\n",
                        l2a2, l2a2 < 0.02f ? "OK" : "MISMATCH");
            // Cross-shape observation; Part F enforces exact independent boundaries.
          } else {
            std::printf("  [C] accept=2 D2H failed\n");
            ok = false;
          }
        } else {
          std::printf("  [C] accept=2 decode failed: %s\n",
                      s.message().c_str());
          ok = false;
        }
        ModelEndSequence(&seq);
      } else {
        std::printf("  [C] accept=2 batch failed\n");
        ok = false;
      }
      if (cap != nullptr && *cap != '\0') {
        cudaDeviceSynchronize();
        unsetenv("Q4T_OP_CAPTURE");
      }
    }
  }

  // ---- Part D (A2): state-level decomposition (diagnostic only) --------
  // Three runs ending at the SAME logical position (after Tprompt+T
  // tokens: 4 prompt + 3 verify), compared component by component.
  // Layout must match CollectRecurrentState (per layer: ssm FP32, conv
  // BF16, then ple_conv BF16 if present).
  stage = "D state decomposition";
  {
    struct Comp {
      std::string name;
      size_t off;
      size_t bytes;
      bool f32;
    };
    std::vector<Comp> comps;
    {
      size_t off = 0;
      for (const auto& l : m.layers) {
        if (l.ssm_state) {
          const size_t ssm_b = static_cast<size_t>(l.linear.nv) *
                               l.linear.kd * l.linear.vd * 4;
          const size_t conv_b = static_cast<size_t>(l.linear.in_qkv()) *
                                (l.linear.conv_k - 1) * 2;
          comps.push_back({"L" + std::to_string(l.layer_id) + " ssm", off,
                           ssm_b, true});
          off += ssm_b;
          comps.push_back({"L" + std::to_string(l.layer_id) + " conv", off,
                           conv_b, false});
          off += conv_b;
        }
        if (l.ple_conv_state) {
          const size_t ple_b = static_cast<size_t>(l.hc_dim) *
                               (l.ple.conv_kernel - 1) *
                               l.ple.conv_dilation * 2;
          comps.push_back({"L" + std::to_string(l.layer_id) + " ple_conv",
                           off, ple_b, false});
          off += ple_b;
        }
      }
    }

    auto snapshot = [&]() {
      ModelStateSnapshot snap;
      if (!ModelSnapshotState(m, &snap, nullptr).ok()) {
        std::printf("  [D] snapshot failed\n");
      }
      return snap;
    };

    // S_batch: prefill(4) + DecodeBatch(T=3) (full-accept state).
    ModelStateSnapshot S_batch;
    {
      ModelSequence seq;
      if (run_batch(/*save=*/false, nullptr, &seq).ok()) {
        S_batch = snapshot();
      } else {
        std::printf("  [D] S_batch run failed\n");
      }
      ModelEndSequence(&seq);
    }

    // S_pref7: single prefill(7) (non-speculative reference state).
    ModelStateSnapshot S_pref7;
    {
      ModelSequence seq;
      s = ModelBeginSequence(m, &seq, nullptr, 0);
      if (s.ok()) {
        s = ModelPrefill(m, &seq, full.data(), Tprompt + T, d_reflogits,
                         nullptr, nullptr, nullptr, 0);
      }
      if (s.ok()) {
        S_pref7 = snapshot();
      } else {
        std::printf("  [D] S_pref7 run failed: %s\n", s.message().c_str());
      }
      ModelEndSequence(&seq);
    }

    // S_seq: prefill(4) + 3x sequential decode (greedy T=1 path).
    ModelStateSnapshot S_seq;
    {
      ModelSequence seq;
      s = ModelBeginSequence(m, &seq, nullptr, 0);
      if (s.ok()) {
        s = ModelPrefill(m, &seq, prompt, Tprompt, d_reflogits, nullptr,
                         nullptr, nullptr, 0);
      }
      for (int k = 0; s.ok() && k < T; ++k) {
        s = ModelDecodeStepSeq(m, &seq, verify[k], d_step_logits, nullptr,
                               nullptr, /*seq_id=*/0);
      }
      if (s.ok()) {
        S_seq = snapshot();
      } else {
        std::printf("  [D] S_seq run failed: %s\n", s.message().c_str());
      }
      ModelEndSequence(&seq);
    }

    auto comp_stats = [&](const char* tag, const ModelStateSnapshot& a,
                          const ModelStateSnapshot& b) {
      if (!a.valid || !b.valid || a.bytes != b.bytes) {
        std::printf("  [D] %s: snapshot invalid or size mismatch\n", tag);
        return;
      }
      for (const auto& c : comps) {
        const uint8_t* pa = a.data.data() + c.off;
        const uint8_t* pb = b.data.data() + c.off;
        size_t diff_bytes = 0;
        size_t first_off = SIZE_MAX;
        double num = 0.0, den = 0.0, maxabs = 0.0;
        const size_t n = c.bytes / (c.f32 ? 4 : 2);
        for (size_t i = 0; i < n; ++i) {
          float x, y;
          if (c.f32) {
            std::memcpy(&x, pa + i * 4, 4);
            std::memcpy(&y, pb + i * 4, 4);
          } else {
            uint16_t xb, yb;
            std::memcpy(&xb, pa + i * 2, 2);
            std::memcpy(&yb, pb + i * 2, 2);
            x = Bf16ToFloat(xb);
            y = Bf16ToFloat(yb);
          }
          if (x != y) {
            if (first_off == SIZE_MAX) first_off = i;
            diff_bytes += c.f32 ? 4 : 2;
          }
          const double d = static_cast<double>(x - y);
          num += d * d;
          den += static_cast<double>(y) * y;
          if (std::fabs(d) > maxabs) maxabs = std::fabs(d);
        }
        const float rel = den > 0.0 ? std::sqrt(num / den) : 0.0f;
        std::printf("  [D] %s %-11s: diff_bytes=%zu/%zu first=%s "
                    "maxabs=%.3e relL2=%.3e\n",
                    tag, c.name.c_str(), diff_bytes, c.bytes,
                    first_off == SIZE_MAX ? "-"
                                          : std::to_string(first_off).c_str(),
                    maxabs, rel);
      }
    };

    comp_stats("batch-vs-pref7", S_batch, S_pref7);
    comp_stats("batch-vs-seq", S_batch, S_seq);
    comp_stats("pref7-vs-seq", S_pref7, S_seq);

    // ---- Part E (A3): same-probe decode from each state -----------------
    // Isolates the state difference (Part D) from the pure
    // decode(T=1)-vs-prefill(T=8) path difference (S_pref7 case: the live
    // state is restored to the reference prefill(7) state, so any residual
    // is path-only).
    stage = "E probe path";
    const size_t ref7_off =
        static_cast<size_t>(Tprompt + T) * static_cast<size_t>(vocab);
    auto argmax = [&](const uint16_t* lg, size_t n) {
      int best = 0;
      float bv = -1e30f;
      for (size_t i = 0; i < n; ++i) {
        const float v = Bf16ToFloat(lg[i]);
        if (v > bv) {
          bv = v;
          best = static_cast<int>(i);
        }
      }
      return best;
    };
    const int ref7_argmax = argmax(ref.data() + ref7_off, vocab);

    auto probe_from_state = [&](const char* tag,
                                const ModelStateSnapshot& st) {
      ModelSequence seq;
      s = ModelBeginSequence(m, &seq, nullptr, 0);
      if (!s.ok()) return;
      s = ModelPrefill(m, &seq, prompt, Tprompt, d_reflogits, nullptr,
                       nullptr, nullptr, 0);
      if (!s.ok()) return;
      for (int k = 0; k < T; ++k) seq.history.push_back(verify[k]);
      seq.position = Tprompt + T;
      if (!ModelRestoreState(m, st, nullptr).ok()) {
        std::printf("  [E] %s: restore failed\n", tag);
        return;
      }
      s = ModelDecodeStepSeq(m, &seq, probe, d_step_logits, nullptr, nullptr,
                             /*seq_id=*/0);
      if (!s.ok()) {
        std::printf("  [E] %s: decode failed: %s\n", tag,
                    s.message().c_str());
        return;
      }
      std::vector<uint16_t> rb(vocab);
      if (cudaMemcpy(rb.data(), d_step_logits, vocab * 2,
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  [E] %s: D2H failed\n", tag);
        return;
      }
      std::vector<uint16_t> rb_ref(ref.begin() + ref7_off,
                                   ref.begin() + ref7_off + vocab);
      const float l2 = L2Rel(rb, rb_ref);
      const int am = argmax(rb.data(), rb.size());
      std::printf("  [E] %s: probe l2_rel=%.5f argmax=%d (ref argmax=%d) "
                  "%s\n",
                  tag, l2, am, ref7_argmax,
                  am == ref7_argmax ? "ARGMAX-MATCH" : "ARGMAX-DIFF");
      ModelEndSequence(&seq);
    };

    if (S_batch.valid) probe_from_state("batch-final (=C a=2)", S_batch);
    if (S_pref7.valid) probe_from_state("pref7 state (path-only)", S_pref7);
    if (S_seq.valid) probe_from_state("seq T1x3 state", S_seq);
  }

  // Independent prefix oracle: execute only the accepted tokens, without
  // saving/restoring checkpoints, then compare its state with the saved row.
  // Part C retains the original cross-shape observations while diagnosing
  // whether the rollback boundary itself differs from prefix execution.
  stage = "F independent prefix";
  for (int a = 0; a < T; ++a) {
    ModelSequence seq;
    s = ModelBeginSequence(m, &seq, nullptr, 0);
    if (s.ok()) {
      s = ModelPrefill(m, &seq, prompt, Tprompt, d_reflogits, nullptr,
                       nullptr, nullptr, 0);
    }
    if (s.ok()) {
      s = ModelDecodeBatch(m, verify, a + 1, Tprompt, prompt, Tprompt,
                           d_blogits, nullptr, nullptr, false, 0);
    }
    if (!s.ok() || cudaStreamSynchronize(nullptr) != cudaSuccess) {
      std::printf("  [F] prefix execution failed (a=%d)\n", a);
      ok = false;
      ModelEndSequence(&seq);
      continue;
    }
    ModelStateSnapshot prefix;
    if (!ModelSnapshotState(m, &prefix, nullptr).ok()) {
      ok = false;
      ModelEndSequence(&seq);
      continue;
    }
    size_t first = 0;
    const bool equal = a == T - 1 ||
                       SnapEqual(prefix, a == 0 ? c0 : c1, &first);
    if (a < T - 1) {
      std::printf("  [F] independent prefix a=%d vs checkpoint: %s "
                  "first_diff=%zu\n", a,
                  equal ? "BIT-EXACT" : "MISMATCH", first);
    }
    if (!equal) ok = false;
    auto decode_boundary = [&](ModelSequence* current,
                               std::vector<uint16_t>* output) {
      current->position = Tprompt + a + 1;
      current->history.assign(prompt, prompt + Tprompt);
      current->history.insert(current->history.end(), verify, verify + a + 1);
      const int32_t next = a + 1 < T ? verify[a + 1] : probe;
      Status result = ModelDecodeStepSeq(m, current, next,
                                         d_step_logits, nullptr, nullptr, 0);
      output->resize(vocab);
      return result.ok() &&
             cudaMemcpy(output->data(), d_step_logits, vocab * 2,
                        cudaMemcpyDeviceToHost) == cudaSuccess;
    };
    std::vector<uint16_t> prefix_logits;
    if (!decode_boundary(&seq, &prefix_logits)) ok = false;
    ModelEndSequence(&seq);
    for (bool wrong_row : {false, true}) {
      std::vector<uint16_t> restored_logits;
      const int row = a == T - 1 ? (wrong_row ? 0 : -1)
                                 : (wrong_row ? 1 - a : a);
      bool ran = run_batch(true, nullptr, &seq).ok();
      if (ran && row >= 0) {
        ran = ModelRestoreCheckpoint(m, row, nullptr, 0).ok();
      }
      if (ran && row == -1) {
        ModelStateSnapshot full;
        ran = ModelSnapshotState(m, &full, nullptr).ok() &&
              SnapEqual(prefix, full);
        std::printf("  [F] independent full-prefix state: %s\n",
                    ran ? "BIT-EXACT" : "MISMATCH");
      }
      ran = ran && decode_boundary(&seq, &restored_logits);
      const bool logits_equal = ran && prefix_logits == restored_logits;
      const bool passed = ran && logits_equal != wrong_row;
      std::printf("  [F] prefix output a=%d row=%d wrong=%d: %s\n", a,
                  row, wrong_row, passed ? "PASS" : "FAIL");
      if (!passed) ok = false;
      ModelEndSequence(&seq);
    }
  }

  cudaFree(d_blogits);
  cudaFree(d_reflogits);
  cudaFree(d_step_logits);
  m.Free();
  std::printf("  stage=%s (last)\n", stage);
  return ok;
}
