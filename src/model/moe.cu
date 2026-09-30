// Complete MoE MLP implementation. See moe.h.
//
// Pipeline (all on `stream`):
//   1. router logits = x @ gate^T                [T, E]  (Bf16Gemm)
//   2. top-k + softmax-over-topk                 (kernel) -> expert_ids, router_w
//   3. routed = MoERoutedForward(x, ids, w)      [T, hs] f32 (quant/moe_gemm)
//   4. shared_gu = x @ shared_gu^T               [T, 2*shared_is] (Bf16Gemm)
//   5. shared_swiglu = silu(g)*u                 [T, shared_is] (kernel)
//   6. shared_down = shared_swiglu @ shared_down^T [T, hs] (Bf16Gemm)
//   7. y = routed + sigmoid(x @ gate_scalar) * shared_down  (kernel)
#include "q4t/model/moe.h"
#include "q4t/trace/router_collector.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <numeric>
#include <string>
#include <vector>

#include "q4t/model/linear.h"
#include "q4t/quant/moe_decode.h"
#include "q4t/quant/moe_gemm.h"
#include "q4t/quant/moe_residency.h"

namespace q4t {
namespace model {

namespace {

constexpr int kBlock = 256;

__device__ __forceinline__ float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}

// Slot-mode sub-chunk prefill helpers (see MoEForward): move a sub-chunk's
// token rows in/out of the full-token buffers. One thread per element.
__global__ void GatherTokenRowsKernel(const uint16_t* __restrict__ x,
                                      const int32_t* __restrict__ token_list,
                                      int rows, int hs,
                                      uint16_t* __restrict__ out) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  const int total = rows * hs;
  if (idx >= total) return;
  const int r = idx / hs;
  const int c = idx % hs;
  out[static_cast<size_t>(r) * hs + c] =
      x[static_cast<size_t>(token_list[r]) * hs + c];
}

__global__ void ScatterTokenRowsKernel(const float* __restrict__ in,
                                       const int32_t* __restrict__ token_list,
                                       int rows, int hs, float* __restrict__ y) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  const int total = rows * hs;
  if (idx >= total) return;
  const int r = idx / hs;
  const int c = idx % hs;
  y[static_cast<size_t>(token_list[r]) * hs + c] =
      in[static_cast<size_t>(r) * hs + c];
}
__device__ __forceinline__ uint16_t FloatToBf16(float f) {
  const __nv_bfloat16 b = __float2bfloat16_rn(f);
  return *reinterpret_cast<const uint16_t*>(&b);
}

// Top-k selection + softmax over the selected k logits. One block per token.
//
// Parallel top-k (was: thread 0 sequential scan, ~113us for E=512 k=10 — the
// 512×k dependent min-find comparisons form a serial dependency chain that
// no amount of memory parallelism can hide). The E logits are loaded into
// shared memory by all 256 threads, then k rounds of parallel max-reduction
// (warp shuffle + cross-warp shared) pick the top-k values in descending
// order. Each round masks the selected expert (set to -1e30f) so it is not
// re-selected. The (value desc, expert_id asc) tie-break makes the selection
// deterministic.
//
// Output slot order is value-descending (vs the old insertion order), so the
// downstream ScatterAdd atomicAdd accumulation order differs. This only
// changes floating-point rounding order of the same (expert, weight) sum —
// well within the test's l2_rel tolerance.
__global__ void RouterTopkKernel(const uint16_t* __restrict__ logits,
                                 int32_t* __restrict__ expert_ids,
                                 float* __restrict__ router_w, int T, int E,
                                 int k) {
  const int t = blockIdx.x;
  if (t >= T) return;
  const uint16_t* row = logits + static_cast<size_t>(t) * E;
  __shared__ float s_vals[512];
  __shared__ float s_wv[8];
  __shared__ int s_wi[8];
  __shared__ float s_sel_v[16];
  __shared__ int s_sel_i[16];
  constexpr int kWarp = 32;
  const int warp = threadIdx.x / kWarp;
  const int lane = threadIdx.x % kWarp;
  const int nwarps = kBlock / kWarp;  // 8

  for (int e = threadIdx.x; e < E; e += kBlock)
    s_vals[e] = Bf16ToFloat(row[e]);
  __syncthreads();

  for (int i = 0; i < k; ++i) {
    // Strided scan: each thread finds max over its strided subset.
    float m = -1e30f;
    int mid = 0;
    for (int e = threadIdx.x; e < E; e += kBlock) {
      const float v = s_vals[e];
      if (v > m || (v == m && e < mid)) {
        m = v;
        mid = e;
      }
    }
    // Warp reduce (max by value, tie-break by id asc).
#pragma unroll
    for (int off = 16; off > 0; off >>= 1) {
      const float om = __shfl_xor_sync(0xffffffffu, m, off);
      const int oid = __shfl_xor_sync(0xffffffffu, mid, off);
      if (om > m || (om == m && oid < mid)) {
        m = om;
        mid = oid;
      }
    }
    if (lane == 0) {
      s_wv[warp] = m;
      s_wi[warp] = mid;
    }
    __syncthreads();
    if (threadIdx.x == 0) {
      float bv = s_wv[0];
      int bi = s_wi[0];
      for (int w = 1; w < nwarps; ++w) {
        if (s_wv[w] > bv || (s_wv[w] == bv && s_wi[w] < bi)) {
          bv = s_wv[w];
          bi = s_wi[w];
        }
      }
      s_sel_v[i] = bv;
      s_sel_i[i] = bi;
      s_vals[bi] = -1e30f;  // mask so it is not re-selected
    }
    __syncthreads();
  }
  // Thread 0: softmax over selected k (from shared memory).
  if (threadIdx.x != 0) return;
  float max_val = s_sel_v[0];
  for (int i = 1; i < k; ++i) max_val = fmaxf(max_val, s_sel_v[i]);
  float sum = 0.0f;
  for (int i = 0; i < k; ++i) {
    s_sel_v[i] = __expf(s_sel_v[i] - max_val);
    sum += s_sel_v[i];
  }
  const float inv = 1.0f / sum;
  int32_t* out_ids = expert_ids + static_cast<size_t>(t) * k;
  float* out_w = router_w + static_cast<size_t>(t) * k;
  for (int i = 0; i < k; ++i) {
    out_ids[i] = s_sel_i[i];
    out_w[i] = s_sel_v[i] * inv;
  }
}

