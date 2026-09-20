// Full-attention (QSA) block benchmark.
//
// Loads ONLY the layer-3 self_attn weights (not the full 84 GB model) and
// times FullAttentionForward over a sweep of T (single sequence, identity page
// table, d_seq_id=null). This is the fast iteration loop for prefill
// optimization: a full serve+nsys run takes ~50 s, this takes seconds.
//
//   q4t_bench_full_attn [--max-len N] [--ts 256,1024,2048,4096] [--iters N]
//                       [--golden <path>] [--check <path>] [--diag]
//
// --diag enables in-process determinism + per-shape GEMM determinism probes
// (a fresh isolated FullAttentionForward on a cold cublasLt cache can be
// nondeterministic; this does NOT indicate a production bug — the serve path
// is verified bit-identical across processes, see docs/LOG.md 2026-07-18).
//
// For T <= idx_budget (2048) QSA degenerates to dense causal attention
// (topk[t] = [0..t]); for T > idx_budget the sparse indexer path is active.
// The sweep straddles both regimes so the prefill cost of each is visible.

#include "q4t/io/weight_loader.h"
#include "q4t/model/full_attention.h"
#include "q4t/model/linear.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <random>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;
using q4t::model::FullAttentionForward;
using q4t::model::FullAttentionWeights;
using q4t::model::FullAttentionWorkspaceBytes;
using q4t::model::LoadFullAttention;
using q4t::model::kKvPageSize;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const int kHs = 2560;
const int kNq = 24;
const int kNkv = 2;
const int kHd = 256;
const int kRotD = 64;
const float kTheta = 1e7f;
const float kEps = 1e-6f;
const int kIdxN = 4;
const int kIdxKv = 1;
const int kIdxHd = 128;
const int kIdxBudget = 2048;
const int kIdxCompress = 4;
const std::string kPrefix = "model.language_model.layers.3.self_attn";

bool FileExists(const char* path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return false;
  close(fd);
  return true;
}

// Deterministic BF16 input ~ N(0, 1) (clipped to avoid overflow in GEMM).
std::vector<uint16_t> MakeInput(size_t n) {
  std::mt19937 rng(12345);
  std::normal_distribution<float> dist(0.f, 1.f);
  std::vector<uint16_t> out(n);
  for (size_t i = 0; i < n; ++i) {
    float v = dist(rng);
    if (v > 8.f) v = 8.f;
    if (v < -8.f) v = -8.f;
    const __nv_bfloat16 b = __float2bfloat16_rn(v);
    out[i] = *reinterpret_cast<const uint16_t*>(&b);
  }
  return out;
}

template <typename Fn>
double TimeMs(int iters, Fn&& fn) {
  // Warmup (cublas workspace init, page-table writes).
  fn();
  cudaDeviceSynchronize();
  auto t0 = std::chrono::steady_clock::now();
  for (int i = 0; i < iters; ++i) fn();
  cudaDeviceSynchronize();
  auto t1 = std::chrono::steady_clock::now();
  return std::chrono::duration<double, std::milli>(t1 - t0).count() / iters;
}

float Bf16ToFloatHost(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}
float L2Rel(const std::vector<uint16_t>& a, const std::vector<uint16_t>& b) {
  double num = 0.0, den = 0.0;
  for (size_t i = 0; i < a.size(); ++i) {
    const double d = double(Bf16ToFloatHost(a[i])) -
                     double(Bf16ToFloatHost(b[i]));
    num += d * d;
    den += double(Bf16ToFloatHost(b[i])) * double(Bf16ToFloatHost(b[i]));
  }
  return float(std::sqrt(num) / (std::sqrt(den) + 1e-9));
}

}  // namespace

