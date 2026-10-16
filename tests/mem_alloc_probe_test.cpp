// Memory allocation probe: replicate writer-row0 setup, then probe
// cudaMalloc sizes at the capture-allocation point (test-only).
#include "q4t/model/model.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

namespace {
const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";
const char* kPleSidecar =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "ple/qwen3.8-flash-next-ple-fp8.bin";
const char* kFixture = ".q4t-work/mtp-repro-20261011/fixture.txt";

long CmaFreeKB() {
  std::ifstream in("/proc/meminfo");
  std::string line;
  while (std::getline(in, line))
    if (line.rfind("CmaFree:", 0) == 0) {
      std::istringstream ss(line.substr(8));
      long v = 0;
      ss >> v;
      return v;
    }
  return -1;
}

void MemInfo(const char* tag) {
  size_t freeb = 0, totalb = 0;
  cudaMemGetInfo(&freeb, &totalb);
  std::printf("[mem] %s: cuda free=%.2f GB total=%.2f GB cma_free=%ld kB\n",
              tag, static_cast<double>(freeb) / 1e9,
              static_cast<double>(totalb) / 1e9, CmaFreeKB());
}

std::vector<int32_t> ParseInts(const std::string& s) {
  std::vector<int32_t> v;
  std::istringstream ss(s);
  std::string tok;
  while (std::getline(ss, tok, ','))
    if (!tok.empty()) v.push_back(static_cast<int32_t>(std::stol(tok)));
  return v;
}

}  // namespace

Q4T_TEST(mem_alloc_probe) {
  int count = 0;
  if (cudaGetDeviceCount(&count) != cudaSuccess || count <= 0)
    Q4T_SKIP("(skipped: no CUDA device)");

  // Load fixture.
  std::ifstream in(kFixture);
  std::string text((std::istreambuf_iterator<char>(in)),
                   std::istreambuf_iterator<char>());
  auto field = [&](const char* key) -> std::string {
    const std::string k = std::string(key) + " ";
    std::istringstream ss(text);
    std::string line;
    while (std::getline(ss, line))
      if (line.rfind(k, 0) == 0) return line.substr(k.size());
    return std::string();
  };
  std::vector<int32_t> prompt = ParseInts(field("prompt_tokens"));
  std::vector<int32_t> prefix = ParseInts(field("prefix_tokens"));
  if (prompt.empty() || prefix.size() <= prompt.size()) {
    std::printf("  fixture parse failed\n");
    return false;
  }

  q4t::model::ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.ple_sidecar = kPleSidecar;
  cfg.max_prefill = 8192;
  cfg.max_len = 8192;
  cfg.max_seq = 1;
  q4t::model::Model m;
  q4t::Status s = q4t::model::LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }
  MemInfo("after_load");

  q4t::model::ModelSequence seq;
  uint16_t* d_plain = nullptr;
  uint16_t* d_prefill = nullptr;
  const int vocab = m.cfg.vocab;
  if (cudaMalloc(reinterpret_cast<void**>(&d_plain),
                 static_cast<size_t>(vocab) * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_prefill),
                 static_cast<size_t>(8192) * vocab * 2) != cudaSuccess) {
    std::printf("  setup cudaMalloc failed\n");
    m.Free();
    return false;
  }
  s = q4t::model::ModelBeginSequence(m, &seq, nullptr);
  if (!s.ok()) {
    std::printf("  begin failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  MemInfo("after_begin");

  s = q4t::model::ModelPrefill(m, &seq, prompt.data(),
                               static_cast<int>(prompt.size()), d_prefill,
                               nullptr);
  if (!s.ok()) {
    std::printf("  prefill failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  MemInfo("after_prefill");

  for (size_t i = prompt.size(); i < prefix.size(); ++i) {
    s = q4t::model::ModelDecodeStepSeq(m, &seq, prefix[i], d_plain, nullptr);
    if (!s.ok()) {
      std::printf("  B1 step %zu failed: %s\n", i, s.message().c_str());
      m.Free();
      return false;
    }
  }
  MemInfo("after_b1_replay");

  cudaFree(d_prefill);
  cudaFree(d_plain);
  d_prefill = d_plain = nullptr;
  MemInfo("after_free_prefill_plain");

  s = q4t::model::ModelReserveVerifyCheckpoints(m, 3);
  if (!s.ok()) {
    std::printf("  reserve ckpt failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  MemInfo("after_ckpt_reserve");

  // Probe: single allocations of decreasing size.
  const size_t sizes[] = {
      static_cast<size_t>(4) << 30,  static_cast<size_t>(1) << 30,
      static_cast<size_t>(512) << 20, static_cast<size_t>(256) << 20,
      static_cast<size_t>(128) << 20, static_cast<size_t>(64) << 20,
      static_cast<size_t>(32) << 20,  static_cast<size_t>(16) << 20,
      static_cast<size_t>(8) << 20,   static_cast<size_t>(4) << 20,
      3145728,                        static_cast<size_t>(2) << 20,
      static_cast<size_t>(1) << 20,   static_cast<size_t>(512) << 10,
      static_cast<size_t>(256) << 10, static_cast<size_t>(128) << 10,
      static_cast<size_t>(64) << 10,  static_cast<size_t>(32) << 10,
      static_cast<size_t>(4) << 10,
  };
  for (size_t sz : sizes) {
    void* p = nullptr;
    cudaError_t e = cudaMalloc(&p, sz);
    if (e == cudaSuccess) {
      std::printf("[probe] malloc %zu bytes: OK\n", sz);
      cudaFree(p);
    } else {
      std::printf("[probe] malloc %zu bytes: FAIL %s\n", sz,
                  cudaGetErrorString(e));
      cudaGetLastError();
    }
  }
  // Probe: 40 x 3145728 (ssm capture pattern).
  int ok = 0;
  for (int i = 0; i < 40; ++i) {
    void* p = nullptr;
    cudaError_t e = cudaMalloc(&p, 3145728);
    if (e != cudaSuccess) {
      std::printf("[probe] ssm-pattern #%d: FAIL %s\n", i,
                  cudaGetErrorString(e));
      cudaGetLastError();
      break;
    }
    ok++;
    cudaFree(p);
  }
  std::printf("[probe] ssm-pattern ok=%d/40\n", ok);
  MemInfo("end");

  m.Free();
  return true;
}
