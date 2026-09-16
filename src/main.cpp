// Qwen4-Thor CLI entry point.
//
// Subcommands (Phase 1):
//   version   Print version and build info.
//   probe     Probe the target device (SM110a capabilities).
//   models    List catalogued model descriptors.
//   generate  Single greedy generation.
//   serve     OpenAI-compatible HTTP API server.
//
// This is the skeleton entry point; subcommands are implemented as their
// modules land (see docs/PHASES.md).

#include <cuda_runtime.h>
#include <cuda_profiler_api.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"
#include "q4t/server/chat_server.h"
#include "q4t/text/tokenizer.h"

namespace {

void PrintVersion() {
  std::printf("q4t (Qwen4-Thor) v0.1.0 — skeleton\n");
  std::printf("  target: Jetson AGX Thor, SM110a\n");
  std::printf("  model:  Qwen3.8-Flash-Next (qwen4_exp)\n");
}

void PrintUsage(const char* prog) {
  std::printf(
      "Usage: %s <command>\n"
      "Commands:\n"
      "  version   Print version and build info\n"
      "  probe     Probe the target device\n"
      "  models    List catalogued model descriptors\n"
      "  generate  Single greedy generation\n"
      "            (q4t generate \"prompt\" [--max-tokens N])\n"
      "  serve     OpenAI-compatible HTTP API server\n"
      "            (q4t serve [--port N] [--model-dir DIR] "
      "[--max-tokens N] [--max-prefill N] [--max-len N] [--max-seq N])\n"
      "  bench-decode  Batched-decode throughput vs batch size\n"
      "            (q4t bench-decode [--batch B] [--steps N] [--prompt P] "
      "[--max-len L])\n"
      "  bench-prefill Batched-prefill (ragged) vs sequential, tok/s + speedup\n"
      "            (q4t bench-prefill [--batch B] [--prompt P] [--sweep])\n",
      prog);
}

int RunProbe() {
  int device_count = 0;
  cudaError_t err = cudaGetDeviceCount(&device_count);
  if (err != cudaSuccess) {
    std::fprintf(stderr, "cudaGetDeviceCount failed: %s\n",
                 cudaGetErrorString(err));
    return 1;
  }
  if (device_count == 0) {
    std::fprintf(stderr, "No CUDA devices found.\n");
    return 1;
  }
  for (int i = 0; i < device_count; ++i) {
    cudaDeviceProp prop{};
    err = cudaGetDeviceProperties(&prop, i);
    if (err != cudaSuccess) {
      std::fprintf(stderr, "cudaGetDeviceProperties(%d) failed: %s\n", i,
                   cudaGetErrorString(err));
      return 1;
    }
    std::printf("Device %d: %s\n", i, prop.name);
    std::printf("  Compute capability: %d.%d\n", prop.major, prop.minor);
    std::printf("  SM count:           %d\n", prop.multiProcessorCount);
    std::printf("  Global memory:      %.1f GB\n",
                static_cast<double>(prop.totalGlobalMem) / (1024.0 * 1024.0 *
                                                            1024.0));
    std::printf("  L2 cache:           %.1f MB\n",
                static_cast<double>(prop.l2CacheSize) / (1024.0 * 1024.0));
    std::printf("  Shared mem/SM:      %zu KB\n",
                prop.sharedMemPerMultiprocessor / 1024);
    std::printf("  Max threads/SM:     %d\n", prop.maxThreadsPerMultiProcessor);
    int clock_khz = 0;
    if (cudaDeviceGetAttribute(&clock_khz, cudaDevAttrClockRate, i) ==
        cudaSuccess) {
      std::printf("  Clock rate:         %d MHz\n", clock_khz / 1000);
    }
  }
  return 0;
}

// Single greedy generation: encode the prompt, run a prefill, then decode
// token-by-token (argmax) until EOS or --max-tokens, and print the result.
int RunGenerate(int argc, char** argv) {
  const char* kDefaultModelDir =
      "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  std::string model_dir = kDefaultModelDir;
  int max_tokens = 64;
  bool use_mtp = false;
  int mtp_k = 3;  // 实测最优 (k=3 1.46x; 见 docs/LOG.md 2026-09-10)
  int max_prefill = 0;  // 0 = use ModelConfig default (2048)
  std::string prompt;

  for (int i = 2; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--model-dir" && i + 1 < argc) {
      model_dir = argv[++i];
    } else if (a == "--max-tokens" && i + 1 < argc) {
      max_tokens = std::atoi(argv[++i]);
    } else if (a == "--mtp") {
      use_mtp = true;
    } else if (a == "--mtp-k" && i + 1 < argc) {
      mtp_k = std::atoi(argv[++i]);
    } else if (a == "--max-prefill" && i + 1 < argc) {
      max_prefill = std::atoi(argv[++i]);
    } else if (!a.empty() && a[0] == '-') {
      std::fprintf(stderr, "Unknown option: %s\n", a.c_str());
      return 2;
    } else {
      if (!prompt.empty()) prompt += " ";
      prompt += a;
    }
  }
  if (prompt.empty()) {
    std::fprintf(stderr, "Usage: q4t generate \"prompt\" "
                         "[--max-tokens N] [--model-dir DIR]\n");
    return 2;
  }

