// Ragged prefill must match independent causal prefixes at the same total
// GEMM shape, bit-exactly. Each sequence is placed first in a separate plain
// prefill and padded to total M with two predetermined suffixes. Suffixes
// cannot affect a causal prefix; the references never use ragged metadata.
// Different-length unpadded references remain visible as cross-shape
// diagnostics, not an assumption of batch-invariant projection arithmetic.
// See docs/evidence/mtp-numerical-closure-20261016 for the original failures
// and the first observed shape-dependent mixing-projection differences.
#include "q4t/model/model.h"
#include "q4t/test.h"

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
using q4t::model::ModelBeginSequence;
using q4t::model::ModelConfig;
using q4t::model::ModelEndSequence;
using q4t::model::ModelPrefill;
using q4t::model::ModelPrefillBatch;
using q4t::model::ModelSequence;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/"
    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/"
    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";
const char* kPleSidecar =
    "/home/rm01/models/dev/llm/garnermccloud/"
    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
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
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar)) {
    Q4T_SKIP("(skipped: model not found)");
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
      {846, 25, 1203, 321, 44},            // len 5
      {1024, 55, 9001},                    // len 3
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

  auto capture = [](const std::string& suffix) {
    const char* directory = std::getenv("Q4T_PREFILL_CAPTURE");
    if (!directory) return;
    for (const char* name : {"Q4T_MLP_DUMP", "Q4T_LIN_DUMP", "Q4T_MOE_DUMP"}) {
      if (suffix.empty()) {
        unsetenv(name);
      } else {
        const std::string tag = std::string(directory) + "/" + suffix;
        setenv(name, tag.c_str(), 1);
      }
    }
  };

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
    capture("ref" + std::to_string(b));
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
               static_cast<size_t>(lens[b]) * vocab * 2,
               cudaMemcpyDeviceToHost);
    ModelEndSequence(&rseq);
  }

  // ---- Ragged batched prefill --------------------------------------------
  // Packed tokens, sequence-major (seq 0's len tokens, then seq 1's, ...).
  std::vector<int32_t> tokens;
  tokens.reserve(Ttot);
  for (int b = 0; b < B; ++b)
    tokens.insert(tokens.end(), prompts[b].begin(), prompts[b].end());

  capture("packed");
  s = ModelPrefillBatch(m, tokens.data(), lens.data(), seq_ids.data(), B,
                        d_logits, nullptr);
  if (!s.ok()) {
    std::printf("  ModelPrefillBatch failed: %s\n", s.message().c_str());
    cudaFree(d_logits);
    m.Free();
    return false;
  }
  std::vector<uint16_t> batched(static_cast<size_t>(Ttot) * vocab);
  cudaMemcpy(batched.data(), d_logits, static_cast<size_t>(Ttot) * vocab * 2,
             cudaMemcpyDeviceToHost);

  // Same total projection shape, but one independent causal sequence. The
  // target prompt is first; suffix padding cannot affect its prefix.
  bool ok = true;
  int packed_offset = 0;
  for (int b = 0; b < B; ++b) {
    for (int padding_case = 0; padding_case < 2; ++padding_case) {
      std::vector<int32_t> padded = prompts[b];
      padded.resize(Ttot, padding_case == 0 ? prompts[b].back() : 0);
      ModelSequence rseq;
      s = ModelBeginSequence(m, &rseq, nullptr, seq_ids[b]);
      capture("padded" + std::to_string(b) + "-" +
              std::to_string(padding_case));
      if (s.ok()) {
        s = ModelPrefill(m, &rseq, padded.data(), Ttot, d_logits, nullptr,
                         nullptr, nullptr, seq_ids[b]);
      }
      if (!s.ok()) {
        cudaFree(d_logits);
        m.Free();
        return false;
      }
      std::vector<uint16_t> padded_logits(static_cast<size_t>(Ttot) * vocab);
      cudaMemcpy(padded_logits.data(), d_logits, padded_logits.size() * 2,
                 cudaMemcpyDeviceToHost);
      float worst = 0.0f;
      int mismatches = 0;
      for (int t = 0; t < lens[b]; ++t) {
        const uint16_t* actual =
            batched.data() + static_cast<size_t>(packed_offset + t) * vocab;
        const uint16_t* expected =
            padded_logits.data() + static_cast<size_t>(t) * vocab;
        worst = std::max(worst, L2Rel(actual, expected, vocab));
        for (int v = 0; v < vocab; ++v) mismatches += actual[v] != expected[v];
      }
      std::printf(
          "  same-M padded prefix seq=%d padding=%d differences=%d "
          "l2=%.8f\n",
          b, padding_case, mismatches, worst);
      if (mismatches != 0) ok = false;
      ModelEndSequence(&rseq);
    }
    packed_offset += lens[b];
  }
  capture("");

  // ---- Compare per token + last-token argmax ------------------------------
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
    const int am_batch = Argmax(
        batched.data() + static_cast<size_t>(offset + last) * vocab, vocab);
    const int am_ref =
        Argmax(ref[b].data() + static_cast<size_t>(last) * vocab, vocab);
    const bool seq_ok = (worst < 0.01f) && (am_batch == am_ref);
    std::printf(
        "  seq %d (len=%d): worst l2_rel=%.5f  argmax batch=%d ref=%d "
        "%s\n",
        b, lens[b], worst, am_batch, am_ref, seq_ok ? "OK" : "MISMATCH");
    // Original cross-shape observation retained; same-M oracle gates above.
    offset += lens[b];
  }

  cudaFree(d_logits);
  m.Free();
  if (!ok) {
    std::printf("  FAIL: batched prefill diverged from single-seq reference\n");
    return false;
  }
  std::printf(
      "  PASS: ragged prefill matches independent same-M prefixes "
      "(B=%d, lens=5/3/7)\n",
      B);
  return true;
}
