// Grouped NVFP4 MoE routed-expert forward (W4A4). See moe_gemm.h.
//
// Per expert e that has tokens:
//   1. Gather the expert's token rows from x into a compact [M_e, hs] buffer.
//   2. Quantize the compact buffer to NVFP4 (per-expert input_scale).
//   3. gate/up GEMM  -> [M_e, 2*moe_is]  (alpha = gu_w_scale2[e]*input_scale[e])
//   4. SwiGLU        -> [M_e, moe_is]
//   5. Quantize inter to NVFP4 (per-expert input_scale).
//   6. down GEMM     -> [M_e, hs]        (alpha = dn_w_scale2[e]*input_scale[e])
//   7. scatter-add router_w * down_out into y.
#include "q4t/quant/moe_gemm.h"

#include <cublasLt.h>
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <atomic>
#include <bit>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/quant/fp4_gemm.h"
#include "q4t/quant/moe_decode.h"
#include "q4t/quant/moe_gemm_test.h"
#include "q4t/quant/format.h"
#include "q4t/quant/swizzle.h"
#include "q4t/trace/mtp_cycle_timing.h"
#include "q4t/trace/mtp_verify_moe_timing.h"

namespace q4t {
namespace quant {

namespace {

constexpr int kBlock = 256;
constexpr size_t kBatchPackedBytes = 5120;
constexpr size_t kBatchSfBytes = 20480;
constexpr size_t kBatchSlotBytes = kBatchPackedBytes + kBatchSfBytes;
static_assert(kBatchSlotBytes * kMoEBatchSlots == MoEBatchGatherExtraBytes());

struct BatchStats {
  std::atomic<uint64_t> applied_calls{0};
  std::atomic<uint64_t> legacy_calls{0};
  std::atomic<uint64_t> bad_counts_fallback{0};
  std::atomic<uint64_t> batch_launches{0};
  std::atomic<uint64_t> legacy_gather_launches{0};
  std::atomic<uint64_t> replaced_gather_launches{0};
};
BatchStats g_batch_stats;

void Increment(std::atomic<uint64_t>* counter, uint64_t amount = 1) {
  counter->fetch_add(amount, std::memory_order_relaxed);
}

bool CaptureGemm(MoECapturedGemm* capture, int M, int N, int K,
                 size_t workspace_bytes, float alpha) {
  capture->key = {M, N, K, workspace_bytes, 16};
  capture->alpha_bits = std::bit_cast<uint32_t>(alpha);
  std::lock_guard<std::mutex> lock(LtCacheMutex());
  const auto& cache = Fp4PlanCache();
  const auto found = cache.find(capture->key);
  if (found == cache.end() || !found->second.has_algo) return false;
  std::memcpy(capture->algo.data(), &found->second.algo,
              sizeof(found->second.algo));
  capture->has_algo = true;
  return true;
}

bool CaptureCopy(MoERoutedCapture* capture, size_t offset, const void* source,
                 size_t bytes, cudaStream_t stream) {
  return cudaMemcpyAsync(capture->device_buffer + offset, source, bytes,
                          cudaMemcpyDeviceToDevice, stream) == cudaSuccess;
}

// Multi-stream MoE: the per-expert chains (gather -> gu GEMM -> swiglu ->
// dn GEMM) are independent, and each small GEMM (M_e ~ tens of tokens) only
// fills a fraction of the 20 SMs. Round-robin experts across a few CUDA
// streams so independent experts overlap and fill the SMs. dn_out is written
// at disjoint per-expert offsets, so only the reused a_packed/a_sf/gu_out and
// the cuBLASLt workspace need to be per-stream. Env Q4T_MOE_STREAMS (default
// 4; 1 = legacy single-stream, bit-identical).
constexpr int kMaxMoeStreams = 8;
cudaStream_t g_moe_streams[kMaxMoeStreams - 1];  // extras; stream 0 = caller's
cudaEvent_t g_moe_events[kMaxMoeStreams - 1];
bool g_moe_streams_init = false;

int MoeStreamCount() {
  const char* env = std::getenv("Q4T_MOE_STREAMS");
  int n = env ? std::atoi(env) : 4;
  if (n < 1) n = 1;
  if (n > kMaxMoeStreams) n = kMaxMoeStreams;
  return n;
}

bool EnsureMoeStreams() {
  if (g_moe_streams_init) return true;
  for (int i = 0; i < kMaxMoeStreams - 1; ++i) {
    if (cudaStreamCreateWithFlags(&g_moe_streams[i], cudaStreamNonBlocking) !=
            cudaSuccess ||
        cudaEventCreateWithFlags(&g_moe_events[i], cudaEventDisableTiming) !=
            cudaSuccess) {
      for (int j = 0; j <= i; ++j) {
        if (g_moe_events[j]) cudaEventDestroy(g_moe_events[j]);
        if (g_moe_streams[j]) cudaStreamDestroy(g_moe_streams[j]);
        g_moe_events[j] = nullptr;
        g_moe_streams[j] = nullptr;
      }
      return false;
    }
  }
  g_moe_streams_init = true;
  return true;
}

// Persistent per-stream scratch (a_packed+a_sf+gu_out carved from one buffer,
// plus a cuBLASLt workspace) so the expert loop does NOT cudaMalloc per call.
struct MoeScratch {
  void* buf = nullptr;
  void* gemm_ws = nullptr;
  size_t buf_bytes = 0;
  size_t gws_bytes = 0;
};
MoeScratch g_moe_scratch[kMaxMoeStreams - 1];  // extras (stream 0 uses ws)

size_t AlignUp256(size_t x) { return (x + 255) & ~static_cast<size_t>(255); }

bool EnsureMoeScratch(int idx, size_t need_buf, size_t need_gws) {
  MoeScratch& s = g_moe_scratch[idx];
  if (s.buf_bytes < need_buf) {
    if (s.buf) cudaFree(s.buf);
    if (cudaMalloc(&s.buf, need_buf) != cudaSuccess) {
      s.buf = nullptr;
      s.buf_bytes = 0;
      return false;
    }
    s.buf_bytes = need_buf;
  }
  if (s.gws_bytes < need_gws) {
    if (s.gemm_ws) cudaFree(s.gemm_ws);
    if (cudaMalloc(&s.gemm_ws, need_gws) != cudaSuccess) {
      s.gemm_ws = nullptr;
      s.gws_bytes = 0;
      return false;
    }
    s.gws_bytes = need_gws;
  }
  return true;
}

// Device copy of the SF swizzle offset (swizzle.h is host-only).
__device__ __forceinline__ float Bf16ToFloat(uint16_t b) {
  const __nv_bfloat16 h = *reinterpret_cast<const __nv_bfloat16*>(&b);
  return __bfloat162float(h);
}
__device__ __forceinline__ size_t SfOffsetDev(int row, int group,
                                              int num_g_tiles) {
  const int i = row % 32;
  const int j = (row % 128) / 32;
  const int ga = group % 4;
  const int within = i * 16 + j * 4 + ga;
  return static_cast<size_t>(within) +
         static_cast<size_t>(group / 4) * 512 +
         static_cast<size_t>(row / 128) * static_cast<size_t>(num_g_tiles) *
             512;
}

// Build per-expert token lists + counts. One thread per (token, slot).
//   expert_ids [M, k] int32, router_w [M, k] float (unused here).
//   token_list [E, M] int32 (stride = M: an expert can be selected by up to
//     M tokens, so the per-expert capacity must be M, not k).
//   expert_counts [E] int32.
__global__ void BuildTokenListsKernel(const int32_t* __restrict__ expert_ids,
                                      int M, int k, int E,
                                      int32_t* __restrict__ expert_counts,
                                      int32_t* __restrict__ token_list) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= M * k) return;
  const int e = expert_ids[idx];
  if (e < 0 || e >= E) return;  // defensive: RouterTopk yields [0, E)
  const int pos = atomicAdd(&expert_counts[e], 1);
  // Store the flat (token, slot) index so the scatter can recover both the
  // token (idx / k) and the router-weight slot (idx % k).
  // stride = M: an expert is selected by at most M distinct tokens, so pos < M
  // for well-formed routing. A degenerate top-k with duplicate experts can push
  // pos >= M; drop those writes so the [E, M] token_list never overflows into
  // the next expert's region (the count is clamped to M host-side to match).
  if (pos < M) token_list[e * M + pos] = idx;
}

// Gather + quantize one expert's tokens: one thread per (row, group of 16).
// Writes the expert's M_e rows to the START of the a_packed / a_sf buffers
// (rows 0..M_e-1) so the following per-expert GEMM can address them directly.
// token_list holds [E, stride] flat (token,slot) indices; expert e's tokens are
// token_list[e*stride + 0 .. M_e-1].
//   x [M, hs] bf16, token_list [E, stride] int32, input_scale (per expert),
//   a_packed [M_e, hs/2] u8 (out), a_sf swizzled e4m3 (out).
__global__ void GatherQuantKernel(
    const uint16_t* __restrict__ x, const int32_t* __restrict__ token_list,
    int M_e, int e, int k, int stride, int hs, int num_g_tiles,
    const float* __restrict__ input_scale,
    uint8_t* __restrict__ a_packed, uint8_t* __restrict__ a_sf) {
  const int groups = hs / 16;
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= M_e * groups) return;
  const int row = idx / groups;
  const int g = idx % groups;
  const int flat = token_list[e * stride + row];  // (token, slot) flat index
  const int t = flat / k;
  const float inv_scale = input_scale[e];