  // 1. Tokenizer.
  q4t::text::TokenizerLimits limits;
  std::unique_ptr<q4t::text::Tokenizer> tok;
  q4t::Status s = q4t::text::Tokenizer::Load(
      model_dir + "/tokenizer.json", limits, &tok);
  if (!s.ok()) {
    std::fprintf(stderr, "tokenizer load failed: %s\n", s.message().c_str());
    return 1;
  }

  // 2. Model.
  q4t::model::ModelConfig cfg;
  cfg.model_dir = model_dir;
  cfg.index_path = model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar =
      model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  if (max_prefill > 0) cfg.max_prefill = max_prefill;
  q4t::model::Model model;
  s = q4t::model::LoadModel(cfg, &model, nullptr);
  if (!s.ok()) {
    std::fprintf(stderr, "model load failed: %s\n", s.message().c_str());
    return 1;
  }
  std::fprintf(stderr, "[q4t] model loaded (%d layers)\n", cfg.num_layers);

  // 2b. MTP draft model (optional, --mtp). Borrowed embed/lm_head from main.
  q4t::mtp::MtpConfig mcfg;
  q4t::mtp::MtpModel mtp;
  bool mtp_loaded = false;
  if (use_mtp) {
    mcfg.mtp_dir = model_dir + "/mtp";
    mcfg.max_prefill = cfg.max_prefill;  // MTP draft-extend runs over the whole
                                         // prompt; size its workspace to match.
    auto t_mtp0 = std::chrono::steady_clock::now();
    s = q4t::mtp::LoadMtp(mcfg, model.head.embed_tokens, model.head.lm_head,
                          &mtp, nullptr);
    if (!s.ok()) {
      std::fprintf(stderr, "MTP load failed (falling back to plain decode): "
                           "%s\n",
                   s.message().c_str());
    } else {
      mtp_loaded = true;
      auto t_mtp1 = std::chrono::steady_clock::now();
      std::fprintf(stderr, "[q4t] MTP loaded (k=%d, %.1f ms)\n", mtp_k,
                   std::chrono::duration<double, std::milli>(t_mtp1 - t_mtp0)
                       .count());
    }
  }

  // 3. Encode prompt.
  std::vector<std::uint32_t> prompt_ids_u32;
  s = tok->Encode(prompt, &prompt_ids_u32);
  if (!s.ok()) {
    std::fprintf(stderr, "encode failed: %s\n", s.message().c_str());
    model.Free();
    return 1;
  }
  std::vector<int32_t> ids(prompt_ids_u32.begin(), prompt_ids_u32.end());
  std::fprintf(stderr, "[q4t] prompt: %zu tokens\n", ids.size());

