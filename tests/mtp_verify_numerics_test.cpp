// Targeted differential test for the MTP vs plain greedy output divergence.
//
// Background: the MTP k=3 E2E baseline (2026-10-10) found MTP-on output
// differing from MTP-off (plain greedy) output at the 1K tier (first text
// diff near output token 20, all 3 repeats identical). The issue has been
// open since 2026-09-09 as "MTP vs plain 有小分歧待查 (疑 NVFP4 near-tie
// 噪声)" — suspected but never measured. EVALUATION.md: a correct
// implementation does not require bit-identity across all computation
// paths, but approximations need a defined contract and evidence, and
// near-tie must not be a blanket explanation for all differences.
//
// Hypothesis under test: the MTP verify pass computes k+1 token logits in
// ONE ModelDecodeBatch(T=k+1) forward, while plain decode uses
// ModelDecodeStepSeq(T=1). The two paths round differently (different
// GEMM/attention tiling), and at near-tie positions argmax flips. The
// acceptance logic itself (longest draft prefix matching per-position
// argmax) is greedy-correct by construction, so any divergence must come
// from the verify row-0 logits differing from the single-decode logits.
//
// Method: one sequence (max_seq=1, same as serve), one prompt prefilled
// once. For each decode position i with token t_i and recurrent state S_i:
//   1. snapshot S_i to host
//   2. plain: ModelDecodeStepSeq(t_i) -> logits_plain, state S_{i+1}
//   3. restore S_i
//   4. batch: ModelDecodeBatch([t_i,f,f,f], T=4, save_checkpoints=true)
//             -> row 0 logits_batched, state S_i+[t_i,f,f,f]
//   5. ModelRestoreCheckpoint(0) -> state S_{i+1}
// Compare argmax + top-2 gap + logit l2_rel at every position.
//
// Causality guard: row 0 must be bit-identical for different filler
// tokens (row 0 cannot read positions after it). Checked at step 0 with
// f=42 vs f=43. A violation is a real bug, not FP noise.
//
// Pass criterion (v2, ULP-aware): execution completes; the causality
// guard holds; every argmax flip is a near-tie on at least one side
// (top-2 gap <= kConfidentUlps ULP at that side's top-1 magnitude). A
// flip confident on both sides (gap > kConfidentUlps ULP on both) fails.
// v1 used an absolute 0.05 threshold, which is below one BF16 ULP at the
// observed logit magnitudes (~0.125) and so misclassified 1-2 ULP rounding
// flips as "confident".
//
// Environment:
//   Q4T_VERIFY_NUMERICS_PROMPT  path to a prompt text file (default:
//                               tests/fixtures/mtp-verify-1k-prompt.txt)
//   Q4T_VERIFY_NUMERICS_STEPS   decode steps to compare (default 256)
#include "q4t/model/model.h"
#include "q4t/test.h"
#include "q4t/text/tokenizer.h"

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
#include <iterator>
#include <string>
#include <vector>