  const uint16_t* src = x + static_cast<size_t>(t) * hs + g * 16;
  float a[16];
  float gmax = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    const __nv_bfloat16 b = *reinterpret_cast<const __nv_bfloat16*>(&src[j]);
    a[j] = __bfloat162float(b);
    gmax = fmaxf(gmax, fabsf(a[j]));
  }
  const float block_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
  const uint8_t sf_code = FloatToE4m3(block_scale / inv_scale);
  const float eff = E4m3ToFloat(sf_code) * inv_scale;
  const float inv = eff > 0.0f ? 1.0f / eff : 0.0f;
  uint8_t bytes[8];
#pragma unroll
  for (int j = 0; j < 8; ++j) {
    const int c0 = FloatToE2m1Code(a[2 * j] * inv);
    const int c1 = FloatToE2m1Code(a[2 * j + 1] * inv);
    bytes[j] = static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
  }
  uint8_t* pdst = a_packed + (static_cast<size_t>(row) * groups + g) * 8;
#pragma unroll
  for (int j = 0; j < 8; ++j) pdst[j] = bytes[j];
  a_sf[SfOffsetDev(row, g, num_g_tiles)] = sf_code;
}

// The row descriptor changes only scheduling. Each thread performs exactly
// the original GatherQuant operations for one (expert-local row, group).
__global__ void GatherQuantBatchKernel(
    const uint16_t* __restrict__ x, const int32_t* __restrict__ token_list,
    const float* __restrict__ input_scale, uint8_t* __restrict__ arena,
    MoEBatchGatherRows rows) {
  constexpr int kGroups = 2560 / 16;
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= rows.count * kGroups) return;
  const MoEBatchGatherRow descriptor = rows.rows[idx / kGroups];
  const int row = descriptor.local_row;
  const int g = idx % kGroups;
  const int flat = token_list[descriptor.expert * 4 + row];
  const int t = flat / 10;
  const float inv_scale = input_scale[descriptor.expert];
  uint8_t* a_packed =
      arena + static_cast<size_t>(descriptor.active_ordinal) * kBatchSlotBytes;
  uint8_t* a_sf = a_packed + kBatchPackedBytes;

  const uint16_t* src = x + static_cast<size_t>(t) * 2560 + g * 16;
  float a[16];
  float gmax = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    const __nv_bfloat16 b = *reinterpret_cast<const __nv_bfloat16*>(&src[j]);
    a[j] = __bfloat162float(b);
    gmax = fmaxf(gmax, fabsf(a[j]));
  }
  const float block_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
  const uint8_t sf_code = FloatToE4m3(block_scale / inv_scale);
  const float eff = E4m3ToFloat(sf_code) * inv_scale;
  const float inv = eff > 0.0f ? 1.0f / eff : 0.0f;
  uint8_t bytes[8];
