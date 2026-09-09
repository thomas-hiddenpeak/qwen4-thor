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
      "[--max-tokens N])\n",
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
  }

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