  // 4. Prefill.
  const int vocab = cfg.vocab;
  const int T_prompt = static_cast<int>(ids.size());
  // d_logits must hold T_prompt rows (prefill lm_head GEMM outputs [T, vocab]).
  // Decode steps write 1 row (T=1) to row 0, which fits within this allocation.
  uint16_t* d_logits = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits),
                 static_cast<size_t>(T_prompt) * vocab * 2) != cudaSuccess) {
    std::fprintf(stderr, "cudaMalloc logits failed\n");
    model.Free();
    return 1;
  }
  std::vector<uint16_t> h_logits(static_cast<size_t>(vocab));

  // MTP trunk buffers (pre-final-mixer multi stream [hc*hs]).
  const size_t hc_dim =
      static_cast<size_t>(mcfg.hc) * static_cast<size_t>(mcfg.hs);
  uint16_t* d_trunk_full = nullptr;  // [T_prompt, hc_dim] (prefill trunk_out)
  uint16_t* d_g = nullptr;           // [hc_dim] (current draft trunk)
  uint16_t* d_g_next = nullptr;      // [hc_dim] (next draft trunk)
  if (mtp_loaded) {
    if (cudaMalloc(reinterpret_cast<void**>(&d_trunk_full),
                   static_cast<size_t>(T_prompt) * hc_dim * 2) !=
            cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_g), hc_dim * 2) != cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_g_next), hc_dim * 2) !=
            cudaSuccess) {
      std::fprintf(stderr, "cudaMalloc trunk failed\n");
      cudaFree(d_logits);
      mtp.Free();
      model.Free();
      return 1;
    }
  }

  // PD-ready 阶段边界 API: Begin (reset state) -> Prefill (handoff point)
  // -> DecodeStepSeq (auto position/history).
  // Warm up the CUDA context / cuBLASLt heuristics with a tiny prefill so the
  // first real prefill isn't inflated by one-time init (kernel load, workspace
  // alloc, autotune). Must run BEFORE the real ModelBeginSequence: the warmup's
  // own Begin/Prefill leaves the per-layer state dirty, and the real Begin below
  // resets it. This is a benchmarking concern, not a correctness one.
  {
    // Warmup must use the SAME T as the real prefill so it exercises the
    // GEMM (cuBLASLt) path and pre-warms the cuBLASLt heuristics for this
    // exact (M, N, K) shape. A T=1 warmup would take the GEMV path (pure
    // kernel, no cuBLASLt) and leave the real prefill's first cuBLASLt call
    // to pay the one-time heuristics overhead.
    std::vector<int32_t> warm(ids);
    uint16_t* d_warm = nullptr;
    if (cudaMalloc(reinterpret_cast<void**>(&d_warm),
                   static_cast<size_t>(ids.size()) * vocab * 2) == cudaSuccess) {
      auto t_warm0 = std::chrono::steady_clock::now();
      q4t::model::ModelSequence wseq;
      q4t::model::ModelBeginSequence(model, &wseq, nullptr);
      q4t::model::ModelPrefill(model, &wseq, warm.data(),
                               static_cast<int>(warm.size()), d_warm, nullptr);
      q4t::model::ModelEndSequence(&wseq);
      cudaFree(d_warm);
      auto t_warm1 = std::chrono::steady_clock::now();
      std::fprintf(stderr, "[q4t] warmup T=%zu: %.1f ms\n", warm.size(),
                   std::chrono::duration<double, std::milli>(t_warm1 - t_warm0)
                       .count());
    }
  }
  q4t::model::ModelSequence seq;
  s = q4t::model::ModelBeginSequence(model, &seq, nullptr);
  if (!s.ok()) {
    std::fprintf(stderr, "begin sequence failed: %s\n", s.message().c_str());
    cudaFree(d_logits);
    model.Free();
    return 1;
  }
  auto t_prefill_start = std::chrono::steady_clock::now();
  s = q4t::model::ModelPrefill(model, &seq, ids.data(),
                               static_cast<int>(ids.size()), d_logits, nullptr,
                               mtp_loaded ? d_trunk_full : nullptr);
  if (!s.ok()) {
    std::fprintf(stderr, "prefill failed: %s\n", s.message().c_str());
    cudaFree(d_logits);
    if (mtp_loaded) mtp.Free();
    model.Free();
    return 1;
  }
  cudaDeviceSynchronize();
  auto t_prefill_end = std::chrono::steady_clock::now();

  // Greedy argmax over a host BF16 logits row.
  auto argmax = [&](const uint16_t* h) {
    int best = 0;
    float best_v = -1e30f;
    for (int v = 0; v < vocab; ++v) {
      const uint32_t bits = static_cast<uint32_t>(h[v]) << 16;
      float f;
      std::memcpy(&f, &bits, sizeof(f));
      if (f > best_v) {
        best_v = f;
        best = v;
      }
    }
    return best;
  };

  // MTP init: fresh KV/indexer state, sample the bonus token b = t_P (main's
  // argmax at the last prompt position), then draft-extend over the prompt to
  // build the draft KV[0..P-1] and seed the first speculative step (d0 + g).
  int32_t mtp_b = -1, mtp_d0 = -1;
  if (mtp_loaded) {
    s = q4t::mtp::MtpResetState(mtp, nullptr);
    if (!s.ok()) {
      std::fprintf(stderr, "MTP reset failed: %s\n", s.message().c_str());
      cudaFree(d_logits);
      mtp.Free();
      model.Free();
      return 1;
    }
    cudaMemcpy(h_logits.data(),
               d_logits + static_cast<size_t>(T_prompt - 1) * vocab,
               static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost);
    mtp_b = argmax(h_logits.data());
    // EAGLE shift: shifted_ids[p] = t_{p+1}, with t_P := b at the tail.
    std::vector<int32_t> shifted(T_prompt);
    for (int i = 0; i < T_prompt - 1; ++i) shifted[i] = ids[i + 1];
    shifted[T_prompt - 1] = mtp_b;
    std::vector<int> pos(T_prompt);
    for (int i = 0; i < T_prompt; ++i) pos[i] = i;
    s = q4t::mtp::MtpDraftExtend(mtp, shifted.data(), d_trunk_full, pos.data(),
                                 T_prompt, &mtp_d0, d_g, nullptr);
    if (!s.ok()) {
      std::fprintf(stderr, "MTP draft-extend failed: %s\n",
                   s.message().c_str());
      cudaFree(d_logits);
      mtp.Free();
      model.Free();
      return 1;
    }
    // Reserve per-token SSM/conv checkpoint buffers so MtpSpeculativeStep's
    // batched verify can save checkpoints and a partial accept can D2D-restore
    // checkpoint[a] instead of re-running the main forward.
    s = q4t::model::ModelReserveVerifyCheckpoints(model, mtp_k);
    if (!s.ok()) {
      std::fprintf(stderr, "MTP checkpoint reserve failed: %s\n",
                   s.message().c_str());
      cudaFree(d_logits);
      mtp.Free();
      model.Free();
      return 1;
    }
    // Reserve the MTP per-step scratch so the speculative step + internal
    // extend reuse persistent buffers instead of per-step cudaMalloc/cudaFree
    // (each cudaFree is an implicit device sync that stalls the pipeline).
    s = q4t::mtp::MtpReserveScratch(mtp, mtp_k + 1);
    if (!s.ok()) {
      std::fprintf(stderr, "MTP scratch reserve failed: %s\n",
                   s.message().c_str());
      cudaFree(d_logits);
      mtp.Free();
      model.Free();
      return 1;
    }
  }

  // Q4T_PROFILE=1: bracket the decode phase with cudaProfilerStart/Stop so
  // `nsys profile --capture-range=cudaProfilerApi` captures only decode
  // (skipping the ~15s model load).
  const bool profile = std::getenv("Q4T_PROFILE") != nullptr;
  if (profile) cudaProfilerStart();
  std::vector<int32_t> generated;
  auto t_decode_start = std::chrono::steady_clock::now();
  if (mtp_loaded) {
    // Speculative decoding: each step emits the bonus b + accepted drafts and
    // yields the next (b, d0, g). MtpSpeculativeStep advances the main seq over
    // exactly the accepted prefix (lazy verification, no rollback).
    int32_t accepted_tokens[64];
    int accepted_count = 0;
    int mtp_steps = 0, mtp_accepted = 0;
    bool done = false;
    while (!done && static_cast<int>(generated.size()) < max_tokens) {
      int32_t next_b = -1, next_d0 = -1;
      s = q4t::mtp::MtpSpeculativeStep(model, mtp, &seq, mtp_b, mtp_d0, d_g,
                                       mtp_k, accepted_tokens, &accepted_count,
                                       &next_b, &next_d0, d_g_next, nullptr);
      if (!s.ok()) {
        std::fprintf(stderr, "speculative step failed: %s\n",
                     s.message().c_str());
        break;
      }
      if (accepted_count <= 0) break;
      mtp_steps++;
      mtp_accepted += accepted_count;
      for (int i = 0; i < accepted_count &&
                          static_cast<int>(generated.size()) < max_tokens;
           ++i) {
        generated.push_back(accepted_tokens[i]);
        if (accepted_tokens[i] == cfg.eos_token_id) {
          done = true;
          break;
        }
      }
      // Roll (b, d0, g) forward for the next step.
      mtp_b = next_b;
      mtp_d0 = next_d0;
      uint16_t* tmp = d_g;
      d_g = d_g_next;
      d_g_next = tmp;
    }
    std::fprintf(stderr,
                 "[q4t] MTP: %d steps, %d tokens, avg %.2f tok/step "
                 "(k=%d)\n",
                 mtp_steps, mtp_accepted,
                 mtp_steps ? static_cast<double>(mtp_accepted) / mtp_steps
                           : 0.0,
                 mtp_k);
  } else {
    // Plain greedy decode (baseline).
    int next_token = -1;
    // First decode token comes from the prefill's LAST position (row T-1).
    cudaMemcpy(h_logits.data(),
               d_logits + static_cast<size_t>(T_prompt - 1) * vocab,
               static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost);
    next_token = argmax(h_logits.data());
    for (int step = 0; step < max_tokens; ++step) {
      generated.push_back(next_token);
      if (next_token == cfg.eos_token_id) break;
      const int32_t tok_id = next_token;
      s = q4t::model::ModelDecodeStepSeq(model, &seq, tok_id, d_logits,
                                         nullptr);
      if (!s.ok()) {
        std::fprintf(stderr, "decode step %d failed: %s\n", step,
                     s.message().c_str());
        break;
      }
      cudaMemcpy(h_logits.data(), d_logits,
                 static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost);
      next_token = argmax(h_logits.data());
    }
  }
  cudaDeviceSynchronize();
  if (profile) cudaProfilerStop();
  auto t_decode_end = std::chrono::steady_clock::now();
  q4t::model::ModelEndSequence(&seq);

  // 6. Decode and print.
  std::vector<std::uint32_t> gen_u32(generated.begin(), generated.end());
  std::string text;
  s = tok->Decode(gen_u32, true, &text);
  if (s.ok()) {
    std::printf("%s", text.c_str());
  }
  const double prefill_ms = std::chrono::duration<double, std::milli>(
                                t_prefill_end - t_prefill_start).count();
  const double decode_ms = std::chrono::duration<double, std::milli>(
                               t_decode_end - t_decode_start).count();
  const double prefill_tps =
      prefill_ms > 0 ? T_prompt / (prefill_ms / 1000.0) : 0.0;
  const double decode_tps =
      decode_ms > 0 ? static_cast<double>(generated.size()) / (decode_ms / 1000.0)
                    : 0.0;
  std::fprintf(stderr, "\n[q4t] generated %zu tokens\n", generated.size());
  std::fprintf(stderr,
               "[q4t] perf: prefill %d tok in %.1f ms (%.1f tok/s) | "
               "decode %zu tok in %.1f ms (%.1f tok/s)%s\n",
               T_prompt, prefill_ms, prefill_tps, generated.size(), decode_ms,
               decode_tps, mtp_loaded ? " [MTP]" : "");

  if (mtp_loaded) {
    cudaFree(d_trunk_full);
    cudaFree(d_g);
    cudaFree(d_g_next);
    mtp.Free();
  }
  cudaFree(d_logits);
  model.Free();
  return 0;
}