#pragma unroll
  for (int j = 0; j < 8; ++j) {
    const int c0 = FloatToE2m1Code(a[2 * j] * inv);
    const int c1 = FloatToE2m1Code(a[2 * j + 1] * inv);
    bytes[j] = static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
  }
  uint8_t* pdst = a_packed + (static_cast<size_t>(row) * kGroups + g) * 8;
#pragma unroll
  for (int j = 0; j < 8; ++j) pdst[j] = bytes[j];
  a_sf[SfOffsetDev(row, g, 40)] = sf_code;
}

// SwiGLU + NVFP4 quantize fused: inter = silu(g)*u, then quantize inter to
// NVFP4 (same convention as QuantizeFloat32ToFp4Kernel). One thread per
// (row, group of 16) — a group is exactly 16 inter elements, so the per-group
// max is local to the thread (no cross-thread reduce). Eliminates the
// separate SwiGLU kernel + the inter f32 round-trip through GMEM.
//   gu_out [rows, 2*moe_is] bf16 (gate | up), a_packed [rows, moe_is/2] u8,
//   a_sf swizzled e4m3, inv_scale = per-expert down input_scale.
__global__ void SwiGLUQuantKernel(const uint16_t* __restrict__ gu_out,
                                  uint8_t* __restrict__ a_packed,
                                  uint8_t* __restrict__ a_sf, int rows,
                                  int moe_is, int num_g_tiles,
                                  float inv_scale) {
  const int groups = moe_is / 16;
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= rows * groups) return;
  const int row = idx / groups;
  const int g = idx % groups;
  const uint16_t* gu = gu_out + static_cast<size_t>(row) * 2 * moe_is;
  float a[16];
  float gmax = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    const float gv = Bf16ToFloat(gu[g * 16 + j]);
    const float uv = Bf16ToFloat(gu[moe_is + g * 16 + j]);
    const float s = 1.0f / (1.0f + __expf(-gv));
    a[j] = gv * s * uv;
    gmax = fmaxf(gmax, fabsf(a[j]));
  }
  const float block_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
  const uint8_t sf_code = FloatToE4m3(block_scale / inv_scale);
  const float eff = E4m3ToFloat(sf_code) * inv_scale;
  const float inv = eff > 0.0f ? 1.0f / eff : 0.0f;
  uint8_t bytes[8];
#pragma unroll
  for (int j = 0; j < 8; ++j) {
    const int c0 = FloatToE2m1Code(a[2 * j] * inv);
    const int c1 = FloatToE2m1Code(a[2 * j + 1] * inv);
    bytes[j] = static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
  }
  uint8_t* pdst = a_packed + (static_cast<size_t>(row) * groups + g) * 8;
#pragma unroll
  for (int j = 0; j < 8; ++j) pdst[j] = bytes[j];
  a_sf[SfOffsetDev(row, g, num_g_tiles)] = sf_code;
}

// Invert the per-expert token lists into row_of_flat[flat] = the grouped
// down-GEMM output row that holds this (token,slot)'s result. Block e handles
// expert e's count = offset[e+1]-offset[e] rows in one launch (vs one per
// expert). GatherQuant/GEMMs process expert e's j-th token from
// token_list[e*stride+j], and the grouped down-GEMM writes it to row
// offset[e]+j, so row_of_flat[token_list[e*stride+j]] = offset[e]+j. This lets
// the final combine be a single deterministic gather (no cross-expert atomics
// on y). token_list is [E, stride]; row_of_flat is [R].
__global__ void BuildRowOfFlatKernel(const int32_t* __restrict__ token_list,
                                     const int32_t* __restrict__ offset,
                                     int stride,
                                     int32_t* __restrict__ row_of_flat) {
  const int e = blockIdx.x;
  const int base = offset[e];
  const int count = offset[e + 1] - base;
  for (int j = threadIdx.x; j < count; j += blockDim.x) {
    row_of_flat[token_list[e * stride + j]] = base + j;
  }
}

// Deterministic combine over the expert-grouped [R, hs] down-GEMM output in a
// single launch (replacing the E per-expert ScatterAddKernel launches, which
// were launch-overhead dominated at ~3.5us median):
//   y[t, :] += Σ_slot router_w[t*k+slot] * dn_out[row_of_flat[t*k+slot], :].
// One thread per (token t, hs element c). Each y[t,c] is written by exactly
// one thread, so there are no atomics (the old per-expert scatter summed the
// k contributions in a fixed expert order; this sums them in a fixed slot
// order) — deterministic and free of the cross-expert atomicAdd contention on
// shared tokens. Adjacent threads share the same row per slot, so the dn_out
// reads coalesce across c.
__global__ void CombineGroupedKernel(const uint16_t* __restrict__ dn_out,
                                     const int32_t* __restrict__ row_of_flat,
                                     const float* __restrict__ router_w, int k,
                                     int hs, int T, float* y) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * hs) return;
  const int t = idx / hs;
  const int c = idx % hs;
  float acc = 0.0f;
  for (int slot = 0; slot < k; ++slot) {
    const int flat = t * k + slot;
    const int r = row_of_flat[flat];
    acc += router_w[flat] * Bf16ToFloat(dn_out[static_cast<size_t>(r) * hs + c]);
  }
  y[static_cast<size_t>(t) * hs + c] += acc;
}

}  // namespace

