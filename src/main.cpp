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

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "q4t/model/model.h"
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
  std::string prompt;

  for (int i = 2; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--model-dir" && i + 1 < argc) {
      model_dir = argv[++i];
    } else if (a == "--max-tokens" && i + 1 < argc) {
      max_tokens = std::atoi(argv[++i]);
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

  s = q4t::model::ModelForward(model, ids.data(), static_cast<int>(ids.size()),
                               d_logits, nullptr);
  if (!s.ok()) {
    std::fprintf(stderr, "prefill failed: %s\n", s.message().c_str());
    cudaFree(d_logits);
    model.Free();
    return 1;
  }

  // 5. Decode loop (greedy argmax).
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

  std::vector<int32_t> generated;
  int position = static_cast<int>(ids.size());
  int next_token = -1;
  // First decode token comes from the prefill's LAST position (row T-1).
  cudaMemcpy(h_logits.data(), d_logits + static_cast<size_t>(T_prompt - 1) * vocab,
             static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost);
  next_token = argmax(h_logits.data());

  for (int step = 0; step < max_tokens; ++step) {
    generated.push_back(next_token);
    if (next_token == cfg.eos_token_id) break;
    const int32_t tok_id = next_token;
    s = q4t::model::ModelDecodeStep(model, tok_id, position, ids.data(),
                                    d_logits, nullptr);
    if (!s.ok()) {
      std::fprintf(stderr, "decode step %d failed: %s\n", step,
                   s.message().c_str());
      break;
    }
    ids.push_back(tok_id);  // extend history for the next PLE context
    ++position;
    cudaMemcpy(h_logits.data(), d_logits, static_cast<size_t>(vocab) * 2,
               cudaMemcpyDeviceToHost);
    next_token = argmax(h_logits.data());
  }

  // 6. Decode and print.
  std::vector<std::uint32_t> gen_u32(generated.begin(), generated.end());
  std::string text;
  s = tok->Decode(gen_u32, true, &text);
  if (s.ok()) {
    std::printf("%s", text.c_str());
  }
  std::fprintf(stderr, "\n[q4t] generated %zu tokens\n", generated.size());

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
