// Diagnostic: compare one-shot vs chunked prefill final-position logits.
// Loads the full model, runs the same prompt through:
//   A) ModelForward (one-shot, T tokens)
//   B) ModelPrefill(chunk) + ModelDecodeBatch continuation (chunked)
// and compares the logits at the final position (T-1). If they differ, the
// chunked prefill has a correctness bug; if they match, the serve-level
// divergence is in the decode loop / state machine.
#include <cuda_runtime.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/model/model.h"
#include "q4t/io/weight_loader.h"
#include "q4t/status.h"

using q4t::Status;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;
using q4t::model::Model;
using q4t::model::ModelConfig;
using q4t::model::ModelSequence;
using q4t::model::LoadModel;
using q4t::model::ModelForward;
using q4t::model::ModelBeginSequence;
using q4t::model::ModelPrefill;
using q4t::model::ModelDecodeBatch;

int main(int argc, char** argv) {
  const int T = (argc > 1) ? std::atoi(argv[1]) : 5086;
  const int chunk = (argc > 2) ? std::atoi(argv[2]) : 2048;
  const char* model_dir =
      "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";

  std::printf("Loading model (T=%d chunk=%d)...\n", T, chunk);
  ModelConfig cfg;
  cfg.model_dir = model_dir;
  cfg.index_path = std::string(model_dir) + "/model.safetensors.index.json";
  // Size the KV/indexer/rope caches for the full T (262K-class prompts).
  cfg.max_len = T;
  // Forward workspace is sized for max_prefill tokens; cap it so a 200K
  // prompt does not allocate a 200K-token workspace. The chunked path only
  // ever forwards `chunk` tokens at a time, so max_prefill = max(chunk, ...)
  // is enough. The one-shot path needs T <= max_prefill (skipped when T is
  // large, see do_oneshot).
  cfg.max_prefill = std::max(chunk, std::min(T, 8192));
  cfg.max_seq = 1;
  cfg.ple_sidecar = std::string(model_dir) + "/ple/qwen3.8-flash-next-ple-fp8.bin";

  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::fprintf(stderr, "LoadModel failed: %s\n", s.message().c_str());
    return 1;
  }
  const int vocab = m.cfg.vocab;
  std::printf("Model loaded (vocab=%d). Running forwards...\n", vocab);

  // Deterministic prompt: repeat a sentence T times (mirrors the serve test).
  std::vector<int32_t> ids(T, 42);  // token 42 = some common token

  // A) One-shot prefill (only when T is small enough that the [T, vocab]
  // buffer fits — a 200K one-shot would need ~100 GB). Run twice to measure
  // baseline non-determinism.
  const bool do_oneshot =
      (static_cast<size_t>(T) * vocab * 2) < (20ull << 30) &&
      T <= cfg.max_prefill;
  std::vector<uint16_t> logits_a, logits_a2;
  if (do_oneshot) {
    logits_a.resize(static_cast<size_t>(T) * vocab);
    logits_a2.resize(static_cast<size_t>(T) * vocab);
    s = ModelForward(m, ids.data(), T, logits_a.data(), nullptr, nullptr, 0);
    if (!s.ok()) {
      std::fprintf(stderr, "one-shot ModelForward failed: %s\n",
                   s.message().c_str());
      return 1;
    }
    cudaDeviceSynchronize();
    s = ModelForward(m, ids.data(), T, logits_a2.data(), nullptr, nullptr, 0);
    if (!s.ok()) {
      std::fprintf(stderr, "one-shot ModelForward (2nd) failed: %s\n",
                   s.message().c_str());
      return 1;
    }
    cudaDeviceSynchronize();
  }

  // B) Chunked prefill, with per-chunk timing (to localize long-context cost).
  ModelSequence seq;
  s = ModelBeginSequence(m, &seq, nullptr, 0);
  if (!s.ok()) {
    std::fprintf(stderr, "ModelBeginSequence failed: %s\n", s.message().c_str());
    return 1;
  }
  std::vector<uint16_t> logits_b(static_cast<size_t>(chunk) * vocab);
  cudaEvent_t ev0, ev1;
  cudaEventCreate(&ev0);
  cudaEventCreate(&ev1);
  double t_total = 0.0;
  cudaEventRecord(ev0);
  s = ModelPrefill(m, &seq, ids.data(), chunk, logits_b.data(), nullptr, nullptr,
                   nullptr, 0);
  cudaEventRecord(ev1);
  cudaEventSynchronize(ev1);
  float ms = 0.f;
  cudaEventElapsedTime(&ms, ev0, ev1);
  t_total += ms;
  std::printf("  chunk 0 [0..%d): %8.2f ms\n", chunk, ms);
  if (!s.ok()) {
    std::fprintf(stderr, "chunk-0 ModelPrefill failed: %s\n", s.message().c_str());
    return 1;
  }
  for (int base = chunk; s.ok() && base < T; base += chunk) {
    const int c = std::min(chunk, T - base);
    const bool last = (base + c >= T);
    cudaEventRecord(ev0);
    s = ModelDecodeBatch(m, ids.data() + base, c, base, ids.data(), base,
                         last ? logits_b.data() : nullptr, nullptr, nullptr,
                         false, 0);
    cudaEventRecord(ev1);
    cudaEventSynchronize(ev1);
    cudaEventElapsedTime(&ms, ev0, ev1);
    t_total += ms;
    if (base < 8 * chunk || base > T - 3 * chunk) {
      std::printf("  chunk [%d..%d): %8.2f ms\n", base, base + c, ms);
    }
  }
  if (!s.ok()) {
    std::fprintf(stderr, "chunked continuation failed: %s\n", s.message().c_str());
    return 1;
  }
  std::printf("  chunked prefill total: %.1f ms for T=%d (chunk=%d)\n",
              t_total, T, chunk);
  cudaDeviceSynchronize();

  // Compare final-position logits (only when the one-shot path ran).
  if (do_oneshot) {
    const int last_c = ((T - 1) % chunk) + 1;  // token count of the last chunk
    const uint16_t* a_final =
        logits_a.data() + static_cast<size_t>(T - 1) * vocab;
    const uint16_t* b_final =
        logits_b.data() + static_cast<size_t>(last_c - 1) * vocab;

    // Baseline: two one-shot ModelForward runs (same shape) — measures pure
    // GEMM non-determinism floor.
    const uint16_t* a2_final =
        logits_a2.data() + static_cast<size_t>(T - 1) * vocab;
    {
      double n2 = 0.0, d2 = 0.0;
      for (int v = 0; v < vocab; ++v) {
        const uint32_t ba = static_cast<uint32_t>(a_final[v]) << 16;
        const uint32_t bb = static_cast<uint32_t>(a2_final[v]) << 16;
        float fa, fb;
        std::memcpy(&fa, &ba, sizeof(float));
        std::memcpy(&fb, &bb, sizeof(float));
        const double dd = double(fa) - double(fb);
        n2 += dd * dd;
        d2 += double(fa) * double(fa);
      }
      std::printf("BASELINE one-shot vs one-shot: l2_rel=%.8f\n",
                  std::sqrt(n2 / (d2 + 1e-30)));
    }

    double num = 0.0, den = 0.0;
    int argmax_a = 0, argmax_b = 0;
    float best_a = -1e30f, best_b = -1e30f;
    for (int v = 0; v < vocab; ++v) {
      const uint32_t ba = static_cast<uint32_t>(a_final[v]) << 16;
      const uint32_t bb = static_cast<uint32_t>(b_final[v]) << 16;
      float fa, fb;
      std::memcpy(&fa, &ba, sizeof(float));
      std::memcpy(&fb, &bb, sizeof(float));
      const double d = double(fa) - double(fb);
      num += d * d;
      den += double(fa) * double(fa);
      if (fa > best_a) { best_a = fa; argmax_a = v; }
      if (fb > best_b) { best_b = fb; argmax_b = v; }
    }
    const float l2_rel = std::sqrt(num / (den + 1e-30));
    std::printf("final-position logits: l2_rel=%.8f argmax_a=%d argmax_b=%d "
                "%s\n", l2_rel, argmax_a, argmax_b,
                (l2_rel < 1e-6f ? "[MATCH]" : "[DIFFER]"));
  } else {
    // Large T: report only the chunked path's final-position argmax.
    const int last_c = ((T - 1) % chunk) + 1;
    const uint16_t* b_final =
        logits_b.data() + static_cast<size_t>(last_c - 1) * vocab;
    int argmax_b = 0;
    float best_b = -1e30f;
    for (int v = 0; v < vocab; ++v) {
      const uint32_t bb = static_cast<uint32_t>(b_final[v]) << 16;
      float fb;
      std::memcpy(&fb, &bb, sizeof(float));
      if (fb > best_b) { best_b = fb; argmax_b = v; }
    }
    std::printf("chunked final-position argmax=%d (one-shot skipped, T too "
                "large)\n", argmax_b);
  }

  m.Free();
  return 0;
}