bool MoEBatchGatherEnabled() {
  const char* value = std::getenv("Q4T_MOE_BATCH_GATHER");
  return value && std::strcmp(value, "1") == 0;
}

MoEBatchGatherStats GetMoEBatchGatherStats() {
  constexpr auto order = std::memory_order_relaxed;
  return {g_batch_stats.applied_calls.load(order),
          g_batch_stats.legacy_calls.load(order),
          g_batch_stats.bad_counts_fallback.load(order),
          g_batch_stats.batch_launches.load(order),
          g_batch_stats.legacy_gather_launches.load(order),
          g_batch_stats.replaced_gather_launches.load(order)};
}

bool MoEBatchGatherShapeSupported(int M, int k, int E, int hs, int moe_is,
                                 int streams) {
  return M == 4 && k == 10 && E == 512 && hs == 2560 && moe_is == 640 &&
         streams == kMoEBatchStreams;
}

bool MakeMoEBatchGatherPlan(const int32_t* counts, int E,
                            MoEBatchGatherPlan* plan) {
  if (!counts || !plan || E != kMoEBatchExperts) return false;
  int total = 0;
  for (int e = 0; e < E; ++e) {
    if (counts[e] < 0 || counts[e] > 4) return false;
    total += counts[e];
  }
  if (total != kMoEBatchSlots) return false;
  *plan = {};
  for (int e = 0; e < E; ++e) {
    if (counts[e] == 0) continue;
    const int ordinal = plan->active_experts++;
    auto& target = plan->streams[ordinal % kMoEBatchStreams];
    for (int row = 0; row < counts[e]; ++row)
      target.rows[target.count++] = {e, row, ordinal};
  }
  return true;
}

size_t MoEWorkspace::RequiredBytes(int M, int k, int hs, int moe_is) {
  const int R = M * k;
  size_t b = 0;
  b += static_cast<size_t>(R) * (hs / 2);  // a_packed
  b += SfBufferSize(R, hs);  // a_sf
  b += static_cast<size_t>(R) * (2 * moe_is) * sizeof(uint16_t);  // gu_out
  b += static_cast<size_t>(R) * hs * sizeof(uint16_t);  // dn_out
  return b;
}

void MoEWorkspace::Init(uint8_t* base) {
  size_t off = 0;
  a_packed = base + off;
  off += a_packed_bytes;
  a_sf = base + off;
  off += a_sf_bytes;
  gu_out = reinterpret_cast<uint16_t*>(base + off);
  off += gu_out_bytes;
  dn_out = reinterpret_cast<uint16_t*>(base + off);
  off += dn_out_bytes;
}

