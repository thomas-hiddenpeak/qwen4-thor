// PLE working-memory budget test.
//
// Verifies the PLE SSD-stream working memory — everything the PLE layer holds
// besides model weights: page pool + io_uring ring + pinned host staging +
// GPU FP8 scratch + pinned host row-ids + reader scratch — stays under the
// 100 MiB Phase-1 budget (ARCHITECTURE.md targets ~64 MiB; the sidecar itself
// stays on NVMe and is never resident).
//
// Creates a PleEmbedding with the REAL sidecar path and the production config
// (capacity_tokens=8192, row_bytes=160, 8 ngram heads), triggers a full-
// capacity gather to populate the reader's scratch to its peak (each 160 B row
// spans at most 2 pages -> up to 2 pieces/row), then measures
// working_memory_bytes() and asserts it is < 100 MiB.
//
// Skipped when CUDA or the real sidecar is absent.
#include "q4t/ple/ple_embedding.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <fcntl.h>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::ple::PleEmbedding;

const char* kSidecar =
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

}  // namespace

Q4T_TEST(ple_working_memory_under_100mib) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kSidecar)) {
    std::printf("  (skipped: PLE sidecar not found)\n");
    return true;
  }

  // Production config (matches ModelConfig defaults in model.h).
  PleEmbedding::Config cfg;
  cfg.sidecar_path = kSidecar;
  cfg.row_bytes = 160;
  cfg.total_rows = 320001536;
  cfg.capacity_tokens = 8192;
  cfg.eos_token_id = 248044;
  cfg.hash_params.ngram_size = 3;
  cfg.hash_params.heads_per_ngram = 8;

  PleEmbedding* pe = nullptr;
  Status s = PleEmbedding::Create(cfg, &pe);
  if (!s.ok()) {
    std::printf("  Create failed: %s\n", s.message().c_str());
    return false;
  }

  // Trigger a full-capacity gather to populate the reader scratch to its peak.
  const size_t heads = cfg.hash_params.ngram_heads();
  const size_t n_rows = cfg.capacity_tokens * heads;
  std::vector<int64_t> row_ids(n_rows);
  for (size_t i = 0; i < n_rows; ++i) row_ids[i] = static_cast<int64_t>(i);
  uint16_t* d_out = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_out),
                 n_rows * cfg.row_bytes * 2) != cudaSuccess) {
    std::printf("  cudaMalloc out failed\n");
    delete pe;
    return false;
  }
  s = pe->GatherRows(row_ids.data(), n_rows, d_out, nullptr);
  if (!s.ok()) {
    std::printf("  GatherRows failed: %s\n", s.message().c_str());
    cudaFree(d_out);
    delete pe;
    return false;
  }
  cudaDeviceSynchronize();

  // Measure the working-memory footprint (peak, after the gather).
  const size_t total = pe->working_memory_bytes();
  const double total_mib = static_cast<double>(total) / (1024.0 * 1024.0);
  const double staging_mib =
      static_cast<double>(cfg.capacity_tokens * heads * cfg.row_bytes) /
      (1024.0 * 1024.0);
  const double rowids_mib =
      static_cast<double>(cfg.capacity_tokens * heads * 8) / (1024.0 * 1024.0);
  std::printf("  PLE working memory = %.2f MiB (%zu bytes), budget 100 MiB\n",
              total_mib, total);
  std::printf(
      "    page pool 32.00 MiB + io_uring ring + pinned staging %.2f MiB + "
      "GPU scratch %.2f MiB + row-ids %.2f MiB + reader scratch\n",
      staging_mib, staging_mib, rowids_mib);

  Q4T_CHECK(total < static_cast<size_t>(100) * 1024 * 1024);

  cudaFree(d_out);
  delete pe;
  return true;
}