namespace {

using q4t::Status;
using q4t::model::LoadModel;
using q4t::model::Model;
using q4t::model::ModelConfig;
using q4t::model::ModelSequence;
using q4t::model::ModelBeginSequence;
using q4t::model::ModelPrefill;
using q4t::model::ModelPrefillTextChunk;
using q4t::model::LogitsRows;
using q4t::model::SequenceCompletion;
using q4t::model::ModelDecodeStepSeq;
using q4t::model::ModelDecodeBatch;
using q4t::model::ModelEndSequence;
using q4t::model::ModelReserveVerifyCheckpoints;
using q4t::model::ModelRestoreCheckpoint;
using q4t::model::ModelSnapshotState;
using q4t::model::ModelRestoreState;
using q4t::model::ModelStateSnapshot;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";
const char* kPleSidecar =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "ple/qwen3.8-flash-next-ple-fp8.bin";
const char* kDefaultPromptPath =
    "tests/fixtures/mtp-verify-1k-prompt.txt";

// A flip is "confident on both sides" when neither path's top-2 gap is
// near-tie; that cannot be explained by BF16 rounding of the same math.
// The threshold is ULP-aware: a T=1 (GEMV) vs T=4 (small-M GEMM) logit
// difference of a few ULP is the expected accumulated rounding across 48
// layers of differently-tiled GEMMs (the observed minimum non-flip top-2
// gap is exactly 1 ULP at logit magnitude ~24). A flip is only "confident"
// when the top-2 gap on BOTH sides exceeds kConfidentUlps ULP at that
// side's top-1 magnitude; 1-3 ULP is within the measured rounding.
constexpr int kConfidentUlps = 4;

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

// BF16 ULP (unit in the last place) at magnitude |v|. BF16 stores a 7-bit
// mantissa, so for |v| in [2^e, 2^(e+1)) the spacing is 2^(e-7). Used to
// express the near-tie threshold in ULP units (see kConfidentUlps).
float Bf16Ulp(float v) {
  const float a = std::fabs(v);
  if (a < 1e-30f) return 0.0f;
  int exp = 0;
  std::frexp(a, &exp);  // a in [2^(exp-1), 2^exp)
  return std::ldexp(1.0f, exp - 8);  // 2^(e-7) with e = exp-1
}

struct Top2 {
  int id = 0;
  float value = 0.0f;
  float runner_up = 0.0f;
  float gap() const { return value - runner_up; }
};

Top2 Top2Bf16(const uint16_t* lg, int vocab) {
  Top2 t;
  for (int v = 0; v < vocab; ++v) {
    const float x = Bf16ToFloat(lg[v]);
    if (x > t.value) {
      t.runner_up = t.value;
      t.value = x;
      t.id = v;
    } else if (x > t.runner_up) {
      t.runner_up = x;
    }
  }
  return t;
}

float L2RelBf16(const uint16_t* a, const uint16_t* b, int n) {
  double num = 0.0, den = 0.0;
  for (int i = 0; i < n; ++i) {
    const float x = Bf16ToFloat(a[i]);
    const float y = Bf16ToFloat(b[i]);
    const double d = static_cast<double>(x) - static_cast<double>(y);
    num += d * d;
    den += static_cast<double>(x) * static_cast<double>(x);
  }
  return den > 0.0 ? static_cast<float>(std::sqrt(num / den)) : 0.0f;
}

std::string ReadFile(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) return std::string();
  return std::string((std::istreambuf_iterator<char>(in)),
                     std::istreambuf_iterator<char>());
}

}  // namespace