static Status MoERoutedForwardImpl(
    const uint16_t* x, const int32_t* expert_ids, const float* router_w,
    float* y, const MoEWeightLayout& weights, void* workspace, void* gemm_ws,
    size_t gemm_ws_bytes, int M, int k, cudaStream_t stream,
    trace::MtpVerifyMoeCall* verify_moe, bool batch_requested,
    MoERoutedCapture* capture) {
  if (batch_requested && verify_moe)
    return Status::Fail("batch gather is incompatible with verify MoE v1");
  if (verify_moe)
    verify_moe->MarkHost(trace::VerifyMoeHostPoint::kRoutedBegin);
  const int E = weights.E;
  const int hs = weights.hs;
  const int moe_is = weights.moe_is;
  if (M <= 0 || k <= 0 || E <= 0 || hs <= 0 || moe_is <= 0) {
    return Status::Fail("invalid MoE forward dims");
  }
  const int R = M * k;
  const bool device_decode =
      M == 1 && k == 10 && E == 512 && hs == 2560 && moe_is == 640;
  const int n_streams = device_decode ? 1 : MoeStreamCount();
  const bool batch_shape = MoEBatchGatherShapeSupported(
      M, k, E, hs, moe_is, n_streams);
  if (capture && (!batch_shape || !capture->device_buffer ||
                  capture->device_bytes < MoERoutedCaptureLayout::kBytes)) {
    return Status::Fail("invalid bounded MoE capture shape or capacity");
  }
  if (capture) {
    capture->batch_applied = false;
    capture->active_experts = 0;
    capture->streams = n_streams;
    capture->injection_triggered = false;
    capture->cleanup_joined = false;
    capture->cleanup_freed = false;
    capture->counts = {};
    capture->offsets = {};
    capture->experts = {};
  }

  MoEWorkspace ws;
  ws.a_packed_bytes = static_cast<size_t>(R) * (hs / 2);
  ws.a_sf_bytes = SfBufferSize(R, hs);
  ws.gu_out_bytes = static_cast<size_t>(R) * (2 * moe_is) * sizeof(uint16_t);
  ws.dn_out_bytes = static_cast<size_t>(R) * hs * sizeof(uint16_t);
  ws.Init(static_cast<uint8_t*>(workspace));

  if (device_decode) {
    if (batch_requested) Increment(&g_batch_stats.legacy_calls);
    return MoEDeviceDecode(x, expert_ids, router_w, y, weights, ws, gemm_ws,
                           gemm_ws_bytes, stream);
  }

  // Host-side scratch for per-expert counts (the token lists stay on device;
  // the gather/scatter kernels read d_token_list directly).
  std::vector<int32_t> counts_h(E, 0);
  // Keep routing metadata in the stream-ordered pool, with one allocation
  // and one lifetime. An expert can select all M tokens, so its list needs
  // capacity M (not k). Align each region as separate CUDA allocations were.
  const size_t counts_bytes = AlignUp256(static_cast<size_t>(E) * sizeof(int32_t));
  const size_t list_bytes =
      AlignUp256(static_cast<size_t>(E) * M * sizeof(int32_t));
  const size_t offset_bytes =
      AlignUp256(static_cast<size_t>(E + 1) * sizeof(int32_t));
  const size_t rows_bytes = AlignUp256(static_cast<size_t>(R) * sizeof(int32_t));
  const size_t metadata_bytes =
      counts_bytes + list_bytes + offset_bytes + rows_bytes;
  const bool reserve_batch = batch_requested && batch_shape;
  const size_t arena_guard = capture ? MoERoutedCaptureLayout::kGuard : 0;
  const size_t arena_bytes = reserve_batch
                                ? MoEBatchGatherExtraBytes() + 2 * arena_guard
                                : 0;
  uint8_t* routing = nullptr;
  if (cudaMallocAsync(&routing, metadata_bytes + arena_bytes, stream) !=
      cudaSuccess) {
    return Status::Fail("cudaMallocAsync routing metadata");
  }
  int32_t* d_counts = reinterpret_cast<int32_t*>(routing);
  int32_t* d_token_list = reinterpret_cast<int32_t*>(routing + counts_bytes);
  int32_t* d_offset =
      reinterpret_cast<int32_t*>(routing + counts_bytes + list_bytes);
  int32_t* d_row_of_flat = reinterpret_cast<int32_t*>(
      routing + counts_bytes + list_bytes + offset_bytes);
  uint8_t* batch_arena = reserve_batch
      ? routing + metadata_bytes + arena_guard : nullptr;
  bool submitted_extras[kMaxMoeStreams] = {};
  bool extras_joined = false;
  // Failure cleanup must establish an ordering edge before the allocation
  // is returned to the pool. A CUDA error that also prevents synchronization
  // leaves the allocation live and is reported explicitly.
  auto free_all = [&]() -> bool {
    bool safe = true;
    if (!extras_joined) {
      for (int si = 1; si < n_streams; ++si) {
        if (!submitted_extras[si]) continue;
        const cudaError_t recorded =
            cudaEventRecord(g_moe_events[si - 1], g_moe_streams[si - 1]);
        const cudaError_t waited = recorded == cudaSuccess
            ? cudaStreamWaitEvent(stream, g_moe_events[si - 1], 0)
            : recorded;
        if (waited != cudaSuccess &&
            cudaStreamSynchronize(g_moe_streams[si - 1]) != cudaSuccess)
          safe = false;
      }
    }
    if (capture) capture->cleanup_joined = safe;
    if (!safe) return false;
    const bool freed = cudaFreeAsync(routing, stream) == cudaSuccess;
    if (capture) capture->cleanup_freed = freed;
    return freed;
  };
  auto fail = [&](const char* message) -> Status {
    if (!free_all())
      return Status::Fail(std::string(message) +
                          "; routing cleanup failed; memory may remain live");
    return Status::Fail(message);
  };
  if (reserve_batch && capture &&
      cudaMemsetAsync(routing + metadata_bytes,
                      MoERoutedCaptureLayout::kSentinel, arena_bytes,
                      stream) != cudaSuccess)
    return fail("initialize batch capture guards");
  if (cudaMemsetAsync(d_counts, 0, E * sizeof(int32_t), stream) != cudaSuccess) {
    return fail("memset counts");
  }

  const int num_g_tiles = SfNumGtiles(hs);

  // 1. Build per-expert token lists.
  {
    const int total = M * k;
    const int blocks = (total + kBlock - 1) / kBlock;
    BuildTokenListsKernel<<<blocks, kBlock, 0, stream>>>(
        expert_ids, M, k, E, d_counts, d_token_list);
  }
  if (cudaGetLastError() != cudaSuccess) {
    return fail("kernel launch error");
  }
  if (verify_moe)
    verify_moe->MarkGpu(trace::VerifyMoeGpuPoint::kListEnd, stream);

  // 2. Read counts to host (one sync). Token lists stay on device.
  {
    // Includes the existing GPU wait; this is not GPU active time.
    trace::MtpCycleSpan timing(trace::CycleDetail::kVerifyMoeCounts);
    if (verify_moe)
      verify_moe->MarkHost(trace::VerifyMoeHostPoint::kCountsWaitBegin);
    if (cudaMemcpyAsync(counts_h.data(), d_counts, E * sizeof(int32_t),
                        cudaMemcpyDeviceToHost, stream) != cudaSuccess) {
      return fail("cudaMemcpy counts");
    }
    if (verify_moe)
      verify_moe->MarkGpu(trace::VerifyMoeGpuPoint::kCountsCopyEnd, stream);
    if (cudaStreamSynchronize(stream) != cudaSuccess) {
      return fail("stream sync");
    }
    if (verify_moe)
      verify_moe->MarkHost(trace::VerifyMoeHostPoint::kCountsWaitEnd);
  }
  if (verify_moe) verify_moe->SetCounts(counts_h.data(), E, M, k);
  MoEBatchGatherPlan batch_plan;
  const bool valid_batch_counts = (reserve_batch || capture) &&
      MakeMoEBatchGatherPlan(counts_h.data(), E, &batch_plan);
  const bool batch_applied = reserve_batch && valid_batch_counts;
  if (capture) {
    if (!valid_batch_counts) return fail("invalid bounded capture counts");
    capture->batch_applied = batch_applied;
    std::copy(counts_h.begin(), counts_h.end(), capture->counts.begin());
  }
  if (batch_requested) {
    Increment(batch_applied ? &g_batch_stats.applied_calls
                            : &g_batch_stats.legacy_calls);
    if (reserve_batch && !valid_batch_counts)
      Increment(&g_batch_stats.bad_counts_fallback);
    if (batch_applied)
      Increment(&g_batch_stats.replaced_gather_launches,
                 batch_plan.active_experts);
  }

  // Clamp per-expert counts to M. An expert can be selected by at most M
  // distinct tokens, so count > M can only arise from a degenerate router top-k
  // with duplicate experts. Left unclamped it would drive GatherQuant/GEMM with
  // M_e > M rows and overflow the per-expert [M]-row a_packed buffer. No-op
  // (bit-identical) for well-formed routing where every count <= M.
  bool clamped = false;
  for (int e = 0; e < E; ++e)
    if (counts_h[e] > M) {
      counts_h[e] = M;
      clamped = true;
    }

  // 3. Expert-group offsets (prefix sum of counts). Grouping the down-GEMM
  //    output into one contiguous [R, hs] buffer lets the combine run as a
  //    single deterministic gather over all tokens instead of one scatter
  //    launch per expert (the per-expert ScatterAddKernel was launch-overhead
  //    dominated: median ~3.5us, below the ~5us launch cost).
  std::vector<int32_t> offset_h(E + 1, 0);
  for (int e = 0; e < E; ++e) offset_h[e + 1] = offset_h[e] + counts_h[e];
  if (capture)
    std::copy(offset_h.begin(), offset_h.end(), capture->offsets.begin());
  if (cudaMemcpyAsync(d_offset, offset_h.data(),
                      static_cast<size_t>(E + 1) * sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess) {
    return fail("cudaMemcpy offset");
  }
  // Zero row_of_flat only when a count was clamped: then some (token,slot) flat
  // is dropped and left uncovered by BuildRowOfFlatKernel, so it must map to
  // grouped row 0 instead of an uninitialized garbage row (keeps the combine's
  // dn_out gather in bounds). Well-formed routing covers every flat, so the
  // memset is skipped entirely (zero cost on the normal path).
  if (clamped &&
      cudaMemsetAsync(d_row_of_flat, 0, static_cast<size_t>(R) * sizeof(int32_t),
                      stream) != cudaSuccess) {
    return fail("memset row_of_flat");
  }
  // Build the flat (token,slot) -> grouped-row inverse map for the combine.
  BuildRowOfFlatKernel<<<E, kBlock, 0, stream>>>(d_token_list, d_offset, M,
                                                 d_row_of_flat);
  if (cudaGetLastError() != cudaSuccess) {
    return fail("build row_of_flat failed");
  }
  if (capture &&
      (!CaptureCopy(capture, MoERoutedCaptureLayout::kTokenListOffset,
                    d_token_list, MoERoutedCaptureLayout::kTokenListBytes,
                    stream) ||
       !CaptureCopy(capture, MoERoutedCaptureLayout::kRowMapOffset,
                    d_row_of_flat, MoERoutedCaptureLayout::kRowMapBytes,
                    stream)))
    return fail("capture routing metadata");
  if (verify_moe)
    verify_moe->MarkHost(trace::VerifyMoeHostPoint::kOffsetsEnd);

  // Per-stream scratch. Stream 0 is the caller's stream (reuses ws + gemm_ws);
  // streams 1..n-1 get their own a_packed/a_sf/gu_out (M rows suffice since
  // M_e <= M) and cuBLASLt workspace so concurrent experts don't clobber. The
  // host sync above guarantees x / d_token_list are ready before any stream
  // reads them; the extras rejoin the caller's stream before the combine.
  struct SScratch {
    uint8_t* a_packed;
    uint8_t* a_sf;
    uint16_t* gu_out;
    void* gemm_ws;
    cudaStream_t stream;
  };
  if (verify_moe) verify_moe->SetStreams(n_streams);
  SScratch sc[kMaxMoeStreams];
  sc[0] = {reinterpret_cast<uint8_t*>(ws.a_packed),
           reinterpret_cast<uint8_t*>(ws.a_sf), ws.gu_out, gemm_ws, stream};
  if (n_streams > 1) {
    if (!EnsureMoeStreams()) return fail("moe streams initialization failed");
    const size_t ap_b = AlignUp256(static_cast<size_t>(M) * (hs / 2));
    const size_t asf_b = AlignUp256(SfBufferSize(M, hs));
    const size_t gu_b =
        AlignUp256(static_cast<size_t>(M) * (2 * moe_is) * sizeof(uint16_t));
    for (int si = 1; si < n_streams; ++si) {
      if (!EnsureMoeScratch(si - 1, ap_b + asf_b + gu_b, gemm_ws_bytes)) {
        return fail("moe stream scratch alloc");
      }
      uint8_t* base = static_cast<uint8_t*>(g_moe_scratch[si - 1].buf);
      sc[si] = {base, base + ap_b,
                reinterpret_cast<uint16_t*>(base + ap_b + asf_b),
                g_moe_scratch[si - 1].gemm_ws, g_moe_streams[si - 1]};
    }
  }

  if (batch_applied) {
    for (int si = 0; si < kMoEBatchStreams; ++si) {
      const auto& rows = batch_plan.streams[si];
      const int blocks = (rows.count * (hs / 16) + kBlock - 1) / kBlock;
      GatherQuantBatchKernel<<<blocks, kBlock, 0, sc[si].stream>>>(
          x, d_token_list, weights.gu_input_scale, batch_arena, rows);
      if (si > 0) submitted_extras[si] = true;
      if (cudaGetLastError() != cudaSuccess)
        return fail("batch gather quant failed");
      Increment(&g_batch_stats.batch_launches);
      if (capture && capture->inject_after_first_extra_batch && si == 1) {
        capture->injection_triggered = true;
        return fail("injected after first extra batch gather");
      }
    }
  }

  // 4. Per-expert GEMM chain.
  if (verify_moe)
    verify_moe->MarkHost(trace::VerifyMoeHostPoint::kExpertLoopBegin);
  int active_idx = 0;
  for (int e = 0; e < E; ++e) {
    const int M_e = counts_h[e];
    if (M_e <= 0) continue;
    const int ordinal = active_idx;
    const int stream_index = ordinal % n_streams;
    const SScratch& ss = sc[stream_index];
    ++active_idx;
    if (stream_index > 0) submitted_extras[stream_index] = true;
    MoECapturedExpert* observed = nullptr;
    if (capture) {
      observed = &capture->experts[ordinal];
      observed->expert = e;
      observed->rows = M_e;
      observed->active_ordinal = ordinal;
      observed->stream_index = stream_index;
      observed->grouped_offset = offset_h[e];
      capture->active_experts = active_idx;
    }

    // gate/up input = token activation (calibrated by gu_input_scale); the
    // down input = SwiGLU intermediate (calibrated by down_proj's own
    // input_scale). Each GEMM's alpha folds in its own (weight_scale_2 *
    // input_scale) pair.
    const float gu_in_scale = weights.gu_input_scale_h[e];
    const float dn_in_scale = weights.dn_input_scale_h[e];
    const float gu_alpha = weights.gu_w_scale2_h[e] * gu_in_scale;
    const float dn_alpha = weights.dn_w_scale2_h[e] * dn_in_scale;

    if (verify_moe)
      verify_moe->MarkExpert(active_idx - 1,
                             trace::VerifyMoeExpertPoint::kGatherBegin,
                             ss.stream);

    // Gather + quantize this expert's M_e token rows into the start of the
    // compact / a_packed / a_sf buffers (rows 0..M_e-1).
    if (!batch_applied) {
      const int total = M_e * (hs / 16);
      const int blocks = (total + kBlock - 1) / kBlock;
      GatherQuantKernel<<<blocks, kBlock, 0, ss.stream>>>(
          reinterpret_cast<const uint16_t*>(x), d_token_list, M_e, e, k, M, hs,
          num_g_tiles, weights.gu_input_scale, ss.a_packed, ss.a_sf);
      if (batch_requested) Increment(&g_batch_stats.legacy_gather_launches);
      if (cudaGetLastError() != cudaSuccess)
        return fail("gather quant failed");
    }
    const uint8_t* gu_packed = batch_applied
        ? batch_arena + static_cast<size_t>(ordinal) * kBatchSlotBytes
        : ss.a_packed;
    const uint8_t* gu_sf = batch_applied
        ? gu_packed + kBatchPackedBytes : ss.a_sf;
    if (capture &&
        (!CaptureCopy(capture, MoERoutedCaptureLayout::StageOffset(ordinal, 0),
                      gu_packed, static_cast<size_t>(M_e) * (hs / 2),
                      ss.stream) ||
         !CaptureCopy(capture, MoERoutedCaptureLayout::StageOffset(ordinal, 1),
                      gu_sf, SfBufferSize(M_e, hs), ss.stream)))
      return fail("capture gate/up inputs");
    if (verify_moe)
      verify_moe->MarkExpert(active_idx - 1,
                             trace::VerifyMoeExpertPoint::kGatherEnd,
                             ss.stream);

    // gate/up GEMM: [M_e, 2*moe_is] = act [M_e, hs] * W_gu [2*moe_is, hs]^T.
    // cuBLASLt nvjet for all M_e. Three hand-written W4A4 GEMV designs
    // (v1 per-element LUT+ldexpf, v2 256-entry product LUT, v3 register
    // LUT + 4 outputs/warp) were all 1.4-2.3x slower than nvjet (17-27us
    // vs 12us): nvjet's tensor cores do the dequant in hardware at 100%
    // DRAM peak (2.86MB/11.9us = 240 GB/s), while a SIMT kernel needs
    // ~5-6 instructions/byte for nibble extract + LUT + FMA vs a 0.63
    // inst/byte budget to stay DRAM-bound (152 Ginst/s issue limit).
    auto r1 = Fp4Gemm(weights.gu_packed_expert(e), weights.gu_sf_expert(e),
                      gu_packed, gu_sf, ss.gu_out, M_e, 2 * moe_is, hs,
                      gu_alpha, 1.0f, ss.gemm_ws, gemm_ws_bytes, ss.stream);
    if (r1.status != CUBLAS_STATUS_SUCCESS || !r1.has_algo) {
      return fail("gate/up GEMM failed");
    }
    if (capture &&
        (!CaptureGemm(&observed->gu, M_e, 2 * moe_is, hs, gemm_ws_bytes,
                      gu_alpha) ||
         !CaptureCopy(capture, MoERoutedCaptureLayout::StageOffset(ordinal, 2),
                      ss.gu_out,
                      static_cast<size_t>(M_e) * 2 * moe_is * sizeof(uint16_t),
                      ss.stream)))
      return fail("capture gate/up output or plan");
    if (verify_moe)
      verify_moe->MarkExpert(active_idx - 1,
                             trace::VerifyMoeExpertPoint::kGuEnd, ss.stream);

    // SwiGLU + quantize inter -> NVFP4 (fused, one launch; reuses a_packed /
    // a_sf, now [M_e, moe_is]).
    {
      const int num_g_tiles_dn = SfNumGtiles(moe_is);
      const int total = M_e * (moe_is / 16);
      const int blocks = (total + kBlock - 1) / kBlock;
      SwiGLUQuantKernel<<<blocks, kBlock, 0, ss.stream>>>(
          ss.gu_out, ss.a_packed, ss.a_sf, M_e, moe_is, num_g_tiles_dn,
          dn_in_scale);
    }
    if (cudaGetLastError() != cudaSuccess) {
      return fail("inter quant failed");
    }
    if (capture &&
        (!CaptureCopy(capture, MoERoutedCaptureLayout::StageOffset(ordinal, 3),
                      ss.a_packed, static_cast<size_t>(M_e) * (moe_is / 2),
                      ss.stream) ||
         !CaptureCopy(capture, MoERoutedCaptureLayout::StageOffset(ordinal, 4),
                      ss.a_sf, SfBufferSize(M_e, moe_is), ss.stream)))
      return fail("capture down inputs");
    if (verify_moe)
      verify_moe->MarkExpert(active_idx - 1,
                             trace::VerifyMoeExpertPoint::kActivationEnd,
                             ss.stream);

    // down GEMM: [M_e, hs] = inter [M_e, moe_is] * W_dn [hs, moe_is]^T.
    // (Same nvjet rationale as gate/up above.) Write into this expert's slice
    // of the grouped [R, hs] output (rows offset[e]..offset[e]+M_e). beta=0 in
    // Fp4Gemm, so each expert overwrites its own disjoint slice.
    auto r2 = Fp4Gemm(weights.dn_packed_expert(e), weights.dn_sf_expert(e),
                      ss.a_packed, ss.a_sf,
                      ws.dn_out + static_cast<size_t>(offset_h[e]) * hs, M_e, hs,
                      moe_is, dn_alpha, 1.0f, ss.gemm_ws, gemm_ws_bytes,
                      ss.stream);
    if (r2.status != CUBLAS_STATUS_SUCCESS || !r2.has_algo) {
      return fail("down GEMM failed");
    }
    if (capture &&
        (!CaptureGemm(&observed->dn, M_e, hs, moe_is, gemm_ws_bytes,
                      dn_alpha) ||
         !CaptureCopy(capture, MoERoutedCaptureLayout::StageOffset(ordinal, 5),
                      ws.dn_out + static_cast<size_t>(offset_h[e]) * hs,
                      static_cast<size_t>(M_e) * hs * sizeof(uint16_t),
                      ss.stream)))
      return fail("capture down output or plan");
    if (verify_moe)
      verify_moe->MarkExpert(active_idx - 1,
                             trace::VerifyMoeExpertPoint::kDnEnd, ss.stream);
  }
  if (verify_moe)
    verify_moe->MarkHost(trace::VerifyMoeHostPoint::kExpertLoopEnd);

  // Rejoin the extra streams: the combine (on the caller's stream) must not
  // read dn_out until every stream's down GEMM has landed.
  for (int si = 1; si < n_streams; ++si) {
    if (cudaEventRecord(g_moe_events[si - 1], g_moe_streams[si - 1]) !=
            cudaSuccess ||
        cudaStreamWaitEvent(stream, g_moe_events[si - 1], 0) != cudaSuccess)
      return fail("join MoE expert streams failed");
  }
  extras_joined = true;
  if (capture && batch_applied &&
      !CaptureCopy(capture, MoERoutedCaptureLayout::kArenaOffset,
                   routing + metadata_bytes, arena_bytes, stream))
    return fail("capture batch arena guards");
  if (verify_moe)
    verify_moe->MarkGpu(trace::VerifyMoeGpuPoint::kExpertsJoined, stream);

  // 5. Single deterministic combine over all experts' grouped rows.
  {
    const size_t total = static_cast<size_t>(M) * hs;
    const int blocks = static_cast<int>((total + kBlock - 1) / kBlock);
    CombineGroupedKernel<<<blocks, kBlock, 0, stream>>>(
        ws.dn_out, d_row_of_flat, router_w, k, hs, M, y);
  }
  if (cudaGetLastError() != cudaSuccess) {
    return fail("combine failed");
  }

  if (!free_all()) return Status::Fail("routing cleanup failed");
  return Status();
}

Status MoERoutedForward(const uint16_t* x, const int32_t* expert_ids,
                        const float* router_w, float* y,
                        const MoEWeightLayout& weights, void* workspace,
                        void* gemm_ws, size_t gemm_ws_bytes, int M, int k,
                        cudaStream_t stream,
                        trace::MtpVerifyMoeCall* verify_moe) {
  return MoERoutedForwardImpl(x, expert_ids, router_w, y, weights, workspace,
                              gemm_ws, gemm_ws_bytes, M, k, stream, verify_moe,
                              MoEBatchGatherEnabled(), nullptr);
}

Status MoERoutedForwardForTest(
    const uint16_t* x, const int32_t* expert_ids, const float* router_w,
    float* y, const MoEWeightLayout& weights, void* workspace, void* gemm_ws,
    size_t gemm_ws_bytes, int M, int k, cudaStream_t stream,
    bool batch_requested, MoERoutedCapture* capture) {
  if (!capture) return Status::Fail("bounded MoE capture is required");
  return MoERoutedForwardImpl(x, expert_ids, router_w, y, weights, workspace,
                              gemm_ws, gemm_ws_bytes, M, k, stream, nullptr,
                              batch_requested, capture);
}

}  // namespace quant
}  // namespace q4t