// OpenAI-compatible HTTP API server: load the model once, then serve
// /healthz, /v1/models, and /v1/chat/completions (stream + non-stream).
// Batched-decode throughput benchmark: prefill B sequences (distinct prompts),
// then run N packed decode steps (ModelDecodeBatchMulti, T=B) with realistic
// per-sequence routing, and report aggregate tok/s. This measures how decode
// throughput scales with batch size — the memory-bound MoE weight load is read
// ONCE per packed forward, so aggregate tok/s should climb with B until the
// per-token expert overlap (B >~ E/k) lets the weights amortize. No argmax /
// D2H in the timed loop (synthetic tokens) so it measures pure forward. With
// --sweep, loads once at max_seq=--batch and benchmarks B=1,2,4,...,--batch
// (subsets of the pooled state); positions are pinned to [P, P+N) each run so
// the attention range is constant across B.
int RunBenchDecode(int argc, char** argv) {
  const char* kDefaultModelDir =
      "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  std::string model_dir = kDefaultModelDir;
  int maxB = 8, N = 64, P = 128, L = 0;  // L=0 -> auto (P+N+slack)
  bool sweep = false;
  for (int i = 2; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--batch" && i + 1 < argc) {
      maxB = std::atoi(argv[++i]);
    } else if (a == "--steps" && i + 1 < argc) {
      N = std::atoi(argv[++i]);
    } else if (a == "--prompt" && i + 1 < argc) {
      P = std::atoi(argv[++i]);
    } else if (a == "--max-len" && i + 1 < argc) {
      L = std::atoi(argv[++i]);
    } else if (a == "--sweep") {
      sweep = true;
    } else if (a == "--model-dir" && i + 1 < argc) {
      model_dir = argv[++i];
    } else {
      std::fprintf(stderr, "Unknown option: %s\n", a.c_str());
      return 2;
    }
  }
  if (maxB <= 0 || N <= 0 || P <= 0) {
    std::fprintf(stderr, "bench-decode: --batch/--steps/--prompt must be > 0\n");
    return 2;
  }
  if (L <= 0) L = P + N + 8;

  q4t::model::ModelConfig cfg;
  cfg.model_dir = model_dir;
  cfg.index_path = model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.max_seq = maxB;
  cfg.max_len = L;
  cfg.max_prefill = std::max(P, 8);
  q4t::model::Model model;
  q4t::Status s = q4t::model::LoadModel(cfg, &model, nullptr);
  if (!s.ok()) {
    std::fprintf(stderr, "model load failed: %s\n", s.message().c_str());
    return 1;
  }
  const int vocab = cfg.vocab;
  const int hist_w = model.ple_emb ? model.ple_hash.ngram_size - 1 : 0;
  std::fprintf(stderr,
               "[q4t] bench-decode: max_seq=%d steps=%d prompt=%d max_len=%d\n",
               maxB, N, P, L);

  uint16_t* d_pref = nullptr;  // [P, vocab] prefill logits (reused per seq)
  uint16_t* d_dec = nullptr;   // [maxB, vocab] decode logits
  if (cudaMalloc(reinterpret_cast<void**>(&d_pref),
                 static_cast<size_t>(P) * vocab * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_dec),
                 static_cast<size_t>(maxB) * vocab * 2) != cudaSuccess) {
    std::fprintf(stderr, "bench-decode: logits cudaMalloc failed\n");
    model.Free();
    return 1;
  }

  // Prefill maxB sequences with distinct prompts (distinct routing per seq).
  std::vector<q4t::model::ModelSequence> seqs(maxB);
  for (int b = 0; b < maxB; ++b) {
    std::vector<int32_t> prompt(P);
    for (int i = 0; i < P; ++i)
      prompt[i] = 100 + ((b * 131 + i * 17) % 20000);
    q4t::model::ModelBeginSequence(model, &seqs[b], nullptr, b);
    s = q4t::model::ModelPrefill(model, &seqs[b], prompt.data(), P, d_pref,
                                 nullptr, nullptr, nullptr, b);
    if (!s.ok()) {
      std::fprintf(stderr, "bench-decode: prefill seq %d failed: %s\n", b,
                   s.message().c_str());
      cudaFree(d_pref);
      cudaFree(d_dec);
      model.Free();
      return 1;
    }
  }
  cudaDeviceSynchronize();

  std::vector<int32_t> tokens(maxB), history(static_cast<size_t>(maxB) *
                                             std::max(hist_w, 1));
  std::vector<int> positions(maxB), seq_ids(maxB);
  for (int b = 0; b < maxB; ++b) {
    seq_ids[b] = b;
    for (int j = 0; j < hist_w; ++j)
      history[static_cast<size_t>(b) * hist_w + j] =
          100 + ((b * 131 + j * 17) % 20000);
  }
  auto syn = [](int b, int step) {
    long v = (static_cast<long>(b) * 9973 + static_cast<long>(step) * 131 + 7) %
             20000;
    if (v < 0) v += 20000;  // C++ modulo of a negative is negative
    return 100 + static_cast<int>(v);
  };

  // One decode-throughput run at batch Bi: warmup + N timed packed steps,
  // positions pinned to [P, P+N) (constant attention range across Bi).
  auto bench = [&](int Bi) {
    auto step_forward = [&](int pos, int step) -> q4t::Status {
      for (int b = 0; b < Bi; ++b) {
        positions[b] = pos;
        tokens[b] = syn(b, step);
      }
      return q4t::model::ModelDecodeBatchMulti(
          model, tokens.data(), positions.data(), seq_ids.data(),
          history.data(), Bi, d_dec, nullptr);
    };
    q4t::Status st = step_forward(P, -1);  // warmup (cuBLASLt heuristics)
    cudaDeviceSynchronize();
    if (!st.ok()) {
      std::fprintf(stderr, "bench-decode: B=%d warmup failed: %s\n", Bi,
                   st.message().c_str());
      return;
    }
    auto t0 = std::chrono::steady_clock::now();
    cudaProfilerStart();
    for (int step = 0; step < N; ++step) {
      st = step_forward(P + (step % N), step);
      if (!st.ok()) {
        std::fprintf(stderr, "bench-decode: B=%d step %d failed: %s\n", Bi, step,
                     st.message().c_str());
        return;
      }
    }
    cudaProfilerStop();
    cudaDeviceSynchronize();
    auto t1 = std::chrono::steady_clock::now();
    const double sec = std::chrono::duration<double>(t1 - t0).count();
    const double agg = static_cast<double>(Bi) * N / sec;
    std::printf(
        "[q4t] bench-decode B=%3d | aggregate %8.1f tok/s | %6.2f tok/s/seq | "
        "%6.2f ms/step\n",
        Bi, agg, agg / Bi, sec * 1000.0 / N);
  };

  if (sweep) {
    for (int Bi = 1; Bi < maxB; Bi *= 2) bench(Bi);
    bench(maxB);
  } else {
    bench(maxB);
  }

  cudaFree(d_pref);
  cudaFree(d_dec);
  model.Free();
  return 0;
}