int main(int argc, char** argv) {
  int max_len = 4096;
  int iters = 5;
  bool diag = false;
  bool warm_idx = false;
  bool warm_kv = false;
  bool flush_l2 = false;
  bool dump_topk = false;
  int base_pos = 0;
  std::string ts_arg = "256,1024,2048,4096";
  std::string golden_path, check_path;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--max-len" && i + 1 < argc) max_len = std::atoi(argv[++i]);
    if (a == "--iters" && i + 1 < argc) iters = std::atoi(argv[++i]);
    if (a == "--ts" && i + 1 < argc) ts_arg = argv[++i];
    if (a == "--golden" && i + 1 < argc) golden_path = argv[++i];
    if (a == "--check" && i + 1 < argc) check_path = argv[++i];
    if (a == "--diag") diag = true;
    if (a == "--dump-topk") dump_topk = true;
    // --warm-idx: pre-fill the INDEXER caches (idx_raw + idx_comp) with
    // random data ONCE and do not reset them per iteration. The indexer
    // logits are iq[t] . idx_comp[g] over the FULL history, so random
    // idx_comp makes TopkSelectKernel pick a RANDOM 512-group subset of
    // the max_len/compress groups — the production scattered-read pattern
    // (vs the zero-cache default where all logits tie at 0 and the top-k
    // degenerates to the first 512 groups, a contiguous prefix). The main
    // KV cache is still zeroed per iteration, so SparseAttentionKernel
    // reads scattered physical pages of a zero-filled (L2-cold, >32MB) KV
    // pool — exactly the production memory pattern, isolated from the
    // indexer/GEMM cost. Requires T > idx_budget to be in the sparse
    // regime.
    if (a == "--warm-idx") warm_idx = true;
    // --warm-kv: pre-fill the MAIN KV cache with random data ONCE and do not
    // reset it per iteration. (NOTE: zero vs random KV alone does NOT reproduce
    // the real prefill — a single layer's 16MB KV fits in L2 either way. Use
    // --flush-l2 for that.)
    if (a == "--warm-kv") warm_kv = true;
    // --flush-l2: write a 128MB scratch buffer before every attention call to
    // evict the KV cache from L2. A single layer's KV (16MB at max_len=8192)
    // fits in the 32MB L2, so a standalone bench always reads KV from L2
    // (~10x faster than real). In the real model, 47 other layers run between
    // attention calls and their KV+activations+PLE data evict the current
    // layer's KV -> HBM scattered reads. --flush-l2 reproduces that by
    // forcing an L2 eviction before each call. THIS is what makes the bench
    // match the real prefill's HBM-bound regime.
    if (a == "--flush-l2") flush_l2 = true;
    // --base-pos N: the T tokens sit at ABSOLUTE positions [N, N+T) instead
    // of [0, T). Models a prefill chunk at the tail of a long sequence:
    // the indexer sees (N+T)/compress visible groups (history included),
    // so with --warm-idx the top-k scatters across the whole KV pool.
    // Requires N + max(ts) <= max_len.
    if (a == "--base-pos" && i + 1 < argc) base_pos = std::atoi(argv[++i]);
  }
  if (!FileExists(kIndex)) {
    std::fprintf(stderr, "model index not found\n");
    return 1;
  }

  WeightIndex* index = nullptr;
  Status s = WeightIndex::Open(kIndex, &index);
  if (!s.ok()) {
    std::fprintf(stderr, "index open failed: %s\n", s.message().c_str());
    return 1;
  }
  WeightLoader* loader = nullptr;
  s = WeightLoader::Create(kModelDir, *index, 8, &loader);
  if (!s.ok()) {
    std::fprintf(stderr, "loader create failed: %s\n", s.message().c_str());
    return 1;
  }

  FullAttentionWeights w;
  s = LoadFullAttention(*loader, kPrefix, kHs, kNq, kNkv, kHd, kRotD, kTheta,
                        kEps, kIdxN, kIdxKv, kIdxHd, kIdxBudget, kIdxCompress,
                        &w, nullptr);
  if (!s.ok()) {
    std::fprintf(stderr, "load failed: %s\n", s.message().c_str());
    return 1;
  }
  w.max_len = max_len;

  // Persistent caches (single sequence, identity page table).
  const int n_pages = (max_len + kKvPageSize - 1) / kKvPageSize;
  const size_t kv_bytes =
      static_cast<size_t>(n_pages) * kKvPageSize * kNkv * 2 * kHd * 2;
  uint16_t* d_kv = nullptr;
  int* d_pt = nullptr;
  uint16_t* d_idx_raw = nullptr;
  uint16_t* d_idx_comp = nullptr;
  int* d_rope = nullptr;
  int* d_pos = nullptr;
  uint16_t* d_x = nullptr;
  uint16_t* d_out = nullptr;
  void* d_l2_flush = nullptr;
  const size_t kFlushBytes = 128ull << 20;
  if (flush_l2 &&
      cudaMalloc(reinterpret_cast<void**>(&d_l2_flush), kFlushBytes) !=
          cudaSuccess) {
    std::fprintf(stderr, "cudaMalloc flush scratch failed\n");
    return 1;
  }
  if (cudaMalloc(reinterpret_cast<void**>(&d_kv), kv_bytes) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_pt),
                 static_cast<size_t>(max_len) * sizeof(int)) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_idx_raw),
                 static_cast<size_t>(max_len) * kIdxHd * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_idx_comp),
                 static_cast<size_t>(max_len) * kIdxHd * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_rope),
                 static_cast<size_t>(3) * max_len * sizeof(int)) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_pos),
                 static_cast<size_t>(max_len) * sizeof(int)) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_x),
                 static_cast<size_t>(max_len) * kHs * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_out),
                 static_cast<size_t>(max_len) * kHs * 2) != cudaSuccess) {
    std::fprintf(stderr, "cudaMalloc failed\n");
    return 1;
  }
  if (warm_kv) {
    // Random KV (does not compress in L2) — the production HBM-bound pattern.
    std::vector<uint16_t> kv_warm = MakeInput(kv_bytes / 2);
    cudaMemcpy(d_kv, kv_warm.data(), kv_bytes, cudaMemcpyHostToDevice);
  } else {
    cudaMemset(d_kv, 0, kv_bytes);
  }
  cudaMemset(d_idx_raw, 0, static_cast<size_t>(max_len) * kIdxHd * 2);
  cudaMemset(d_idx_comp, 0, static_cast<size_t>(max_len) * kIdxHd * 2);
  if (warm_idx) {
    const size_t idx_elems = static_cast<size_t>(max_len) * kIdxHd;
    std::vector<uint16_t> idx_warm = MakeInput(idx_elems);
    cudaMemcpy(d_idx_raw, idx_warm.data(), idx_elems * 2,
               cudaMemcpyHostToDevice);
    cudaMemcpy(d_idx_comp, idx_warm.data(), idx_elems * 2,
               cudaMemcpyHostToDevice);
  }
  // Identity page table + rope (pure text: all three rows = position).
  {
    std::vector<int> pt(max_len), rope(3 * max_len), pos(max_len);
    for (int p = 0; p < max_len; ++p) {
      pt[p] = p / kKvPageSize;
      rope[p] = rope[max_len + p] = rope[2 * max_len + p] = p;
      pos[p] = base_pos + p;
    }
    cudaMemcpy(d_pt, pt.data(), pt.size() * sizeof(int), cudaMemcpyHostToDevice);
    cudaMemcpy(d_rope, rope.data(), rope.size() * sizeof(int),
               cudaMemcpyHostToDevice);
    cudaMemcpy(d_pos, pos.data(), pos.size() * sizeof(int),
               cudaMemcpyHostToDevice);
  }
  const std::vector<uint16_t> x_host =
      MakeInput(static_cast<size_t>(max_len) * kHs);
  cudaMemcpy(d_x, x_host.data(), x_host.size() * 2, cudaMemcpyHostToDevice);

  std::vector<int> ts;
  {
    std::string tok;
    for (char c : ts_arg) {
      if (c == ',') {
        ts.push_back(std::atoi(tok.c_str()));
        tok.clear();
      } else tok.push_back(c);
    }
    if (!tok.empty()) ts.push_back(std::atoi(tok.c_str()));
  }

  std::printf("FullAttentionForward bench (layer 3, nq=%d nkv=%d hd=%d, "
              "max_len=%d, iters=%d)\n",
              kNq, kNkv, kHd, max_len, iters);
  std::printf("%-8s %-10s %-12s %-10s\n", "T", "regime", "ms/iter", "tok/ms");
  for (int T : ts) {
    if (T > max_len) {
      std::fprintf(stderr, "T=%d exceeds max_len %d\n", T, max_len);
      continue;
    }
    const size_t ws_bytes = FullAttentionWorkspaceBytes(w, T);
    void* d_ws = nullptr;
    if (cudaMalloc(&d_ws, ws_bytes) != cudaSuccess) {
      std::fprintf(stderr, "workspace alloc failed for T=%d\n", T);
      continue;
    }
    const char* regime = (T <= kIdxBudget) ? "dense" : "sparse";
    // Reset the persistent caches at the START OF EVERY iteration: the bench
    // reuses one pooled cache across iterations, and without a reset each
    // iteration would attend to the previous one's K/V (stale state ->
    // nondeterministic output, wrong timing). Each iteration then models one
    // fresh prefill of T tokens from an empty cache.
    const double ms = TimeMs(
        iters, [&] {
          if (flush_l2) {
            // Evict the KV cache from L2 (write 128MB of scratch data).
            cudaMemset(d_l2_flush, 0xAB, kFlushBytes);
          }
          if (!warm_kv) cudaMemset(d_kv, 0, kv_bytes);
          if (!warm_idx) {
            cudaMemset(d_idx_raw, 0,
                       static_cast<size_t>(max_len) * kIdxHd * 2);
            cudaMemset(d_idx_comp, 0,
                       static_cast<size_t>(max_len) * kIdxHd * 2);
          }
          FullAttentionForward(w, d_x, d_out, d_pos, d_rope, d_kv, d_pt,
                               d_idx_raw, d_idx_comp, T, d_ws, ws_bytes,
                               nullptr, nullptr);
        });
    std::printf("%-8d %-10s %-12.3f %-10.1f\n", T, regime, ms, T / ms);
    if (dump_topk) {
      // topk_len lives at a fixed offset in the workspace (mirror the carve
      // in FullAttentionWorkspaceBytes; T > kOnePassT so no one-pass buffers).
      const int nq = kNq, nkv = kNkv, hd = kHd;
      const int idx_hd = 128, n_iq = kIdxN, n_ik = kIdxKv;
      const int max_blocks = 2048, max_topk = 2052, block_topk = 512;
      size_t off = 0;
      auto al = [&](size_t b) { off += (b + 255) & ~size_t(255); };
      al(size_t(T) * (nq * 2 * hd) * 2);
      al(size_t(T) * (nkv * hd) * 2);
      al(size_t(T) * (nkv * hd) * 2);
      al(size_t(T) * nq * hd * 2);
      al(size_t(T) * nq * hd * 2);
      al(size_t(T) * n_iq * idx_hd * 2);
      al(size_t(T) * n_ik * idx_hd * 2);
      al(size_t(idx_hd) * 2);
      al(size_t(T) * max_blocks * 4);
      al(size_t(T) * max_topk * 4);
      std::vector<int> tl(T);
      cudaMemcpy(tl.data(), static_cast<char*>(d_ws) + off,
                 size_t(T) * 4, cudaMemcpyDeviceToHost);
      int mn = 1 << 30, mx = 0;
      long sum = 0;
      for (int v : tl) { mn = std::min(mn, v); mx = std::max(mx, v); sum += v; }
      std::printf("  topk_len: min=%d max=%d mean=%.1f (T=%d, idx_budget=2048)\n",
                  mn, mx, double(sum) / T, T);
    }
    // Diagnostics (opt-in via --diag): in-process determinism (two fresh
    // prefills in the same process) and per-shape GEMM determinism. NOTE: a
    // fresh isolated FullAttentionForward on a cold cublasLt cache can be
    // nondeterministic (first-call algo selection); this does NOT indicate a
    // production bug — the serve path (warm cache, sequential forward) is
    // verified bit-identical across processes (see docs/LOG.md 2026-07-18).
    if (diag) {
      auto fresh_run = [&] {
        cudaMemset(d_kv, 0, kv_bytes);
        cudaMemset(d_idx_raw, 0, static_cast<size_t>(max_len) * kIdxHd * 2);
        cudaMemset(d_idx_comp, 0, static_cast<size_t>(max_len) * kIdxHd * 2);
        cudaMemset(d_ws, 0, ws_bytes);  // isolate stale GEMM workspace
        FullAttentionForward(w, d_x, d_out, d_pos, d_rope, d_kv, d_pt,
                             d_idx_raw, d_idx_comp, T, d_ws, ws_bytes,
                             nullptr, nullptr);
        cudaDeviceSynchronize();  // fully drain before reading d_out
        std::vector<uint16_t> h(static_cast<size_t>(T) * kHs);
        cudaMemcpy(h.data(), d_out, h.size() * 2, cudaMemcpyDeviceToHost);
        cudaDeviceSynchronize();
        return h;
      };
      const std::vector<uint16_t> r1 = fresh_run();
      const std::vector<uint16_t> r2 = fresh_run();
      std::printf("  in-proc determinism: l2_rel=%.8f %s\n", L2Rel(r1, r2),
                  (L2Rel(r1, r2) < 1e-6f ? "[PASS]" : "[FAIL]"));
    }
    // GEMM determinism probe (opt-in via --diag): two identical Bf16Gemm calls
    // in the same process (same cached plan, same workspace) must be
    // bit-identical. Tests every projection shape used by FullAttentionForward;
    // if any FAILs, the nondeterminism is in cuBLASLt for that shape, not the
    // attention kernels.
    if (diag) {
      const size_t gemm_ws = 32u * 1024u * 1024u;
      const int nqg = kNq * 2 * kHd;   // 12288
      const int nkv = kNkv * kHd;      // 512
      const int niq = kIdxN * kIdxHd;  // 512
      const int nik = kIdxKv * kIdxHd; // 128
      const int no = kHs;              // 2560, K = nq*hd = 6144
      struct Shape { const char* name; const uint16_t* w; int N, K; };
      const Shape shapes[] = {
          {"q_proj", w.q_proj, nqg, kHs},
          {"k_proj", w.k_proj, nkv, kHs},
          {"v_proj", w.v_proj, nkv, kHs},
          {"iq_proj", w.index_qk_proj, niq, kHs},
          {"ik_proj", w.index_qk_proj + static_cast<size_t>(niq) * kHs, nik, kHs},
          {"o_proj", w.o_proj, no, kNq * kHd},
      };
      // o_proj's input is d_attn [T, nq*hd] (K=6144), NOT d_x [T, hs]; give it
      // a dedicated input buffer (the K=2560 shapes use d_x, whose row stride
      // matches).
      uint16_t* d_xo = nullptr;
      cudaMalloc(reinterpret_cast<void**>(&d_xo),
                 static_cast<size_t>(T) * kNq * kHd * 2);
      uint16_t* g1 = nullptr;
      uint16_t* g2 = nullptr;
      void* gws = nullptr;
      const size_t max_elems = static_cast<size_t>(T) * nqg;
      if (cudaMalloc(reinterpret_cast<void**>(&g1), max_elems * 2) == cudaSuccess &&
          cudaMalloc(reinterpret_cast<void**>(&g2), max_elems * 2) == cudaSuccess &&
          cudaMalloc(&gws, gemm_ws) == cudaSuccess) {
        for (const auto& sh : shapes) {
          const uint16_t* xin = (sh.K == kNq * kHd) ? d_xo : d_x;
          q4t::model::Bf16Gemm(xin, sh.w, g1, T, sh.N, sh.K, 1.f, 0.f, gws,
                               gemm_ws, nullptr);
          q4t::model::Bf16Gemm(xin, sh.w, g2, T, sh.N, sh.K, 1.f, 0.f, gws,
                               gemm_ws, nullptr);
          std::vector<uint16_t> a(static_cast<size_t>(T) * sh.N),
              b(static_cast<size_t>(T) * sh.N);
          cudaMemcpy(a.data(), g1, a.size() * 2, cudaMemcpyDeviceToHost);
          cudaMemcpy(b.data(), g2, b.size() * 2, cudaMemcpyDeviceToHost);
          const float l2 = L2Rel(a, b);
          std::printf("  gemm %-8s (N=%d K=%d): l2_rel=%.8f %s\n", sh.name,
                      sh.N, sh.K, l2, (l2 < 1e-6f ? "[PASS]" : "[FAIL]"));
        }
      }
      cudaFree(g1); cudaFree(g2); cudaFree(gws); cudaFree(d_xo);
    }
    if (!golden_path.empty()) {
      std::vector<uint16_t> host(static_cast<size_t>(T) * kHs);
      cudaMemcpy(host.data(), d_out, host.size() * 2, cudaMemcpyDeviceToHost);
      FILE* f = std::fopen(golden_path.c_str(), "wb");
      if (!f) std::fprintf(stderr, "golden open failed: %s\n", golden_path.c_str());
      else {
        std::fwrite(host.data(), 2, host.size(), f);
        std::fclose(f);
        std::printf("  golden dumped: %s (%zu BF16)\n", golden_path.c_str(),
                    host.size());
      }
    }
    if (!check_path.empty()) {
      FILE* f = std::fopen(check_path.c_str(), "rb");
      if (!f) {
        std::fprintf(stderr, "check: cannot open %s\n", check_path.c_str());
      } else {
        std::vector<uint16_t> gold(static_cast<size_t>(T) * kHs);
        const size_t got =
            std::fread(gold.data(), 2, gold.size(), f);
        std::fclose(f);
        if (got != gold.size()) {
          std::fprintf(stderr, "  check: size mismatch (got %zu)\n", got);
        } else {
          std::vector<uint16_t> cur(static_cast<size_t>(T) * kHs);
          cudaMemcpy(cur.data(), d_out, cur.size() * 2,
                     cudaMemcpyDeviceToHost);
          const float l2 = L2Rel(cur, gold);
          std::printf("  check vs %s: l2_rel=%.6f %s\n", check_path.c_str(),
                      l2, (l2 < 1e-6f ? "[PASS]" : "[FAIL]"));
        }
      }
    }
    cudaFree(d_ws);
  }

  w.Free();
  cudaFree(d_kv); cudaFree(d_pt); cudaFree(d_idx_raw); cudaFree(d_idx_comp);
  if (d_l2_flush) cudaFree(d_l2_flush);
  cudaFree(d_rope); cudaFree(d_pos); cudaFree(d_x); cudaFree(d_out);
  delete loader;
  delete index;
  return 0;
}