// Debug: when Q4T_MOE_DUMP=<tag>, copy this MoE block's router top-k expert
// ids [T, k] int32 and router weights [T, k] f32 to
// <tag>_m<idx>.{eid,rw}.bin so a prefill-vs-decode (or C++-vs-reference)
// comparison can check whether the top-k expert SELECTION flips (a single
// flipped expert in a 512-expert MoE changes the output far more than the
// GEMM rounding noise). Each forward calls MoEForward once per layer, in
// layer order, so idx (0-based, reset when the tag changes) is the layer.
void DumpRouterTopk(const int32_t* eid, const float* rw, int T, int k) {
  const char* e = std::getenv("Q4T_MOE_DUMP");
  if (!e || !*e) return;
  static std::string last_tag;
  static int idx = 0;
  if (std::string(e) != last_tag) {
    last_tag = e;
    idx = 0;
  }
  const int li = idx++;
  std::string base = std::string(e) + "_m" + std::to_string(li);
  std::vector<int32_t> h_eid(static_cast<size_t>(T) * k);
  cudaMemcpy(h_eid.data(), eid, h_eid.size() * sizeof(int32_t),
             cudaMemcpyDeviceToHost);
  FILE* f = std::fopen((base + ".eid.bin").c_str(), "wb");
  if (f) {
    std::fwrite(h_eid.data(), sizeof(int32_t), h_eid.size(), f);
    std::fclose(f);
  }
  std::vector<float> h_rw(static_cast<size_t>(T) * k);
  cudaMemcpy(h_rw.data(), rw, h_rw.size() * sizeof(float),
             cudaMemcpyDeviceToHost);
  f = std::fopen((base + ".rw.bin").c_str(), "wb");
  if (f) {
    std::fwrite(h_rw.data(), sizeof(float), h_rw.size(), f);
    std::fclose(f);
  }
}

// shared_swiglu[t, c] = silu(gu[t, c]) * gu[t, shared_is + c].
__global__ void SwiGLUKernel(const uint16_t* __restrict__ gu,
                             uint16_t* __restrict__ out, int T, int shared_is) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * shared_is) return;
  const int t = idx / shared_is;
  const int c = idx % shared_is;
  const uint16_t* row = gu + static_cast<size_t>(t) * (2 * shared_is);
  const float g = Bf16ToFloat(row[c]);
  const float u = Bf16ToFloat(row[shared_is + c]);
  const float sig = 1.0f / (1.0f + __expf(-g));
  out[idx] = FloatToBf16((g * sig) * u);
}