// bench-prefill: quantify the ragged batched-prefill win. Prefill B distinct
// SHORT prompts (a) one-at-a-time via B ModelPrefill calls (the dense weights
// are re-read for EACH sequence) and (b) packed via one ModelPrefillBatch (the
// weights are read ONCE for the whole batch), reporting tok/s + speedup. Short
// prompts are weight-bandwidth-bound (few tokens to amortize the weight sweep),
// so batching many of them is the serve prefill-burst win. With --sweep,
// benchmarks B = 1, 2, 4, ..., --batch.
int RunBenchPrefill(int argc, char** argv) {
  const char* kDefaultModelDir =
      "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  std::string model_dir = kDefaultModelDir;
  int maxB = 16, P = 64;  // batch size, per-prompt length
  bool sweep = false;
  for (int i = 2; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--batch" && i + 1 < argc) {
      maxB = std::atoi(argv[++i]);
    } else if (a == "--prompt" && i + 1 < argc) {
      P = std::atoi(argv[++i]);
    } else if (a == "--sweep") {
      sweep = true;
    } else if (a == "--model-dir" && i + 1 < argc) {
      model_dir = argv[++i];
    } else {
      std::fprintf(stderr, "Unknown option: %s\n", a.c_str());
      return 2;
    }
  }
  if (maxB <= 0 || P <= 0) {
    std::fprintf(stderr, "bench-prefill: --batch/--prompt must be > 0\n");
    return 2;
  }

  q4t::model::ModelConfig cfg;
  cfg.model_dir = model_dir;
  cfg.index_path = model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.max_seq = maxB;
  cfg.max_len = std::max(P + 8, 256);  // full-attention KV/indexer need slack
  cfg.max_prefill = maxB * P + 8;  // Ttot = B*P must fit
  q4t::model::Model model;
  q4t::Status s = q4t::model::LoadModel(cfg, &model, nullptr);
  if (!s.ok()) {
    std::fprintf(stderr, "model load failed: %s\n", s.message().c_str());
    return 1;
  }
  const int vocab = cfg.vocab;
  std::fprintf(stderr, "[q4t] bench-prefill: max_seq=%d prompt=%d\n", maxB, P);

  uint16_t* d_logits = nullptr;  // [maxB*P, vocab] packed prefill logits
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits),
                 static_cast<size_t>(maxB) * P * vocab * 2) != cudaSuccess) {
    std::fprintf(stderr, "bench-prefill: logits cudaMalloc failed\n");
    model.Free();
    return 1;
  }

  // maxB distinct prompts (distinct MoE routing per sequence).
  std::vector<std::vector<int32_t>> prompts(maxB);
  for (int b = 0; b < maxB; ++b) {
    prompts[b].resize(P);
    for (int i = 0; i < P; ++i)
      prompts[b][i] = 100 + ((b * 131 + i * 17) % 20000);
  }

  auto bench = [&](int Bi) {
    // (a) Sequential: Bi ModelPrefill calls (weights re-read per sequence).
    {  // warmup (cuBLASLt M=P heuristics)
      q4t::model::ModelSequence w;
      q4t::model::ModelBeginSequence(model, &w, nullptr, 0);
      q4t::Status wst = q4t::model::ModelPrefill(model, &w, prompts[0].data(),
                                                 P, d_logits, nullptr, nullptr,
                                                 nullptr, 0);
      cudaError_t ce = cudaDeviceSynchronize();
      if (!wst.ok() || ce != cudaSuccess) {
        std::fprintf(stderr, "bench-prefill: warmup B=%d failed: %s / %s\n", Bi,
                     wst.message().c_str(), cudaGetErrorString(ce));
        return;
      }
    }
    cudaDeviceSynchronize();
    auto t0 = std::chrono::steady_clock::now();
    for (int b = 0; b < Bi; ++b) {
      q4t::model::ModelSequence seq;
      q4t::model::ModelBeginSequence(model, &seq, nullptr, b);
      q4t::Status st = q4t::model::ModelPrefill(model, &seq, prompts[b].data(),
                                                P, d_logits, nullptr, nullptr,
                                                nullptr, b);
      if (!st.ok()) {
        std::fprintf(stderr, "bench-prefill: seq prefill %d failed: %s\n", b,
                     st.message().c_str());
        return;
      }
    }
    cudaDeviceSynchronize();
    auto t1 = std::chrono::steady_clock::now();
    const double seq_sec = std::chrono::duration<double>(t1 - t0).count();

    // (b) Batched: one ModelPrefillBatch (weights read ONCE).
    std::vector<int32_t> packed;
    packed.reserve(static_cast<size_t>(Bi) * P);
    std::vector<int> lens(Bi), sids(Bi);
    for (int b = 0; b < Bi; ++b) {
      packed.insert(packed.end(), prompts[b].begin(), prompts[b].end());
      lens[b] = P;
      sids[b] = b;
    }
    q4t::model::ModelPrefillBatch(model, packed.data(), lens.data(),
                                  sids.data(), Bi, d_logits, nullptr);  // warmup
    cudaDeviceSynchronize();
    auto t2 = std::chrono::steady_clock::now();
    q4t::Status st = q4t::model::ModelPrefillBatch(
        model, packed.data(), lens.data(), sids.data(), Bi, d_logits, nullptr);
    cudaDeviceSynchronize();
    auto t3 = std::chrono::steady_clock::now();
    if (!st.ok()) {
      std::fprintf(stderr, "bench-prefill: batch B=%d failed: %s\n", Bi,
                   st.message().c_str());
      return;
    }
    const double bat_sec = std::chrono::duration<double>(t3 - t2).count();
    const double toks = static_cast<double>(Bi) * P;
    std::printf(
        "[q4t] bench-prefill B=%3d | seq %8.1f tok/s (%6.1f ms) | batch %8.1f "
        "tok/s (%6.1f ms) | speedup %.2fx\n",
        Bi, toks / seq_sec, seq_sec * 1000.0, toks / bat_sec, bat_sec * 1000.0,
        seq_sec / bat_sec);
  };

  if (sweep) {
    for (int Bi = 1; Bi < maxB; Bi *= 2) bench(Bi);
    bench(maxB);
  } else {
    bench(maxB);
  }

  cudaFree(d_logits);
  model.Free();
  return 0;
}

