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

#include <cstdint>
#include <vector>

#include "q4t/quant/fp4_gemm.h"
#include "q4t/quant/format.h"
#include "q4t/quant/swizzle.h"

namespace q4t {
namespace quant {

namespace {

constexpr int kBlock = 256;

// Device copy of the SF swizzle offset (swizzle.h is host-only).
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
//   token_list [E, k] int32 (token index per expert, padded to k),
//   expert_counts [E] int32.
__global__ void BuildTokenListsKernel(const int32_t* __restrict__ expert_ids,
                                      int M, int k, int E,
                                      int32_t* __restrict__ expert_counts,
                                      int32_t* __restrict__ token_list) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= M * k) return;
  const int e = expert_ids[idx];
  const int pos = atomicAdd(&expert_counts[e], 1);
  // Store the flat (token, slot) index so the scatter can recover both the
  // token (idx / k) and the router-weight slot (idx % k).
  token_list[e * k + pos] = idx;
}

// Gather + quantize one expert's tokens: one thread per (row, group of 16).
// Writes the expert's M_e rows to the START of the compact / a_packed / a_sf
// buffers (rows 0..M_e-1) so the following per-expert GEMM can address them
// directly. token_list holds [E, k] token indices; expert e's tokens are
// token_list[e * k + 0 .. M_e-1].
//   x [M, hs] bf16, token_list [E, k] int32, input_scale float (per expert),
//   compact [M_e, hs] bf16 (out), a_packed [M_e, hs/2] u8 (out),
//   a_sf swizzled e4m3 (out).
__global__ void GatherQuantKernel(
    const uint16_t* __restrict__ x, const int32_t* __restrict__ token_list,
    int M_e, int e, int k, int hs, int num_g_tiles,
    const float* __restrict__ input_scale, uint16_t* __restrict__ compact,
    uint8_t* __restrict__ a_packed, uint8_t* __restrict__ a_sf) {
  const int groups = hs / 16;
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= M_e * groups) return;
  const int row = idx / groups;
  const int g = idx % groups;
  const int flat = token_list[e * k + row];  // (token, slot) flat index
  const int t = flat / k;
  const float inv_scale = input_scale[e];

  const uint16_t* src = x + static_cast<size_t>(t) * hs + g * 16;
  uint16_t* dst = compact + static_cast<size_t>(row) * hs + g * 16;
  float a[16];
  float gmax = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    const __nv_bfloat16 b = *reinterpret_cast<const __nv_bfloat16*>(&src[j]);
    a[j] = __bfloat162float(b);
    dst[j] = src[j];
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

// Quantize a float32 [rows, K] buffer (the SwiGLU intermediate) to NVFP4,
// using the same convention as GatherQuantKernel. One thread per (row, group
// of 16).
__global__ void QuantizeFloat32ToFp4Kernel(
    const float* __restrict__ f32, uint8_t* __restrict__ a_packed,
    uint8_t* __restrict__ a_sf, int rows, int K, int num_g_tiles,
    float inv_scale) {
  const int groups = K / 16;
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= rows * groups) return;
  const int row = idx / groups;
  const int g = idx % groups;
  const float* src = f32 + static_cast<size_t>(row) * K + g * 16;
  float a[16];
  float gmax = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    a[j] = src[j];
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

// SwiGLU: inter = silu(g) * u. gu_out [rows, 2*moe_is] f32 -> inter [rows,
// moe_is] f32. One thread per (row, moe_is element).
__global__ void SwiGLUKernel(const float* __restrict__ gu_out,
                             float* __restrict__ inter, int rows, int moe_is) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= rows * moe_is) return;
  const int row = idx / moe_is;
  const int c = idx % moe_is;
  const float g = gu_out[static_cast<size_t>(row) * 2 * moe_is + c];
  const float u = gu_out[static_cast<size_t>(row) * 2 * moe_is + moe_is + c];
  const float s = 1.0f / (1.0f + __expf(-g));
  inter[idx] = (g * s) * u;
}

// Scatter-add: y[t, :] += router_w[t, slot] * dn_out[row, :]. One thread per
// (row, hs element).
__global__ void ScatterAddKernel(const float* __restrict__ dn_out,
                                 const int32_t* __restrict__ token_list,
                                 const float* __restrict__ router_w, int k,
                                 int hs, int M_e, int e, float* y) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= M_e * hs) return;
  const int row = idx / hs;
  const int c = idx % hs;
  const int flat = token_list[e * k + row];  // (token, slot) flat index
  const int t = flat / k;
  const float w = router_w[flat];
  atomicAdd(&y[static_cast<size_t>(t) * hs + c], w * dn_out[idx]);
}

}  // namespace

