// Sparse-gather bandwidth micro-benchmark.
//
// Question: the production SparseAttentionKernel reads ~2048 scattered 1KB
// regions (topk positions ordered by logit score, i.e. arbitrary memory
// order) per token and runs at 176 GB/s = 73% of the 241 GB/s LPDDR5x peak.
// Memory-level parallelism is already sufficient (~1.6 MB in flight across
// resident blocks), so the 27% gap is suspected to be DRAM locality from the
// scatter. This bench isolates that: it reads the SAME set of 1KB regions in
// (a) random (score) order vs (b) sorted (memory-address) order and reports
// the bandwidth of each. If (b) >> (a), sorting topk positions by slot is the
// lever; if they match, the scatter ceiling is ~73% and we look elsewhere.
//
// Access pattern mirrors the real kernel: 16 positions per chunk, each 1KB
// read as 64 x 16B (32 K + 32 V), grid (T, nkv).
//
// Build: see CMakeLists (q4t_sparse_gather_microbench).
// Run:   ./build/q4t_sparse_gather_microbench [--slots 65536] [--iters 20]

#include <cuda_runtime.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <algorithm>
#include <vector>

using u16 = unsigned short;
constexpr int kHd = 256;
constexpr int kNkv = 2;
constexpr int kTopk = 2048;  // positions per token (512 blocks x compress 4)
constexpr int kChunk = 16;

__global__ void GatherKernel(const u16* __restrict__ kv,
                             const int* __restrict__ sel, int n_topk, int nkv,
                             int hd, float* __restrict__ sink) {
  // kv layout [n_slots, nkv, 2, hd] u16: per (slot, kvh) K[hd] then V[hd] = 1KB.
  const int t = blockIdx.x;
  const int kvh = blockIdx.y;
  const int* row = sel + static_cast<size_t>(t) * n_topk;
  float acc = 0.f;
  for (int c = 0; c < n_topk; c += kChunk) {
    const int chunk = (n_topk - c < kChunk) ? (n_topk - c) : kChunk;
    // 16B-granular reads, same shape as the production stage().
    for (int i = threadIdx.x; i < chunk * 64; i += 256) {
      const int pc = i >> 6;
      const int u = i & 63;
      const int is_v = u >> 5;
      const int d8 = (u & 31) * 8;
      const int p = row[c + pc];
      const size_t g = (static_cast<size_t>(p) * nkv + kvh) * (2 * hd) +
                       (is_v ? hd : 0) + d8;
      acc += static_cast<float>(kv[g]);
    }
    __syncthreads();
  }
  if (acc == 123456.789f) sink[0] = acc;  // defeat DCE
}

static double TimeMs(int iters, const std::function<void()>& f) {
  cudaEvent_t a, b;
  cudaEventCreate(&a);
  cudaEventCreate(&b);
  f();  // warm
  cudaDeviceSynchronize();
  cudaEventRecord(a);
  for (int i = 0; i < iters; ++i) f();
  cudaEventRecord(b);
  cudaEventSynchronize(b);
  float ms = 0.f;
  cudaEventElapsedTime(&ms, a, b);
  cudaEventDestroy(a);
  cudaEventDestroy(b);
  return ms / iters;
}

int main(int argc, char** argv) {
  int iters = 20;
  int T = 8192;
  int topk = 2048;  // per-token positions (production: 512 blocks x 4)
  int slots = 2 << 20; // 2GB pool default -> each 1KB read ~once (true HBM)
  for (int i = 1; i < argc; ++i) {
    if (!strcmp(argv[i], "--iters") && i + 1 < argc) iters = atoi(argv[++i]);
    if (!strcmp(argv[i], "--t") && i + 1 < argc) T = atoi(argv[++i]);
    if (!strcmp(argv[i], "--topk") && i + 1 < argc) topk = atoi(argv[++i]);
    if (!strcmp(argv[i], "--slots") && i + 1 < argc) slots = atoi(argv[++i]);
  }
  const size_t kv_bytes = static_cast<size_t>(slots) * kNkv * 2 * kHd * 2;
  u16* d_kv = nullptr;
  int* d_sel = nullptr;
  float* d_sink = nullptr;
  if (cudaMalloc(&d_kv, kv_bytes) != cudaSuccess ||
      cudaMalloc(&d_sel, static_cast<size_t>(T) * kTopk * 4) != cudaSuccess ||
      cudaMalloc(&d_sink, 4) != cudaSuccess) {
    std::fprintf(stderr, "alloc failed\n");
    return 1;
  }
  std::vector<u16> kv(kv_bytes / 2);
  for (size_t i = 0; i < kv.size(); ++i)
    kv[i] = static_cast<u16>(i & 0xffff);
  cudaMemcpy(d_kv, kv.data(), kv_bytes, cudaMemcpyHostToDevice);

  // Each token gets its OWN random 512-block subset (as in production: every
  // query's top-k is a different scattered set), so the working set spans the
  // whole KV pool -> HBM-bound. The two variants read the SAME positions per
  // token, differing only in order: random (score) vs sorted (address).
  const int nblk = topk / 4;
  std::vector<int> sel_rand(static_cast<size_t>(T) * topk),
      sel_sorted(static_cast<size_t>(T) * topk);
  std::vector<int> row(topk);
  for (int t = 0; t < T; ++t) {
    for (int b = 0; b < nblk; ++b) {
      const int blk = (rand() % (slots / 4)) * 4;
      for (int j = 0; j < 4; ++j) row[b * 4 + j] = blk + j;
    }
    std::copy(row.begin(), row.end(),
              sel_rand.begin() + static_cast<size_t>(t) * topk);
    std::sort(row.begin(), row.end());
    std::copy(row.begin(), row.end(),
              sel_sorted.begin() + static_cast<size_t>(t) * topk);
  }

  const double bytes_per_iter =
      static_cast<double>(T) * topk * 1024.0 * kNkv;  // both kvh
  auto run = [&](int* d_sel) {
    GatherKernel<<<dim3(T, kNkv), 256>>>(d_kv, d_sel, topk, kNkv, kHd, d_sink);
  };
  cudaMemcpy(d_sel, sel_rand.data(), sel_rand.size() * 4, cudaMemcpyHostToDevice);
  const double ms_rand = TimeMs(iters, [&] { run(d_sel); });
  cudaMemcpy(d_sel, sel_sorted.data(), sel_sorted.size() * 4,
             cudaMemcpyHostToDevice);
  const double ms_sorted = TimeMs(iters, [&] { run(d_sel); });

  std::printf("slots=%d (KV %.1f MB)  T=%d  topk=%d  iters=%d  "
              "[pool=full iter, each 1KB read ~1x]\n",
              slots, kv_bytes / 1e6, T, topk, iters);
  std::printf("bytes read per iter (both kvh): %.2f GB\n",
              bytes_per_iter / 1e9);
  std::printf("random  order: %8.3f ms  -> %7.1f GB/s\n", ms_rand,
              bytes_per_iter / ms_rand / 1e6);
  std::printf("sorted  order: %8.3f ms  -> %7.1f GB/s\n", ms_sorted,
              bytes_per_iter / ms_sorted / 1e6);
  std::printf("speedup sorted/random: %.3fx\n", ms_rand / ms_sorted);
  cudaFree(d_kv);
  cudaFree(d_sel);
  cudaFree(d_sink);
  return 0;
}
