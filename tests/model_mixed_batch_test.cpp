// Test for ModelMixedBatch (Phase 2 fused prefill+decode continuous batching).
// Loads a small 2-layer model (linear + linear/PLE) and verifies:
//
//   A. ALL-FRESH == ModelPrefillBatch: a mixed batch with every base_position 0
//      and EOS prior_history reproduces ModelPrefillBatch bit-identically (it is
//      the same code path). Guards the generalization from regressing the
//      prefill-only case (PD-disaggregation still uses ModelPrefillBatch).
//   B. FUSED fresh + continue: one sequence prefills fresh (base 0) while
//      another continues from its existing state (base > 0, one decode token),
//      in ONE forward. The fresh sequence's per-token logits match a standalone
//      ModelPrefill; the continuing sequence's row matches the last row of a
//      full ModelPrefill(prompt + token) — i.e. the fused forward computes the
//      decode token's logits as if it were part of a full prefill.
#include "q4t/model/model.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::model::LoadModel;
using q4t::model::Model;
using q4t::model::ModelBeginSequence;
using q4t::model::ModelConfig;
using q4t::model::ModelEndSequence;
using q4t::model::ModelMixedBatch;
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

Q4T_TEST(model_mixed_batch) {
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

  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = 2;  // layer 0 linear, layer 1 linear + PLE
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
  const int w = m.ple_hash.ngram_size - 1;  // PLE n-gram history width
  const int32_t eos = static_cast<int32_t>(m.cfg.eos_token_id);

  const std::vector<int32_t> P0 = {846, 25, 1203, 321, 44};  // fresh, len 5
  const std::vector<int32_t> P1 = {1024, 55, 9001, 700};     // continue, len 4
  const int lenA = static_cast<int>(P0.size());
  const int len1 = static_cast<int>(P1.size());
  const int32_t Btok = 42;  // the decode token for the continuing sequence

  uint16_t* d_logits = nullptr;  // [<=Ttot, vocab]
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits),
                 static_cast<size_t>(16) * vocab * 2) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    m.Free();
    return false;
  }
  auto fail = [&](const char* msg) {
    std::printf("  %s\n", msg);
    cudaFree(d_logits);
    m.Free();
    return false;
  };

  // ======================= Test A: all-fresh == ModelPrefillBatch ==========
  {
    const int B = 2;
    std::vector<int32_t> toks(P0.begin(), P0.end());
    toks.insert(toks.end(), P1.begin(), P1.end());
    std::vector<int> lens = {lenA, len1};
    std::vector<int> seq_ids = {0, 1};
    std::vector<int> bases = {0, 0};  // all fresh
    std::vector<int32_t> prior(static_cast<size_t>(B) * w, eos);  // EOS
    const int Ttot = lenA + len1;

    s = ModelPrefillBatch(m, toks.data(), lens.data(), seq_ids.data(), B,
                          d_logits, nullptr);
    if (!s.ok()) return fail("A: ModelPrefillBatch failed");
    std::vector<uint16_t> ref_pb(static_cast<size_t>(Ttot) * vocab);
    cudaMemcpy(ref_pb.data(), d_logits, ref_pb.size() * 2,
               cudaMemcpyDeviceToHost);

    s = ModelMixedBatch(m, toks.data(), lens.data(), seq_ids.data(),
                        bases.data(), prior.data(), B, d_logits, nullptr);
    if (!s.ok()) return fail("A: ModelMixedBatch failed");
    std::vector<uint16_t> out_mb(static_cast<size_t>(Ttot) * vocab);
    cudaMemcpy(out_mb.data(), d_logits, out_mb.size() * 2,
               cudaMemcpyDeviceToHost);

    const float rel = L2Rel(out_mb.data(), ref_pb.data(), Ttot * vocab);
    std::printf("  A all-fresh vs ModelPrefillBatch: l2_rel=%.2e\n", rel);
    if (rel > 1e-4f) return fail("A: mixed all-fresh != ModelPrefillBatch");
  }

  // ======================= Test B: fused fresh + continue ==================
  // Reference for the continuing row: a full prefill of (P1 + Btok); its last
  // row is Btok's logits at position len1 in a full-prefill context.
  std::vector<uint16_t> ref_cont(vocab);
  {
    std::vector<int32_t> full(P1.begin(), P1.end());
    full.push_back(Btok);
    ModelSequence rs;
    s = ModelBeginSequence(m, &rs, nullptr, /*seq_id=*/1);
    if (s.ok())
      s = ModelPrefill(m, &rs, full.data(), static_cast<int>(full.size()),
                       d_logits, nullptr, nullptr, nullptr, 1);
    if (!s.ok()) return fail("B: full-prefill ref failed");
    cudaMemcpy(ref_cont.data(),
               d_logits + static_cast<size_t>(full.size() - 1) * vocab,
               static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost);
    ModelEndSequence(&rs);
  }

  // Reference for the fresh sequence: a standalone prefill of P0 (seq 0).
  std::vector<uint16_t> ref_fresh(static_cast<size_t>(lenA) * vocab);
  {
    ModelSequence rs;
    s = ModelBeginSequence(m, &rs, nullptr, /*seq_id=*/0);
    if (s.ok())
      s = ModelPrefill(m, &rs, P0.data(), lenA, d_logits, nullptr, nullptr,
                       nullptr, 0);
    if (!s.ok()) return fail("B: fresh-prefill ref failed");
    cudaMemcpy(ref_fresh.data(), d_logits, ref_fresh.size() * 2,
               cudaMemcpyDeviceToHost);
    ModelEndSequence(&rs);
  }

  // Set up the continuing sequence's state at position len1 (prefill P1, seq 1).
  {
    ModelSequence rs;
    s = ModelBeginSequence(m, &rs, nullptr, 1);
    if (s.ok())
      s = ModelPrefill(m, &rs, P1.data(), len1, d_logits, nullptr, nullptr,
                       nullptr, 1);
    if (!s.ok()) return fail("B: continue-seq setup prefill failed");
    ModelEndSequence(&rs);
  }

  // Fused batch: seq 0 fresh (P0, base 0), seq 1 continue (Btok at base len1).
  {
    const int B = 2;
    std::vector<int32_t> toks(P0.begin(), P0.end());
    toks.push_back(Btok);
    std::vector<int> lens = {lenA, 1};
    std::vector<int> seq_ids = {0, 1};
    std::vector<int> bases = {0, len1};
    // prior_history: seq 0 fresh -> EOS; seq 1 -> P1's last w tokens (oldest
    // first, EOS-filled if len1 < w).
    std::vector<int32_t> prior(static_cast<size_t>(B) * w, eos);
    for (int j = 0; j < w; ++j) {
      const int src = len1 - w + j;
      prior[static_cast<size_t>(1) * w + j] = (src >= 0) ? P1[src] : eos;
    }
    const int Ttot = lenA + 1;
    s = ModelMixedBatch(m, toks.data(), lens.data(), seq_ids.data(),
                        bases.data(), prior.data(), B, d_logits, nullptr);
    if (!s.ok()) return fail("B: ModelMixedBatch failed");
    std::vector<uint16_t> out(static_cast<size_t>(Ttot) * vocab);
    cudaMemcpy(out.data(), d_logits, out.size() * 2, cudaMemcpyDeviceToHost);

    const float rel_fresh = L2Rel(out.data(), ref_fresh.data(), lenA * vocab);
    const float rel_cont =
        L2Rel(out.data() + static_cast<size_t>(lenA) * vocab, ref_cont.data(),
              vocab);
    const int am_cont = Argmax(out.data() + static_cast<size_t>(lenA) * vocab,
                               vocab);
    const int am_ref = Argmax(ref_cont.data(), vocab);
    std::printf(
        "  B fused: fresh l2_rel=%.2e | continue l2_rel=%.2e | argmax %d vs %d\n",
        rel_fresh, rel_cont, am_cont, am_ref);
    if (rel_fresh > 1e-2f) return fail("B: fresh rows diverge");
    if (rel_cont > 3e-2f) return fail("B: continue row diverges");
    if (am_cont != am_ref) return fail("B: continue argmax mismatch");
  }

  cudaFree(d_logits);
  m.Free();
  return true;
}