size_t MoEWorkspace::RequiredBytes(int M, int k, int hs, int moe_is) {
  const int R = M * k;
  size_t b = 0;
  b += static_cast<size_t>(R) * hs * sizeof(uint16_t);  // compact
  b += static_cast<size_t>(R) * (hs / 2);  // a_packed
  b += SfBufferSize(R, hs);  // a_sf
  b += static_cast<size_t>(R) * (2 * moe_is) * sizeof(float);  // gu_out
  b += static_cast<size_t>(R) * moe_is * sizeof(float);  // inter
  b += static_cast<size_t>(R) * hs * sizeof(float);  // dn_out
  return b;
}

void MoEWorkspace::Init(uint8_t* base) {
  size_t off = 0;
  compact = base;
  off += compact_bytes;
  a_packed = base + off;
  off += a_packed_bytes;
  a_sf = base + off;
  off += a_sf_bytes;
  gu_out = reinterpret_cast<float*>(base + off);
  off += gu_out_bytes;
  inter = reinterpret_cast<float*>(base + off);
  off += inter_bytes;
  dn_out = reinterpret_cast<float*>(base + off);
  off += dn_out_bytes;
}

Status MoERoutedForward(const uint16_t* x, const int32_t* expert_ids,
                        const float* router_w, float* y,
                        const MoEWeightLayout& weights, void* workspace,
                        void* gemm_ws, size_t gemm_ws_bytes, int M, int k,
                        cudaStream_t stream) {
  const int E = weights.E;
  const int hs = weights.hs;
  const int moe_is = weights.moe_is;
  if (M <= 0 || k <= 0 || E <= 0 || hs <= 0 || moe_is <= 0) {
    return Status::Fail("invalid MoE forward dims");
  }
  const int R = M * k;

  MoEWorkspace ws;
  ws.compact_bytes = static_cast<size_t>(R) * hs * sizeof(uint16_t);
  ws.a_packed_bytes = static_cast<size_t>(R) * (hs / 2);
  ws.a_sf_bytes = SfBufferSize(R, hs);
  ws.gu_out_bytes = static_cast<size_t>(R) * (2 * moe_is) * sizeof(float);
  ws.inter_bytes = static_cast<size_t>(R) * moe_is * sizeof(float);
  ws.dn_out_bytes = static_cast<size_t>(R) * hs * sizeof(float);
  ws.Init(static_cast<uint8_t*>(workspace));

  // Host-side scratch for per-expert counts (the token lists stay on device;
  // the gather/scatter kernels read d_token_list directly).
  std::vector<int32_t> counts_h(E, 0);
  int32_t* d_counts = nullptr;
  int32_t* d_token_list = nullptr;
  if (cudaMalloc(&d_counts, E * sizeof(int32_t)) != cudaSuccess)
    return Status::Fail("cudaMalloc counts");
  if (cudaMalloc(&d_token_list, static_cast<size_t>(E) * k * sizeof(int32_t)) !=
      cudaSuccess) {
    cudaFree(d_counts);
    return Status::Fail("cudaMalloc token_list");
  }
  cudaMemsetAsync(d_counts, 0, E * sizeof(int32_t), stream);

  const int num_g_tiles = SfNumGtiles(hs);

  // 1. Build per-expert token lists.
  {
    const int total = M * k;
    const int blocks = (total + kBlock - 1) / kBlock;
    BuildTokenListsKernel<<<blocks, kBlock, 0, stream>>>(
        expert_ids, M, k, E, d_counts, d_token_list);
  }
  if (cudaGetLastError() != cudaSuccess) {
    cudaFree(d_counts);
    cudaFree(d_token_list);
    return Status::Fail("kernel launch error");
  }

  // 2. Read counts to host (one sync). Token lists stay on device.
  if (cudaMemcpyAsync(counts_h.data(), d_counts, E * sizeof(int32_t),
                      cudaMemcpyDeviceToHost, stream) != cudaSuccess) {
    cudaFree(d_counts);
    cudaFree(d_token_list);
    return Status::Fail("cudaMemcpy counts");
  }
  if (cudaStreamSynchronize(stream) != cudaSuccess) {
    cudaFree(d_counts);
    cudaFree(d_token_list);
    return Status::Fail("stream sync");
  }

  // 4. Per-expert GEMM chain.
  for (int e = 0; e < E; ++e) {
    const int M_e = counts_h[e];
    if (M_e <= 0) continue;

    // gate/up input = token activation (calibrated by gu_input_scale); the
    // down input = SwiGLU intermediate (calibrated by down_proj's own
    // input_scale). Each GEMM's alpha folds in its own (weight_scale_2 *
    // input_scale) pair.
    const float gu_in_scale = weights.gu_input_scale_h[e];
    const float dn_in_scale = weights.dn_input_scale_h[e];
    const float gu_alpha = weights.gu_w_scale2_h[e] * gu_in_scale;
    const float dn_alpha = weights.dn_w_scale2_h[e] * dn_in_scale;

    // Gather + quantize this expert's M_e token rows into the start of the
    // compact / a_packed / a_sf buffers (rows 0..M_e-1).
    {
      const int total = M_e * (hs / 16);
      const int blocks = (total + kBlock - 1) / kBlock;
      GatherQuantKernel<<<blocks, kBlock, 0, stream>>>(
          reinterpret_cast<const uint16_t*>(x), d_token_list, M_e, e, k, hs,
          num_g_tiles, weights.gu_input_scale,
          reinterpret_cast<uint16_t*>(ws.compact),
          reinterpret_cast<uint8_t*>(ws.a_packed),
          reinterpret_cast<uint8_t*>(ws.a_sf));
    }
    if (cudaGetLastError() != cudaSuccess) {
      cudaFree(d_counts);
      cudaFree(d_token_list);
      return Status::Fail("gather quant failed");
    }

    // gate/up GEMM: [M_e, 2*moe_is] = act [M_e, hs] * W_gu [2*moe_is, hs]^T.
    auto r1 = Fp4Gemm(weights.gu_packed_expert(e), weights.gu_sf_expert(e),
                      ws.a_packed, ws.a_sf, ws.gu_out, M_e, 2 * moe_is, hs,
                      gu_alpha, 1.0f, gemm_ws, gemm_ws_bytes, stream);
    if (r1.status != CUBLAS_STATUS_SUCCESS || !r1.has_algo) {
      cudaFree(d_counts);
      cudaFree(d_token_list);
      return Status::Fail("gate/up GEMM failed");
    }

    // SwiGLU: [M_e, moe_is].
    {
      const int total = M_e * moe_is;
      const int blocks = (total + kBlock - 1) / kBlock;
      SwiGLUKernel<<<blocks, kBlock, 0, stream>>>(ws.gu_out, ws.inter, M_e,
                                                  moe_is);
    }

    // Quantize inter (float32) -> NVFP4 (reuse a_packed / a_sf, now [M_e,
    // moe_is]).
    {
      const int num_g_tiles_dn = SfNumGtiles(moe_is);
      const int total = M_e * (moe_is / 16);
      const int blocks = (total + kBlock - 1) / kBlock;
      QuantizeFloat32ToFp4Kernel<<<blocks, kBlock, 0, stream>>>(
          ws.inter, reinterpret_cast<uint8_t*>(ws.a_packed),
          reinterpret_cast<uint8_t*>(ws.a_sf), M_e, moe_is, num_g_tiles_dn,
          dn_in_scale);
    }
    if (cudaGetLastError() != cudaSuccess) {
      cudaFree(d_counts);
      cudaFree(d_token_list);
      return Status::Fail("inter quant failed");
    }

    // down GEMM: [M_e, hs] = inter [M_e, moe_is] * W_dn [hs, moe_is]^T.
    auto r2 = Fp4Gemm(weights.dn_packed_expert(e), weights.dn_sf_expert(e),
                      ws.a_packed, ws.a_sf, ws.dn_out, M_e, hs, moe_is,
                      dn_alpha, 1.0f, gemm_ws, gemm_ws_bytes, stream);
    if (r2.status != CUBLAS_STATUS_SUCCESS || !r2.has_algo) {
      cudaFree(d_counts);
      cudaFree(d_token_list);
      return Status::Fail("down GEMM failed");
    }

    // scatter-add into y.
    {
      const int total = M_e * hs;
      const int blocks = (total + kBlock - 1) / kBlock;
      ScatterAddKernel<<<blocks, kBlock, 0, stream>>>(
          ws.dn_out, d_token_list, router_w, k, hs, M_e, e, y);
    }
  }

  cudaFree(d_counts);
  cudaFree(d_token_list);
  return Status();
}

}  // namespace quant
}  // namespace q4t