// y[t, c] = routed[t, c] + sigmoid(dot(x[t], gate_scalar)) * shared_down[t, c].
// One block per token; the gate dot is reduced across the block.
__global__ void MoECombineKernel(const float* __restrict__ routed,
                                 const uint16_t* __restrict__ shared_down,
                                 const uint16_t* __restrict__ x,
                                 const uint16_t* __restrict__ gate_scalar,
                                 uint16_t* __restrict__ y, int T, int hs) {
  const int t = blockIdx.x;
  if (t >= T) return;
  __shared__ float s_part[kBlock];
  __shared__ float s_gate;
  const uint16_t* xrow = x + static_cast<size_t>(t) * hs;
  const uint16_t* grow = gate_scalar;  // [1, hs]
  float acc = 0.0f;
  for (int i = threadIdx.x; i < hs; i += blockDim.x)
    acc += Bf16ToFloat(xrow[i]) * Bf16ToFloat(grow[i]);
  s_part[threadIdx.x] = acc;
  __syncthreads();
  for (int off = blockDim.x / 2; off > 0; off >>= 1) {
    if (threadIdx.x < off) s_part[threadIdx.x] += s_part[threadIdx.x + off];
    __syncthreads();
  }
  if (threadIdx.x == 0) s_gate = 1.0f / (1.0f + __expf(-s_part[0]));
  __syncthreads();
  const float gate = s_gate;
  const float* rrow = routed + static_cast<size_t>(t) * hs;
  const uint16_t* sdrow = shared_down + static_cast<size_t>(t) * hs;
  uint16_t* yrow = y + static_cast<size_t>(t) * hs;
  for (int c = threadIdx.x; c < hs; c += blockDim.x) {
    const float v = rrow[c] + gate * Bf16ToFloat(sdrow[c]);
    yrow[c] = FloatToBf16(v);
  }
}

Status CheckGemm(const Bf16GemmResult& r) {
  if (r.status != CUBLAS_STATUS_SUCCESS || !r.has_algo) {
    return Status::Fail(std::string("Bf16Gemm failed (status=") +
                        std::to_string((int)r.status) + ")");
  }
  return Status();
}

}  // namespace

void MoEExtraWeights::Free() {
  if (gate) cudaFree(gate);
  if (shared_gu) cudaFree(shared_gu);
  if (shared_down) cudaFree(shared_down);
  if (shared_gate_scalar) cudaFree(shared_gate_scalar);
  shared_gu_fp8.Free();
  shared_down_fp8.Free();
  gate = shared_gu = shared_down = shared_gate_scalar = nullptr;
}

Status LoadMoEExtra(const io::WeightLoader& loader, const std::string& prefix,
                    int E, int hs, int shared_is, MoEExtraWeights* out,
                    cudaStream_t stream) {
  if (E <= 0 || hs <= 0 || shared_is <= 0) return Status::Fail("bad dims");
  out->E = E;
  out->hs = hs;
  out->shared_is = shared_is;

  auto alloc = [](uint16_t** p, size_t bytes) -> Status {
    if (cudaMalloc(reinterpret_cast<void**>(p), bytes) != cudaSuccess)
      return Status::Fail("cudaMalloc failed");
    return Status();
  };
  auto load = [&loader, stream](const std::string& name, uint16_t* dst,
                                size_t bytes) -> Status {
    std::vector<uint16_t> host(bytes / sizeof(uint16_t));
    Status s = loader.ReadTensor(name, host.data());
    if (!s.ok()) return s;
    if (cudaMemcpyAsync(dst, host.data(), bytes, cudaMemcpyHostToDevice,
                        stream) != cudaSuccess)
      return Status::Fail("H2D failed");
    return Status();
  };

  Status s;
  if (!(s = alloc(&out->gate, static_cast<size_t>(E) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->shared_gu,
                  static_cast<size_t>(2 * shared_is) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->shared_down,
                  static_cast<size_t>(hs) * shared_is * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->shared_gate_scalar, hs * sizeof(uint16_t)))) return s;

  if (!(s = load(prefix + ".gate.weight", out->gate,
                 static_cast<size_t>(E) * hs * sizeof(uint16_t))))
    return s;
  // shared_gu: gate rows first, then up rows.
  if (!(s = load(prefix + ".shared_expert.gate_proj.weight", out->shared_gu,
                 static_cast<size_t>(shared_is) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".shared_expert.up_proj.weight",
                 out->shared_gu + static_cast<size_t>(shared_is) * hs,
                 static_cast<size_t>(shared_is) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".shared_expert.down_proj.weight", out->shared_down,
                 static_cast<size_t>(hs) * shared_is * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".shared_expert_gate.weight", out->shared_gate_scalar,
                 hs * sizeof(uint16_t))))
    return s;
  // FP8 (e4m3) decode shadows for the shared-expert projections (gated by
  // Q4T_FP8_PROJ). The router `gate` stays BF16 (exact top-k routing).
  if (!BuildFp8Shadow(out->shared_gu, 2 * shared_is, hs, &out->shared_gu_fp8,
                      Fp8Part::kMoeShared, stream))
    return Status::Fail("shared_gu FP8 shadow");
  if (!BuildFp8Shadow(out->shared_down, hs, shared_is, &out->shared_down_fp8,
                      Fp8Part::kMoeShared, stream))
    return Status::Fail("shared_down FP8 shadow");
  if (stream != nullptr && cudaStreamSynchronize(stream) != cudaSuccess)
    return Status::Fail("stream sync failed");
  return Status();
}