int RunServe(int argc, char** argv) {
  const char* kDefaultModelDir =
      "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  q4t::server::ServerOptions opts;
  opts.model_dir = kDefaultModelDir;

  for (int i = 2; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--port" && i + 1 < argc) {
      opts.port = std::atoi(argv[++i]);
    } else if (a == "--model-dir" && i + 1 < argc) {
      opts.model_dir = argv[++i];
    } else if (a == "--max-tokens" && i + 1 < argc) {
      opts.max_tokens = std::atoi(argv[++i]);
    } else if (a == "--max-prefill" && i + 1 < argc) {
      opts.max_prefill = std::atoi(argv[++i]);
    } else if (a == "--max-len" && i + 1 < argc) {
      opts.max_len = std::atoi(argv[++i]);
    } else if (a == "--max-seq" && i + 1 < argc) {
      opts.max_seq = std::atoi(argv[++i]);
    } else if (a == "--no-mtp") {
      opts.no_mtp = true;
    } else {
      std::fprintf(stderr, "Unknown option: %s\n", a.c_str());
      return 2;
    }
  }

  q4t::server::ChatServer server;
  q4t::Status s = server.Start(opts);
  if (!s.ok()) {
    std::fprintf(stderr, "serve start failed: %s\n", s.message().c_str());
    return 1;
  }
  s = server.Run();
  if (!s.ok()) {
    std::fprintf(stderr, "serve stopped: %s\n", s.message().c_str());
    return 1;
  }
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    PrintUsage(argv[0]);
    return 1;
  }
  const std::string cmd = argv[1];
  if (cmd == "version") {
    PrintVersion();
    return 0;
  }
  if (cmd == "probe") {
    return RunProbe();
  }
  if (cmd == "generate") {
    return RunGenerate(argc, argv);
  }
  if (cmd == "bench-decode") {
    return RunBenchDecode(argc, argv);
  }
  if (cmd == "bench-prefill") {
    return RunBenchPrefill(argc, argv);
  }
  if (cmd == "serve") {
    return RunServe(argc, argv);
  }
  if (cmd == "models") {
    std::fprintf(stderr, "'%s' is not implemented yet (see docs/PHASES.md)\n",
                 cmd.c_str());
    return 2;
  }
  std::fprintf(stderr, "Unknown command: %s\n", cmd.c_str());
  PrintUsage(argv[0]);
  return 1;
}