Q4T_TEST(mtp_verify_numerics) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar)) {
    Q4T_SKIP("(skipped: model or PLE sidecar not found)");
  }

  // Prompt: env override or the committed 1K E2E fixture.
  const char* prompt_env = std::getenv("Q4T_VERIFY_NUMERICS_PROMPT");
  const std::string prompt_path =
      prompt_env ? prompt_env : kDefaultPromptPath;
  const std::string prompt_text = ReadFile(prompt_path);
  if (prompt_text.empty()) {
    std::printf("  prompt file unreadable: %s\n", prompt_path.c_str());
    return false;
  }
  const char* steps_env = std::getenv("Q4T_VERIFY_NUMERICS_STEPS");
  const int steps =
      std::min(std::max(steps_env ? std::atoi(steps_env) : 256, 1), 512);

  // Tokenize with the server's own tokenizer.
  q4t::text::TokenizerLimits limits;
  std::unique_ptr<q4t::text::Tokenizer> tok;
  Status s = q4t::text::Tokenizer::Load(std::string(kModelDir) +
                                            "/tokenizer.json",
                                        limits, &tok);
  if (!s.ok()) {
    std::printf("  tokenizer load failed: %s\n", s.message().c_str());
    return false;
  }
  std::vector<std::uint32_t> uids;
  s = tok->Encode(prompt_text, &uids);
  if (!s.ok()) {
    std::printf("  tokenize failed: %s\n", s.message().c_str());
    return false;
  }
  const int P = static_cast<int>(uids.size());
  std::vector<int32_t> prompt(P);
  for (int i = 0; i < P; ++i) prompt[i] = static_cast<int32_t>(uids[i]);
  std::printf("  prompt_tokens=%d steps=%d\n", P, steps);

  // Full model, single sequence (serve configuration).
  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.ple_sidecar = kPleSidecar;
  cfg.max_prefill = 8192;
  const char* maxlen_env = std::getenv("Q4T_VERIFY_NUMERICS_MAX_LEN");
  cfg.max_len = maxlen_env ? std::max(std::atoi(maxlen_env), 8192) : 8192;
  cfg.max_seq = 1;
  if (P + steps + 4 > cfg.max_len) {
    std::printf("  prompt+steps exceed max_len; not a valid test setup\n");
    return false;
  }
  Model m;
  s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  model load failed: %s\n", s.message().c_str());
    return false;
  }
  const int vocab = m.cfg.vocab;
  cudaStream_t stream = nullptr;

  // Per-component layout for state-diff attribution (mirrors
  // CollectRecurrentState: per layer ssm, conv, then ple_conv if present).
  struct CompInfo {
    int layer;
    const char* kind;
    size_t off;
    size_t bytes;
  };
  std::vector<CompInfo> comps;
  {
    size_t off = 0;
    for (int li = 0; li < m.cfg.num_layers; ++li) {
      const auto& l = m.layers[li];
      if (l.ssm_state) {
        const size_t b = static_cast<size_t>(l.linear.nv) * l.linear.kd *
                         l.linear.vd * 4;
        comps.push_back({li, "ssm", off, b});
        off += b;
        const size_t c = static_cast<size_t>(l.linear.in_qkv()) *
                         (l.linear.conv_k - 1) * 2;
        comps.push_back({li, "conv", off, c});
        off += c;
      }
      if (l.ple_conv_state) {
        const size_t pc = static_cast<size_t>(l.hc_dim) *
                          (l.ple.conv_kernel - 1) * l.ple.conv_dilation * 2;
        comps.push_back({li, "ple_conv", off, pc});
        off += pc;
      }
    }
  }
  struct CompStat {
    size_t steps_diff = 0;      // steps (of `steps`) with any diff in this comp
    size_t max_diff_bytes = 0;  // max differing bytes within a single step
    size_t total_diff_bytes = 0;
    size_t diff_both_nonzero = 0;
    size_t diff_plain_zero = 0;
    size_t diff_batch_zero = 0;
    size_t first_off = SIZE_MAX;  // first differing byte offset (SIZE_MAX=none)
  };
  std::vector<CompStat> comp_stats(comps.size());

  uint16_t* d_plain = nullptr;  // [vocab]
  uint16_t* d_batch = nullptr;  // [4, vocab]
  if (cudaMalloc(reinterpret_cast<void**>(&d_plain),
                 static_cast<size_t>(vocab) * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_batch),
                 static_cast<size_t>(4) * vocab * 2) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    m.Free();
    return false;
  }
  std::vector<uint16_t> h_plain(vocab), h_batch(vocab);

  ModelSequence seq;
  s = ModelBeginSequence(m, &seq, stream);
  if (!s.ok()) {
    std::printf("  begin sequence failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  uint16_t* d_prefill_logits = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_prefill_logits),
                 static_cast<size_t>(vocab) * 2) != cudaSuccess) {
    std::printf("  cudaMalloc prefill logits failed\n");
    m.Free();
    return false;
  }
  // Chunked prefill (mirrors the scheduler): ModelPrefill only accepts a
  // single chunk (T <= max_prefill); long prompts must be prefilled in
  // max_prefill-sized chunks via ModelPrefillTextChunk.
  {
    int base = 0;
    while (base < P) {
      const int count = std::min(m.cfg.max_prefill, P - base);
      const bool last = (base + count == P);
      s = ModelPrefillTextChunk(
          m, &seq, prompt.data(), P, count,
          last ? d_prefill_logits : nullptr, stream, nullptr,
          LogitsRows::kLastRow, SequenceCompletion::kWait);
      if (!s.ok()) {
        std::printf("  prefill chunk failed at base=%d: %s\n", base,
                    s.message().c_str());
        m.Free();
        return false;
      }
      base += count;
    }
  }
  std::vector<uint16_t> h_last(vocab);
  cudaMemcpy(h_last.data(), d_prefill_logits, vocab * 2,
             cudaMemcpyDeviceToHost);
  const int32_t t0 = Top2Bf16(h_last.data(), vocab).id;

  // Reserve count is normally T-1 (== verify_ckpt_cap in production, where
  // cap == mtp_k == tokens_per_seq-1). The env override exists to exercise
  // cap != T-1: save, restore and snapshot must all use the reserved cap
  // stride even though only T-1 rows are valid.
  const char* reserve_env = std::getenv("Q4T_VERIFY_NUMERICS_RESERVE");
  const int reserve = reserve_env ? std::atoi(reserve_env) : 4;
  s = ModelReserveVerifyCheckpoints(m, reserve);
  if (!s.ok()) {
    std::printf("  reserve checkpoints failed (cap=%d): %s\n", reserve,
                s.message().c_str());
    m.Free();
    return false;
  }
  std::printf("  reserve_cap=%d (T-1=3)\n", reserve);
  std::printf("  snapshot_bytes=%zu\n",
              q4t::model::ModelStateSnapshotBytes(m));
  ModelStateSnapshot snap;
  // State-update T-independence check (E8 analog): the recurrent state after
  // the T=1 decode must equal the checkpoint-0 state after the T=4 decode
  // (state after the first token). A difference means the per-token state
  // update (SSM/conv/PLE conv) is itself T-dependent.
  ModelStateSnapshot snap_p, snap_b, snap_ck;
  int state_diff_steps = 0;
  // Direct-read checkpoint-0 diagnostics (step 0 only): does the checkpoint
  // buffer itself hold the T=1 state, or is the divergence in the save?
  int ckpt_direct_diff_steps = 0;
  size_t ckpt_direct_max_diff_bytes = 0;
  long state_first_diff_step = -1;
  size_t state_max_diff_bytes = 0;
  bool dump_done = false;

  const int32_t kFillerA = 42;
  const int32_t kFillerB = 43;

  // Causality guard at step 0: row 0 must be bit-identical for different
  // filler tokens (row 0 cannot read positions after it).
  {
    const int base = seq.position;
    const int hist_len = static_cast<int>(seq.history.size());
    const std::vector<int32_t> hist_copy = seq.history;
    ModelStateSnapshot s0;
    s = ModelSnapshotState(m, &s0, stream);
    if (!s.ok()) {
      std::printf("  snapshot failed: %s\n", s.message().c_str());
      m.Free();
      return false;
    }
    int32_t ids_a[4] = {t0, kFillerA, kFillerA, kFillerA};
    s = ModelDecodeBatch(m, ids_a, 4, base, hist_copy.data(), hist_len,
                         d_batch, stream, nullptr, true, 0);
    if (s.ok() && !m.verify_ssm_record_fold)
      s = Status::Fail("T=4 causality A missed SSM record/fold");
    if (!s.ok()) {
      std::printf("  batch(f=42) failed: %s\n", s.message().c_str());
      m.Free();
      return false;
    }
    cudaMemcpy(h_plain.data(), d_batch, vocab * 2, cudaMemcpyDeviceToHost);
    const std::vector<uint16_t> row_a = h_plain;
    s = ModelRestoreState(m, s0, stream);
    if (!s.ok()) {
      std::printf("  restore failed: %s\n", s.message().c_str());
      m.Free();
      return false;
    }
    int32_t ids_b[4] = {t0, kFillerB, kFillerB, kFillerB};
    s = ModelDecodeBatch(m, ids_b, 4, base, hist_copy.data(), hist_len,
                         d_batch, stream, nullptr, true, 0);
    if (s.ok() && !m.verify_ssm_record_fold)
      s = Status::Fail("T=4 causality B missed SSM record/fold");
    if (!s.ok()) {
      std::printf("  batch(f=43) failed: %s\n", s.message().c_str());
      m.Free();
      return false;
    }
    cudaMemcpy(h_plain.data(), d_batch, vocab * 2, cudaMemcpyDeviceToHost);
    s = ModelRestoreState(m, s0, stream);
    if (!s.ok()) {
      std::printf("  restore failed: %s\n", s.message().c_str());
      m.Free();
      return false;
    }
    const bool causal_ok =
        std::equal(row_a.begin(), row_a.end(), h_plain.begin());
    std::printf("  causality: row0 bit-identical across fillers=%s\n",
                causal_ok ? "yes" : "NO");
    if (!causal_ok) {
      std::printf("  FAIL: verify row 0 depends on later batch tokens\n");
      m.Free();
      return false;
    }
  }

  // Main comparison loop.
  int32_t t = t0;
  int flips = 0;
  int confident_flips = 0;
  float min_nonflip_gap = 1e30f;
  float max_flip_l2 = 0.0f;
  for (int i = 0; i < steps; ++i) {
    const int base = seq.position;
    const int hist_len = static_cast<int>(seq.history.size());
    const std::vector<int32_t> hist_copy = seq.history;

    // 1. snapshot S_i
    s = ModelSnapshotState(m, &snap, stream);
    if (!s.ok()) {
      std::printf("  step %d snapshot failed: %s\n", i, s.message().c_str());
      m.Free();
      return false;
    }
    // 2. plain T=1 (step 0: dump per-layer intermediates for state-divergence
    //    localization)
    if (i == 0) {
      setenv("Q4T_LIN_DUMP", "q4t_state_plain", 1);
      setenv("Q4T_MLP_DUMP", "q4t_state_plain", 1);
    }
    s = ModelDecodeStepSeq(m, &seq, t, d_plain, stream);
    if (i == 0) {
      unsetenv("Q4T_LIN_DUMP");
      unsetenv("Q4T_MLP_DUMP");
    }
    if (!s.ok()) {
      std::printf("  step %d plain failed: %s\n", i, s.message().c_str());
      m.Free();
      return false;
    }
    // 2b. snapshot S_{i+1}^plain (state after the T=1 decode)
    s = ModelSnapshotState(m, &snap_p, stream);
    if (!s.ok()) {
      std::printf("  step %d snapshot_p failed: %s\n", i, s.message().c_str());
      m.Free();
      return false;
    }
    // 3. restore S_i
    s = ModelRestoreState(m, snap, stream);
    if (!s.ok()) {
      std::printf("  step %d restore failed: %s\n", i, s.message().c_str());
      m.Free();
      return false;
    }
    // 4. batch T=4 (verify shape), row 0 (step 0: dump per-layer
    //    intermediates for state-divergence localization)
    int32_t ids[4] = {t, kFillerA, kFillerA, kFillerA};
    if (i == 0) {
      setenv("Q4T_LIN_DUMP", "q4t_state_batch", 1);
      setenv("Q4T_MLP_DUMP", "q4t_state_batch", 1);
    }
    s = ModelDecodeBatch(m, ids, 4, base, hist_copy.data(), hist_len,
                         d_batch, stream, nullptr, true, 0);
    if (s.ok() && !m.verify_ssm_record_fold)
      s = Status::Fail("T=4 numeric contract missed SSM record/fold");
    if (i == 0) {
      unsetenv("Q4T_LIN_DUMP");
      unsetenv("Q4T_MLP_DUMP");
    }
    if (!s.ok()) {
      std::printf("  step %d batch failed: %s\n", i, s.message().c_str());
      m.Free();
      return false;
    }
    // 4b. DIRECT-READ checkpoint 0 (before restore) and compare with the T=1
    //     state snap_p. If this matches snap_p, the checkpoint SAVE is correct
    //     and any divergence is downstream (restore); if it differs, the
    //     checkpoint save (or the serial-kernel state update) is the source.
    s = ModelSnapshotCheckpoint(m, 0, &snap_ck, stream, 0);
    if (!s.ok()) {
      std::printf("  step %d snapshot_ckpt failed: %s\n", i,
                  s.message().c_str());
      m.Free();
      return false;
    }
    if (snap_ck.valid && snap_p.data != snap_ck.data) {
      ++ckpt_direct_diff_steps;
      size_t nd = 0;
      size_t first = snap_ck.data.size();
      for (size_t k = 0; k < snap_ck.data.size(); ++k)
        if (snap_p.data[k] != snap_ck.data[k]) {
          ++nd;
          if (first == snap_ck.data.size()) first = k;
        }
      ckpt_direct_max_diff_bytes =
          std::max(ckpt_direct_max_diff_bytes, nd);
      if (i == 0) {
        std::printf("  ckpt_direct: step0 first_diff_offset=%zu "
                    "diff_bytes=%zu total=%zu\n",
                    first, nd, snap_ck.data.size());
      }
    }
    // 5. restore to S_{i+1} (checkpoint after the first batch token)
    s = ModelRestoreCheckpoint(m, 0, stream, 0);
    if (!s.ok()) {
      std::printf("  step %d checkpoint restore failed: %s\n", i,
                  s.message().c_str());
      m.Free();
      return false;
    }
    // 5b. snapshot S_{i+1}^batch (checkpoint-0 state) and compare with the
    //     T=1 state. Both decode the same token t from the same S_i.
    s = ModelSnapshotState(m, &snap_b, stream);
    if (!s.ok()) {
      std::printf("  step %d snapshot_b failed: %s\n", i, s.message().c_str());
      m.Free();
      return false;
    }
    if (snap_p.data != snap_b.data) {
      ++state_diff_steps;
      if (state_first_diff_step < 0) state_first_diff_step = i;
      size_t nd = 0;
      std::vector<size_t> per_step(comps.size(), 0);
      size_t ci = 0;
      for (size_t k = 0; k < snap_p.data.size(); ++k) {
        while (ci + 1 < comps.size() && k >= comps[ci + 1].off) ++ci;
        if (snap_p.data[k] != snap_b.data[k]) {
          ++nd;
          ++per_step[ci];
          auto& st = comp_stats[ci];
          if (st.first_off == SIZE_MAX) st.first_off = k - comps[ci].off;
          ++st.total_diff_bytes;
          if (snap_p.data[k] == 0) ++st.diff_plain_zero;
          if (snap_b.data[k] == 0) ++st.diff_batch_zero;
          if (snap_p.data[k] != 0 && snap_b.data[k] != 0)
            ++st.diff_both_nonzero;
        }
      }
      state_max_diff_bytes = std::max(state_max_diff_bytes, nd);
      for (size_t c = 0; c < comps.size(); ++c) {
        comp_stats[c].max_diff_bytes =
            std::max(comp_stats[c].max_diff_bytes, per_step[c]);
        if (per_step[c] > 0) ++comp_stats[c].steps_diff;
      }
      // Numerical component sample at the LAST step: interpret the first few
      // differing words per component type (ssm=FP32 4B, conv/ple_conv=BF16
      // 2B) and report plain/batch values, abs diff, relative diff, and
      // finiteness. Bounded (3 instances per type) to keep the log small.
      if (i == steps - 1) {
        int shown_ssm = 0, shown_conv = 0, shown_ple = 0;
        for (size_t c = 0; c < comps.size(); ++c) {
          const auto& comp = comps[c];
          const char k0 = comp.kind[0];  // 's'=ssm, 'c'=conv, 'p'=ple_conv
          const bool is_f32 = (k0 == 's');
          const size_t ws = is_f32 ? 4 : 2;
          if (comp.bytes < ws) continue;
          int& shown =
              (k0 == 's') ? shown_ssm : (k0 == 'p') ? shown_ple : shown_conv;
          if (shown >= 3) continue;
          for (size_t w = 0; w + ws <= comp.bytes && shown < 3; w += ws) {
            const size_t kk = comp.off + w;
            bool diff = false;
            for (size_t b = 0; b < ws; ++b)
              if (snap_p.data[kk + b] != snap_b.data[kk + b]) {
                diff = true;
                break;
              }
            if (!diff) continue;
            float pv = 0.0f, bv = 0.0f;
            if (is_f32) {
              std::memcpy(&pv, &snap_p.data[kk], 4);
              std::memcpy(&bv, &snap_b.data[kk], 4);
            } else {
              uint16_t u1, u2;
              std::memcpy(&u1, &snap_p.data[kk], 2);
              std::memcpy(&u2, &snap_b.data[kk], 2);
              pv = Bf16ToFloat(u1);
              bv = Bf16ToFloat(u2);
            }
            const float ad = std::fabs(pv - bv);
            const float denom = std::max(std::fabs(pv), std::fabs(bv));
            const float rel = ad / (denom > 1e-30f ? denom : 1e-30f);
            std::printf(
                "    num_sample layer=%d %-8s w=%zu plain=%g batch=%g "
                "absdiff=%g reldiff=%g finite=%d\n",
                comp.layer, comp.kind, w, pv, bv, ad, rel,
                (std::isfinite(pv) && std::isfinite(bv)) ? 1 : 0);
            ++shown;
          }
        }
      }
      // First-difference diagnostics (step 0 only): locate the diverging
      // buffer by offset and show the difference magnitude.
      if (i == 0 && !dump_done) {
        dump_done = true;
        const auto& a = snap_p.data;
        const auto& b = snap_b.data;
        size_t first = a.size();
        for (size_t k = 0; k < a.size(); ++k)
          if (a[k] != b[k]) { first = k; break; }
        std::printf("  state_diag: first_diff_offset=%zu total=%zu", first,
                    a.size());
        // Per-1MB window diff counts.
        const size_t win = 1024 * 1024;
        for (size_t w0 = 0; w0 < a.size(); w0 += win) {
          size_t w1 = std::min(w0 + win, a.size());
          size_t wd = 0;
          for (size_t k = w0; k < w1; ++k)
            if (a[k] != b[k]) ++wd;
          std::printf(" win[%zu..%zu]=%zu", w0, w1, wd);
        }
        std::printf("\n");
        // Sample the first 8 differing 32-bit words.
        int shown = 0;
        for (size_t k = 0; k + 4 <= a.size() && shown < 8; k += 4) {
          uint32_t wa, wb;
          std::memcpy(&wa, a.data() + k, 4);
          std::memcpy(&wb, b.data() + k, 4);
          if (wa != wb) {
            std::printf("  state_diag: off=%zu plain=0x%08x batch=0x%08x\n",
                        k, wa, wb);
            ++shown;
          }
        }
      }
    }

    cudaMemcpy(h_plain.data(), d_plain, vocab * 2, cudaMemcpyDeviceToHost);
    cudaMemcpy(h_batch.data(), d_batch, vocab * 2, cudaMemcpyDeviceToHost);
    const Top2 tp = Top2Bf16(h_plain.data(), vocab);
    const Top2 tb = Top2Bf16(h_batch.data(), vocab);
    const float l2 = L2RelBf16(h_plain.data(), h_batch.data(), vocab);
    if (tp.id != tb.id) {
      ++flips;
      const float ulp_p = Bf16Ulp(tp.value);
      const float ulp_b = Bf16Ulp(tb.value);
      const bool near_tie =
          tp.gap() <= kConfidentUlps * ulp_p ||
          tb.gap() <= kConfidentUlps * ulp_b;
      if (!near_tie) ++confident_flips;
      max_flip_l2 = std::max(max_flip_l2, l2);
      std::printf("  step=%d FLIP plain=%d batch=%d gap_plain=%.6f "
                  "(%.1f ulp) gap_batch=%.6f (%.1f ulp) l2_rel=%.6f "
                  "near_tie=%d\n",
                  i, tp.id, tb.id, tp.gap(),
                  ulp_p > 0 ? tp.gap() / ulp_p : 0.0f, tb.gap(),
                  ulp_b > 0 ? tb.gap() / ulp_b : 0.0f, l2,
                  near_tie ? 1 : 0);
      // Top-logit capture from the existing h_plain/h_batch (no re-run, so
      // seq.position/history/KV are untouched): the actual values at each
      // side's argmax and at the other side's argmax, to distinguish a
      // near-tie from a genuine error.
      std::printf(
          "    plain: top1=%d(v=%.6f) top2v=%.6f gap=%.6f "
          "at_batch_id(%d)=%.6f\n",
          tp.id, tp.value, tp.runner_up, tp.gap(), tb.id,
          Bf16ToFloat(h_plain[tb.id]));
      std::printf(
          "    batch: top1=%d(v=%.6f) top2v=%.6f gap=%.6f "
          "at_plain_id(%d)=%.6f\n",
          tb.id, tb.value, tb.runner_up, tb.gap(), tp.id,
          Bf16ToFloat(h_batch[tp.id]));
      // Per-layer dump at the FIRST flip step (E9 analog): re-run both
      // forwards from S_i with Q4T_MLP_DUMP active so the layer-wise trunk /
      // MoE-boundary dumps let us localize the first diverging component.
      // The re-run is deterministic (same S_i, same KV up to base) and ends
      // by restoring the same checkpoint, so the trajectory is unchanged.
      if (!dump_done) {
        dump_done = true;
        s = ModelRestoreState(m, snap, stream);
        if (!s.ok()) {
          std::printf("  step %d dump restore failed: %s\n", i,
                      s.message().c_str());
          m.Free();
          return false;
        }
        setenv("Q4T_MLP_DUMP", "q4t_mtp_plain", 1);
        s = ModelDecodeStepSeq(m, &seq, t, d_plain, stream);
        unsetenv("Q4T_MLP_DUMP");
        if (!s.ok()) {
          std::printf("  step %d dump plain failed: %s\n", i,
                      s.message().c_str());
          m.Free();
          return false;
        }
        s = ModelRestoreState(m, snap, stream);
        if (!s.ok()) {
          std::printf("  step %d dump restore2 failed: %s\n", i,
                      s.message().c_str());
          m.Free();
          return false;
        }
        setenv("Q4T_MLP_DUMP", "q4t_mtp_batch", 1);
        s = ModelDecodeBatch(m, ids, 4, base, hist_copy.data(), hist_len,
                             d_batch, stream, nullptr, true, 0);
        if (s.ok() && !m.verify_ssm_record_fold)
          s = Status::Fail("T=4 diagnostic rerun missed SSM record/fold");
        unsetenv("Q4T_MLP_DUMP");
        if (!s.ok()) {
          std::printf("  step %d dump batch failed: %s\n", i,
                      s.message().c_str());
          m.Free();
          return false;
        }
        s = ModelRestoreCheckpoint(m, 0, stream, 0);
        if (!s.ok()) {
          std::printf("  step %d dump checkpoint restore failed: %s\n", i,
                      s.message().c_str());
          m.Free();
          return false;
        }
        std::printf("  dump: step %d re-run with tags q4t_mtp_plain / "
                    "q4t_mtp_batch\n", i);
      }
    } else {
      min_nonflip_gap =
          std::min(min_nonflip_gap, std::min(tp.gap(), tb.gap()));
    }
    t = tp.id;  // both paths advance with the plain token
  }

  // State validity per the read/write contract: the snapshot buffer size
  // must equal the expected total (sum of per-layer ssm+conv+ple_conv bytes).
  {
    size_t expected = 0;
    for (const auto& c : comps) expected += c.bytes;
    std::printf("  state_size_check expected=%zu actual=%zu match=%d\n",
                expected, snap_p.data.size(),
                expected == snap_p.data.size() ? 1 : 0);
  }
  std::printf("  state_diff_by_component (both_nz/plain0/batch0 = share of "
              "differing bytes with both non-zero / plain zero / batch zero):"
              "\n");
  for (size_t c = 0; c < comps.size(); ++c) {
    const auto& st = comp_stats[c];
    if (st.steps_diff == 0) continue;
    const double tot = static_cast<double>(st.total_diff_bytes);
    std::printf("    layer=%d %-8s steps=%zu max_bytes=%zu total=%zu "
                "both_nz=%.1f%% plain0=%.1f%% batch0=%.1f%% first_off=%zu\n",
                comps[c].layer, comps[c].kind, st.steps_diff,
                st.max_diff_bytes, st.total_diff_bytes,
                tot > 0 ? 100.0 * st.diff_both_nonzero / tot : 0.0,
                tot > 0 ? 100.0 * st.diff_plain_zero / tot : 0.0,
                tot > 0 ? 100.0 * st.diff_batch_zero / tot : 0.0,
                st.first_off == SIZE_MAX ? 0 : st.first_off);
  }
  std::printf("  flips=%d/%d confident_both_sides=%d max_flip_l2=%.6f "
              "min_nonflip_gap=%.6f\n",
              flips, steps, confident_flips, max_flip_l2, min_nonflip_gap);
  std::printf("  state_update_T_independent: %d/%d steps differ, "
              "first=%ld max_diff_bytes=%zu\n",
              state_diff_steps, steps, state_first_diff_step,
              state_max_diff_bytes);
  std::printf("  ckpt0_direct_read: %d/%d steps differ from T=1, "
              "max_diff_bytes=%zu\n",
              ckpt_direct_diff_steps, steps, ckpt_direct_max_diff_bytes);

  cudaFree(d_plain);
  cudaFree(d_batch);
  cudaFree(d_prefill_logits);
  ModelEndSequence(&seq);
  m.Free();
  if (confident_flips > 0) {
    std::printf("  FAIL: %d flip(s) confident on both sides (top-2 gap "
                "> %d ULP in both paths) — not explainable by BF16 "
                "rounding\n",
                confident_flips, kConfidentUlps);
    return false;
  }
  return true;
}