size_t MoEForwardWorkspaceBytes(int T, int k, int hs, int moe_is, int shared_is,
                                int E) {
  const size_t routed = quant::MoEWorkspace::RequiredBytes(T, k, hs, moe_is);
  size_t scratch = 0;
  scratch += static_cast<size_t>(T) * E * sizeof(uint16_t);  // router logits
  scratch += static_cast<size_t>(T) * k * sizeof(int32_t);  // expert_ids
  scratch += static_cast<size_t>(T) * k * sizeof(float);  // router_w
  scratch += static_cast<size_t>(T) * hs * sizeof(float);  // routed y (f32)
  scratch += static_cast<size_t>(T) * (2 * shared_is) * sizeof(uint16_t);  // gu
  scratch += static_cast<size_t>(T) * shared_is * sizeof(uint16_t);  // swiglu
  scratch += static_cast<size_t>(T) * hs * sizeof(uint16_t);  // shared_down
  // 8-byte alignment padding. NOTE: parentheses are required — `+` binds
  // tighter than `&`, so without them this miscomputes to a tiny value.
  return ((routed + 7) & ~size_t(7)) + ((scratch + 7) & ~size_t(7));
}

Status MoEForward(const uint16_t* x, const quant::MoEWeightLayout& routed,
                  const MoEExtraWeights& extra, uint16_t* y, int T, int k,
                  void* workspace, size_t workspace_bytes, void* gemm_ws,
                  size_t gemm_ws_bytes, cudaStream_t stream,
                  trace::RouterCollector* trace, int layer_id,
                  const quant::MoEResidency* residency) {
  const int hs = extra.hs;
  const int E = extra.E;
  const int shared_is = extra.shared_is;
  if (T <= 0) return Status();
  if (k <= 0 || k > 16) return Status::Fail("k out of range");
  if (workspace_bytes < MoEForwardWorkspaceBytes(T, k, hs, routed.moe_is,
                                                 shared_is, E)) {
    return Status::Fail("workspace too small");
  }

  // Carve workspace: routed region first, then scratch.
  uint8_t* base = static_cast<uint8_t*>(workspace);
  const size_t routed_bytes = quant::MoEWorkspace::RequiredBytes(T, k, hs,
                                                                 routed.moe_is);
  uint8_t* routed_ws = base;
  uint8_t* scratch = base + ((routed_bytes + 7) & ~size_t(7));

  uint16_t* d_logits = reinterpret_cast<uint16_t*>(scratch);
  scratch += static_cast<size_t>(T) * E * sizeof(uint16_t);
  int32_t* d_eid = reinterpret_cast<int32_t*>(scratch);
  scratch += static_cast<size_t>(T) * k * sizeof(int32_t);
  float* d_rw = reinterpret_cast<float*>(scratch);
  scratch += static_cast<size_t>(T) * k * sizeof(float);
  float* d_routed = reinterpret_cast<float*>(scratch);
  scratch += static_cast<size_t>(T) * hs * sizeof(float);
  uint16_t* d_gu = reinterpret_cast<uint16_t*>(scratch);
  scratch += static_cast<size_t>(T) * (2 * shared_is) * sizeof(uint16_t);
  uint16_t* d_swiglu = reinterpret_cast<uint16_t*>(scratch);
  scratch += static_cast<size_t>(T) * shared_is * sizeof(uint16_t);
  uint16_t* d_shared_down = reinterpret_cast<uint16_t*>(scratch);
  scratch += static_cast<size_t>(T) * hs * sizeof(uint16_t);

  Status s;
  // 1. router logits = x @ gate^T  [T, hs] x [E, hs]^T -> [T, E].
  s = CheckGemm(Bf16Gemm(x, extra.gate, d_logits, T, E, hs, 1.0f, 0.0f,
                         gemm_ws, gemm_ws_bytes, stream));
  if (!s.ok()) return s;
  // 2. top-k + softmax.
  RouterTopkKernel<<<T, kBlock, 0, stream>>>(d_logits, d_eid, d_rw, T, E, k);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("topk launch");
  if (trace) {
    s = trace->CaptureLayer(layer_id, d_eid, T, k, stream);
    if (!s.ok()) return s;
  }
  DumpRouterTopk(d_eid, d_rw, T, k);
  // 2b/3. Routed experts (NVFP4). In slot mode the router's full E expert
  //     IDs must be mapped to the layer's C resident slots; missing experts
  //     load on demand, stream-ordered after every earlier GEMM that read the
  //     evicted slot and before any GEMM that reads the new one (see
  //     moe_residency.h). The trace above already captured the ORIGINAL ids.
  if (residency) {
    const int C = residency->Slots();
    if (C < k) return Status::Fail("residency slots below top-k");
    std::vector<int32_t> ids_h(static_cast<size_t>(T) * k);
    std::vector<float> rw_h(static_cast<size_t>(T) * k);
    const bool tim = residency->TimingEnabled();
    const auto t_d2h =
        tim ? std::chrono::steady_clock::now()
            : std::chrono::steady_clock::time_point{};
    if (cudaMemcpyAsync(ids_h.data(), d_eid,
                        ids_h.size() * sizeof(int32_t),
                        cudaMemcpyDeviceToHost, stream) != cudaSuccess)
      return Status::Fail("residency D2H expert ids");
    if (cudaMemcpyAsync(rw_h.data(), d_rw, rw_h.size() * sizeof(float),
                        cudaMemcpyDeviceToHost, stream) != cudaSuccess)
      return Status::Fail("residency D2H router weights");
    if (cudaStreamSynchronize(stream) != cudaSuccess)
      return Status::Fail("residency stream sync");
    if (tim) {
      residency->RecordD2HSync(static_cast<uint64_t>(
          std::chrono::duration_cast<std::chrono::nanoseconds>(
              std::chrono::steady_clock::now() - t_d2h)
              .count()));
    }

    for (int i = 0; i < static_cast<int>(ids_h.size()); ++i) {
      if (ids_h[i] < 0 || ids_h[i] >= E) {
        return Status::Fail("residency expert id out of range");
      }
    }

    // Cluster tokens by expert-set similarity before partitioning. The
    // grouped GEMM computes each token's full top-k sum in the router's
    // top-k order regardless of token order, and results scatter back to the
    // original token rows, so reordering tokens is numerically invisible.
    // Adjacent tokens then share expert sets, which keeps sub-chunk expert
    // sets overlapping and cuts evictions/reloads to near the distinct set.
    std::vector<int32_t> order(T);
    std::iota(order.begin(), order.end(), 0);
    std::vector<int32_t> keys(static_cast<size_t>(T) * k);
    for (int t = 0; t < T; ++t) {
      int32_t* key = keys.data() + static_cast<size_t>(t) * k;
      for (int j = 0; j < k; ++j) {
        key[j] = ids_h[static_cast<size_t>(t) * k + j];
      }
      std::sort(key, key + k);
    }
    std::sort(order.begin(), order.end(),
              [&](int a, int b) {
                const int32_t* ka = keys.data() + static_cast<size_t>(a) * k;
                const int32_t* kb = keys.data() + static_cast<size_t>(b) * k;
                return std::lexicographical_compare(ka, ka + k, kb, kb + k);
              });

    // Partition the (sorted) tokens into sub-chunks whose distinct
    // NON-protected expert set fits the dynamic capacity. Resolve keeps at
    // most that many experts loadable at once; protected hot slots are always
    // resident and never evicted, so they do not count. Each token belongs to
    // exactly one sub-chunk, so every token's routed sum is unchanged.
    const int D =
        residency->HotProtected() ? C - residency->ProtectedCount() : C;
    std::vector<std::vector<int32_t>> chunks;
    {
      std::vector<uint8_t> in_set(E, 0);
      std::vector<int32_t> cur;
      int distinct = 0;
      auto flush = [&]() {
        if (cur.empty()) return;
        int actual = 0;
        {
          std::vector<uint8_t> chk(E, 0);
          for (int tt : cur)
            for (int jj = 0; jj < k; ++jj) {
              const int e = ids_h[static_cast<size_t>(tt) * k + jj];
              if (!chk[e]) {
                chk[e] = 1;
                ++actual;
              }
            }
        }
        std::fprintf(stderr,
                     "[q4t][residency][diag] layer=%d flush size=%zu "
                     "book=%d actual=%d\n",
                     layer_id, cur.size(), distinct, actual);
        chunks.push_back(std::move(cur));
        cur.clear();
        std::fill(in_set.begin(), in_set.end(), 0);
        distinct = 0;
      };
      for (int oi = 0; oi < T; ++oi) {
        const int t = order[oi];
        std::vector<int> fresh;
        for (int j = 0; j < k; ++j) {
          const int e = ids_h[static_cast<size_t>(t) * k + j];
          if (!in_set[e]) {
            in_set[e] = 1;
            fresh.push_back(e);
          }
        }
        if (distinct + static_cast<int>(fresh.size()) > D) {
          for (int e : fresh) in_set[e] = 0;  // roll back the probe
          if (!cur.empty()) flush();
          // The flush emptied the bookkeeping: re-probe against the NEW
          // chunk, or experts shared with the previous chunk are missed and
          // the chunk's true distinct set can exceed D (observed: book=256
          // actual=257 at C=256, PlanResolve capacity failure).
          fresh.clear();
          for (int j = 0; j < k; ++j) {
            const int e = ids_h[static_cast<size_t>(t) * k + j];
            if (!in_set[e]) {
              in_set[e] = 1;
              fresh.push_back(e);
            }
          }
          distinct += static_cast<int>(fresh.size());
        } else {
          distinct += static_cast<int>(fresh.size());
        }
        cur.push_back(t);
        if (oi % 64 == 63) {
          int cnt = 0;
          std::vector<uint8_t> chk(E, 0);
          for (int tt : cur)
            for (int jj = 0; jj < k; ++jj) {
              const int e = ids_h[static_cast<size_t>(tt) * k + jj];
              if (!chk[e]) {
                chk[e] = 1;
                ++cnt;
              }
            }
          if (cnt != distinct || cnt > D) {
            std::fprintf(stderr,
                         "[q4t][residency][diag] layer=%d oi=%d t=%d "
                         "INVARIANT BROKEN book=%d actual=%d D=%d\n",
                         layer_id, oi, t, distinct, cnt, D);
          }
        }
      }
      if (!cur.empty()) flush();
    }

    // Multi-sub-chunk pipeline: per sub-chunk, commit its staged experts
    // (H2D stream-ordered after the previous sub-chunk's GEMM, so evicting a
    // slot is safe), gather its rows, run the grouped GEMM, scatter the
    // result back. While the GPU runs sub-chunk j's GEMM, the host stages
    // sub-chunk j+1's missing experts (NVMe read + swizzle) in parallel, so
    // the next commit finds them ready.
    const size_t xsub_b = static_cast<size_t>(T) * hs * sizeof(uint16_t);
    const size_t ysub_b = static_cast<size_t>(T) * hs * sizeof(float);
    const size_t idsub_b = static_cast<size_t>(T) * k * sizeof(int32_t);
    const size_t idx_b = static_cast<size_t>(T) * sizeof(int32_t);
    struct SubBuf {
      uint8_t* p = nullptr;
      cudaStream_t s;
      ~SubBuf() {
        if (p) cudaFreeAsync(p, s);
      }
    } buf{nullptr, stream};
    if (cudaMallocAsync(&buf.p, xsub_b + ysub_b + 2 * idsub_b + idx_b,
                        stream) != cudaSuccess)
      return Status::Fail("residency sub-chunk buffers");
    uint16_t* d_xsub = reinterpret_cast<uint16_t*>(buf.p);
    float* d_ysub = reinterpret_cast<float*>(buf.p + xsub_b);
    int32_t* d_idsub =
        reinterpret_cast<int32_t*>(buf.p + xsub_b + ysub_b);
    float* d_rwsub =
        reinterpret_cast<float*>(buf.p + xsub_b + ysub_b + idsub_b);
    int32_t* d_tokidx =
        reinterpret_cast<int32_t*>(buf.p + xsub_b + ysub_b + 2 * idsub_b);
    if (cudaMemsetAsync(d_routed, 0,
                        static_cast<size_t>(T) * hs * sizeof(float),
                        stream) != cudaSuccess)
      return Status::Fail("memset routed");

    struct Sub {
      const std::vector<int32_t>* toks = nullptr;
      std::vector<int32_t> slots;  // slot id per (token, top-k) entry
      quant::MoEResidency::LoadPlan plan;
    };
    std::vector<Sub> subs(chunks.size());
    for (size_t j = 0; j < chunks.size(); ++j) {
      subs[j].toks = &chunks[j];
      subs[j].slots.resize(chunks[j].size() * static_cast<size_t>(k));
    }
    auto plan_sub = [&](int j) -> Status {
      const auto& toks = *subs[j].toks;
      const int T_sub = static_cast<int>(toks.size());
      std::vector<int32_t> exp_sub(static_cast<size_t>(T_sub) * k);
      for (int i = 0; i < T_sub; ++i) {
        const size_t base = static_cast<size_t>(toks[i]) * k;
        for (int jj = 0; jj < k; ++jj) {
          exp_sub[static_cast<size_t>(i) * k + jj] = ids_h[base + jj];
        }
      }
      // T_sub == 1 is the decode step; report its lookups/misses in the
      // decode phase counters (acceptance split prefill vs decode loads).
      return residency->PlanResolve(exp_sub.data(), T_sub * k,
                                    subs[j].slots.data(), &subs[j].plan,
                                    T_sub == 1);
    };

    {
      int maxd = 0;
      for (const auto& c : chunks) {
        std::vector<uint8_t> seen(E, 0);
        int d = 0;
        for (int tt : c)
          for (int jj = 0; jj < k; ++jj) {
            const int e = ids_h[static_cast<size_t>(tt) * k + jj];
            if (!seen[e]) {
              seen[e] = 1;
              ++d;
            }
          }
        if (d > maxd) maxd = d;
      }
      std::fprintf(stderr,
                   "[q4t][residency][diag] layer=%d T=%d D=%d chunks=%zu "
                   "max_distinct=%d resident_before=%d\n",
                   layer_id, T, D, chunks.size(), maxd,
                   residency->ResidentCount());
    }
    Status ps = plan_sub(0);
    if (!ps.ok()) return ps;
    ps = residency->LoadPhase1(subs[0].plan);
    if (!ps.ok()) return ps;
    for (size_t j = 0; j < chunks.size(); ++j) {
      const auto& toks = *subs[j].toks;
      const int T_sub = static_cast<int>(toks.size());
      // Commit sub-chunk j's staged chunks (H2D after sub-chunk j-1's GEMM,
      // before sub-chunk j's GEMM). The stage-ahead below commits sub-chunk
      // j fully during the previous iteration, so this only does work for
      // j = 0.
      while (!subs[j].plan.fully_committed()) {
        s = residency->LoadPhase2(subs[j].plan, stream);
        if (!s.ok()) return s;
        ps = residency->LoadPhase1(subs[j].plan);
        if (!ps.ok()) return ps;
      }
      std::vector<float> rw_sub(static_cast<size_t>(T_sub) * k);
      for (int i = 0; i < T_sub; ++i) {
        const size_t base = static_cast<size_t>(toks[i]) * k;
        for (int jj = 0; jj < k; ++jj) {
          rw_sub[static_cast<size_t>(i) * k + jj] = rw_h[base + jj];
        }
      }
      if (cudaMemcpyAsync(d_tokidx, toks.data(),
                          static_cast<size_t>(T_sub) * sizeof(int32_t),
                          cudaMemcpyHostToDevice, stream) != cudaSuccess)
        return Status::Fail("residency H2D token idx");
      const int total = T_sub * hs;
      GatherTokenRowsKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0,
                              stream>>>(x, d_tokidx, T_sub, hs, d_xsub);
      if (cudaGetLastError() != cudaSuccess)
        return Status::Fail("residency gather launch");
      if (cudaMemcpyAsync(d_idsub, subs[j].slots.data(),
                          subs[j].slots.size() * sizeof(int32_t),
                          cudaMemcpyHostToDevice, stream) != cudaSuccess)
        return Status::Fail("residency H2D slot ids");
      if (cudaMemcpyAsync(d_rwsub, rw_sub.data(),
                          rw_sub.size() * sizeof(float),
                          cudaMemcpyHostToDevice, stream) != cudaSuccess)
        return Status::Fail("residency H2D router weights");
      if (cudaMemsetAsync(d_ysub, 0,
                          static_cast<size_t>(T_sub) * hs * sizeof(float),
                          stream) != cudaSuccess)
        return Status::Fail("residency memset sub-chunk out");
      // Slot-mode loads/evictions are stream-ordered on the caller's stream;
      // the GEMM must therefore run single-stream (Q4T_MOE_STREAMS=1,
      // enforced at model load) or its extra streams would read a slot
      // before its load H2D lands or after it is evicted.
      // Decode (T_sub == 1) uses the fixed-shape device decode path
      // directly. MoERoutedForward's dispatch for that path requires
      // E == 512, which the C-slot layout (E == C) does not satisfy, so
      // routing slot-mode decode through it would fall back to the
      // host-orchestrated grouped GEMM (measured ~4x slower per token).
      // The device decode kernel itself is generic in E (its per-expert
      // stride is a constant, not derived from weights.E), so it is safe
      // to call it directly with the C-slot layout and slot IDs. GEMM
      // (moe_gemm.cu) is frozen for this goal; the dispatch therefore
      // lives in the residency layer (see the GEMM-freeze scope in
      // docs/MOE_RESIDENCY_PLAN_2026-09-30.md).
      if (T_sub == 1 && k == 10 && routed.hs == 2560 &&
          routed.moe_is == 640) {
        quant::MoEWorkspace ws;
        const int R = k;  // M == 1
        ws.a_packed_bytes = static_cast<size_t>(R) * (routed.hs / 2);
        ws.a_sf_bytes = quant::SfBufferSize(R, routed.hs);
        ws.gu_out_bytes =
            static_cast<size_t>(R) * (2 * routed.moe_is) * sizeof(uint16_t);
        ws.dn_out_bytes = static_cast<size_t>(R) * routed.hs * sizeof(uint16_t);
        ws.Init(static_cast<uint8_t*>(routed_ws));
        s = quant::MoEDeviceDecode(d_xsub, d_idsub, d_rwsub, d_ysub, routed,
                                   ws, gemm_ws, gemm_ws_bytes, stream);
      } else {
        s = quant::MoERoutedForward(d_xsub, d_idsub, d_rwsub, d_ysub,
                                   routed, routed_ws, gemm_ws,
                                   gemm_ws_bytes, T_sub, k, stream);
      }
      if (!s.ok()) return s;
      ScatterTokenRowsKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0,
                               stream>>>(d_ysub, d_tokidx, T_sub, hs,
                                         d_routed);
      if (cudaGetLastError() != cudaSuccess)
        return Status::Fail("residency scatter launch");
      // Stage sub-chunk j+1 while the GPU runs sub-chunk j's GEMM: stage
      // chunk 0, then commit + stage the remaining chunks. Their H2Ds are
      // stream-ordered after sub-chunk j's GEMM, so any slot eviction is
      // safe for sub-chunk j.
      if (j + 1 < chunks.size()) {
        ps = plan_sub(j + 1);
        if (!ps.ok()) return ps;
        ps = residency->LoadPhase1(subs[j + 1].plan);
        if (!ps.ok()) return ps;
        while (!subs[j + 1].plan.fully_committed()) {
          s = residency->LoadPhase2(subs[j + 1].plan, stream);
          if (!s.ok()) return s;
          ps = residency->LoadPhase1(subs[j + 1].plan);
          if (!ps.ok()) return ps;
        }
      }
    }
  } else {
    // 3. routed experts (NVFP4).
    if (cudaMemsetAsync(d_routed, 0,
                        static_cast<size_t>(T) * hs * sizeof(float),
                        stream) != cudaSuccess)
      return Status::Fail("memset routed");
    s = quant::MoERoutedForward(x, d_eid, d_rw, d_routed, routed, routed_ws,
                                gemm_ws, gemm_ws_bytes, T, k, stream);
    if (!s.ok()) return s;
  }
  // 4. shared gate/up.
  s = CheckGemm(ProjGemm(x, extra.shared_gu, &extra.shared_gu_fp8, d_gu, T,
                         2 * shared_is, hs, 1.0f, 0.0f, gemm_ws, gemm_ws_bytes,
                         stream));
  if (!s.ok()) return s;
  // 5. SwiGLU.
  {
    const int total = T * shared_is;
    SwiGLUKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        d_gu, d_swiglu, T, shared_is);
  }
  // 6. shared down.
  s = CheckGemm(ProjGemm(d_swiglu, extra.shared_down, &extra.shared_down_fp8,
                         d_shared_down, T, hs, shared_is, 1.0f, 0.0f, gemm_ws,
                         gemm_ws_bytes, stream));
  if (!s.ok()) return s;
  // 7. combine.
  MoECombineKernel<<<T, kBlock, 0, stream>>>(d_routed, d_shared_down, x,
                                             extra.shared_gate_scalar, y, T,
                                             hs);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("combine launch");
  return Status();
}

}  // namespace model
}  // namespace q4t
