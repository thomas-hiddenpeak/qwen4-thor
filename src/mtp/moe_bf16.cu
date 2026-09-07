// BF16 routed-expert MoE forward for the MTP draft layer. See moe_bf16.h.
//
// Per expert e that has tokens:
//   1. Gather the expert's token rows from x into a compact [M_e, hs] buffer.
//   2. gate/up GEMM  -> [M_e, 2*moe_is]  (BF16, alpha = 1.0)
//   3. SwiGLU        -> [M_e, moe_is]
//   4. down GEMM     -> [M_e, hs]        (BF16, alpha = 1.0)
//   5. scatter-add router_w * down_out into y.
//
// The orchestration (BuildTokenLists + one host sync for the counts + the
// per-expert loop) mirrors q4t::quant::MoERoutedForward; the only difference
// is the per-expert GEMMs are plain BF16 (no runtime activation quantization).
#include "q4t/mtp/moe_bf16.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstring>
#include <vector>

#include "q4t/model/linear.h"

namespace q4t {
namespace mtp {

namespace {

constexpr int kBlock = 256;

__device__ __forceinline__ float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}
__device__ __forceinline__ uint16_t FloatToBf16(float f) {
  const __nv_bfloat16 b = __float2bfloat16_rn(f);
  return *reinterpret_cast<const uint16_t*>(&b);
}

// Build per-expert token lists + counts. One thread per (token, slot).
//   expert_ids [M, k] int32.
//   token_list [E, M] int32 (stride = M: an expert can be selected by up to
//     M tokens, so the per-expert capacity must be M, not k).
//   expert_counts [E] int32.
// Stores the flat (token, slot) index so the scatter can recover both the
// token (idx / k) and the router-weight slot (idx % k).
__global__ void BuildTokenListsKernel(const int32_t* __restrict__ expert_ids,
                                      int M, int k, int E,
                                      int32_t* __restrict__ expert_counts,
                                      int32_t* __restrict__ token_list) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= M * k) return;
  const int e = expert_ids[idx];
  const int pos = atomicAdd(&expert_counts[e], 1);
  token_list[e * M + pos] = idx;
}

// Gather one expert's token rows from x into the start of the compact buffer
// (rows 0..M_e-1) so the following per-expert GEMM can address them directly.
//   x [M, hs] bf16, token_list [E, M] int32, compact [M_e, hs] bf16 (out).
__global__ void GatherKernel(const uint16_t* __restrict__ x,
                             const int32_t* __restrict__ token_list, int M_e,
                             int e, int k, int stride, int hs,
                             uint16_t* __restrict__ compact) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= M_e * hs) return;
  const int row = idx / hs;
  const int flat = token_list[e * stride + row];  // (token, slot) flat index
  const int t = flat / k;
  const uint16_t* src = x + static_cast<size_t>(t) * hs + (idx % hs);
  compact[static_cast<size_t>(row) * hs + (idx % hs)] = *src;
}

// SwiGLU for the shared expert: out[t, c] = silu(gu[t, c]) * gu[t, shared_is +
// c]. gu [T, 2*shared_is] bf16 -> out [T, shared_is] bf16.
__global__ void SharedSwiGLUKernel(const uint16_t* __restrict__ gu,
                                   uint16_t* __restrict__ out, int T,
                                   int shared_is) {
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

// SwiGLU: inter = silu(g) * u. gu_out [rows, 2*moe_is] bf16 -> inter [rows,
// moe_is] bf16. One thread per (row, moe_is element).
__global__ void SwiGLUKernel(const uint16_t* __restrict__ gu_out,
                             uint16_t* __restrict__ inter, int rows,
                             int moe_is) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= rows * moe_is) return;
  const int row = idx / moe_is;
  const int c = idx % moe_is;
  auto bf2f = [](uint16_t b) {
    uint32_t bits = static_cast<uint32_t>(b) << 16;
    float f;
    std::memcpy(&f, &bits, sizeof(f));
    return f;
  };
  const float g = bf2f(gu_out[static_cast<size_t>(row) * 2 * moe_is + c]);
  const float u = bf2f(gu_out[static_cast<size_t>(row) * 2 * moe_is + moe_is + c]);
  const float s = 1.0f / (1.0f + __expf(-g));
  const __nv_bfloat16 out = __float2bfloat16_rn((g * s) * u);
  inter[idx] = *reinterpret_cast<const uint16_t*>(&out);
}

// Scatter-add: y[t, :] += router_w[t, slot] * dn_out[row, :]. One thread per
// (row, hs element).
__global__ void ScatterAddKernel(const uint16_t* __restrict__ dn_out,
                                 const int32_t* __restrict__ token_list,
                                 const float* __restrict__ router_w, int k,
                                 int stride, int hs, int M_e, int e, float* y) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= M_e * hs) return;
  const int row = idx / hs;
  const int c = idx % hs;
  const int flat = token_list[e * stride + row];  // (token, slot) flat index
  const int t = flat / k;
  const float w = router_w[flat];
  uint32_t bits = static_cast<uint32_t>(dn_out[idx]) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  atomicAdd(&y[static_cast<size_t>(t) * hs + c], w * f);
}

// Top-k selection + softmax over the selected k logits (mirrors
// q4t::model's RouterTopkKernel). One block per token; thread 0 does the
// sequential scan (E is small, 512).
__global__ void RouterTopkKernel(const uint16_t* __restrict__ logits,
                                 int32_t* __restrict__ expert_ids,
                                 float* __restrict__ router_w, int T, int E,
                                 int k) {
  const int t = blockIdx.x;
  if (t >= T) return;
  const uint16_t* row = logits + static_cast<size_t>(t) * E;
  if (threadIdx.x != 0) return;
  float top_vals[16];
  int top_ids[16];
  for (int i = 0; i < k; ++i) {
    top_vals[i] = -1e30f;
    top_ids[i] = 0;
  }
  for (int e = 0; e < E; ++e) {
    const float val = Bf16ToFloat(row[e]);
    int min_k = 0;
    float min_val = top_vals[0];
    for (int i = 1; i < k; ++i) {
      if (top_vals[i] < min_val) {
        min_val = top_vals[i];
        min_k = i;
      }
    }
    if (val > min_val) {
      top_vals[min_k] = val;
      top_ids[min_k] = e;
    }
  }
  float max_val = top_vals[0];
  for (int i = 1; i < k; ++i) max_val = fmaxf(max_val, top_vals[i]);
  float sum = 0.0f;
  for (int i = 0; i < k; ++i) {
    top_vals[i] = __expf(top_vals[i] - max_val);
    sum += top_vals[i];
  }
  const float inv = 1.0f / sum;
  int32_t* out_ids = expert_ids + static_cast<size_t>(t) * k;
  float* out_w = router_w + static_cast<size_t>(t) * k;
  for (int i = 0; i < k; ++i) {
    out_ids[i] = top_ids[i];
    out_w[i] = top_vals[i] * inv;
  }
}

// y[t, c] = routed[t, c] + sigmoid(dot(x[t], gate_scalar)) * shared_down[t, c]
// (mirrors q4t::model's MoECombineKernel). One block per token.
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

}  // namespace

void MoeBf16Weights::Free() {
  if (gu) cudaFree(gu);
  if (down) cudaFree(down);
  gu = down = nullptr;
}

size_t MoeBf16WorkspaceBytes(int M, int k, int hs, int moe_is, int E) {
  const int R = M * k;
  size_t b = 0;
  b += static_cast<size_t>(R) * hs * sizeof(uint16_t);  // compact
  b += static_cast<size_t>(R) * (2 * moe_is) * sizeof(uint16_t);  // gu_out
  b += static_cast<size_t>(R) * moe_is * sizeof(uint16_t);  // inter
  b += static_cast<size_t>(R) * hs * sizeof(uint16_t);  // dn_out
  // token lists + counts (allocated via cudaMallocAsync in the forward, but
  // counted here so a caller sizing a single shared buffer can include them).
  b += static_cast<size_t>(E) * M * sizeof(int32_t);  // token_list
  b += static_cast<size_t>(E) * sizeof(int32_t);  // counts
  return (b + 255) & ~size_t(255);
}

Status LoadMoeBf16(const io::WeightLoader& loader, const std::string& prefix,
                   int E, int hs, int moe_is, MoeBf16Weights* out,
                   cudaStream_t stream) {
  if (E <= 0 || hs <= 0 || moe_is <= 0) {
    return Status::Fail("invalid MTP MoE dims");
  }
  out->E = E;
  out->hs = hs;
  out->moe_is = moe_is;

  const size_t gu_bytes =
      static_cast<size_t>(E) * (2 * moe_is) * hs * sizeof(uint16_t);
  const size_t down_bytes =
      static_cast<size_t>(E) * hs * moe_is * sizeof(uint16_t);
  if (cudaMalloc(reinterpret_cast<void**>(&out->gu), gu_bytes) != cudaSuccess)
    return Status::Fail("cudaMalloc gu");
  if (cudaMalloc(reinterpret_cast<void**>(&out->down), down_bytes) !=
      cudaSuccess) {
    cudaFree(out->gu);
    out->gu = nullptr;
    return Status::Fail("cudaMalloc down");
  }

  // Read the 3-D tensors as raw contiguous bytes (row-major, expert-major).
  std::vector<uint16_t> host;
  Status s;
  host.resize(gu_bytes / sizeof(uint16_t));
  if (!(s = loader.ReadTensor(prefix + ".experts.gate_up_proj", host.data()))
           .ok())
    return s;
  if (cudaMemcpyAsync(out->gu, host.data(), gu_bytes, cudaMemcpyHostToDevice,
                      stream) != cudaSuccess)
    return Status::Fail("H2D gu");
  host.resize(down_bytes / sizeof(uint16_t));
  if (!(s = loader.ReadTensor(prefix + ".experts.down_proj", host.data())).ok())
    return s;
  if (cudaMemcpyAsync(out->down, host.data(), down_bytes, cudaMemcpyHostToDevice,
                      stream) != cudaSuccess)
    return Status::Fail("H2D down");
  if (stream != nullptr && cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("stream sync failed");
  }
  return Status();
}

Status MoeBf16RoutedForward(const uint16_t* x, const int32_t* expert_ids,
                            const float* router_w, float* y,
                            const MoeBf16Weights& weights, void* workspace,
                            void* gemm_ws, size_t gemm_ws_bytes, int M, int k,
                            cudaStream_t stream) {
  const int E = weights.E;
  const int hs = weights.hs;
  const int moe_is = weights.moe_is;
  if (M <= 0 || k <= 0 || E <= 0 || hs <= 0 || moe_is <= 0) {
    return Status::Fail("invalid MTP MoE forward dims");
  }
  const int R = M * k;

  // Carve the workspace: compact [R, hs] bf16, gu_out [R, 2*moe_is] bf16,
  // inter [R, moe_is] bf16, dn_out [R, hs] bf16.
  uint8_t* base = static_cast<uint8_t*>(workspace);
  size_t off = 0;
  auto carve = [&](size_t bytes) {
    void* p = base + off;
    off += (bytes + 7) & ~size_t(7);
    return p;
  };
  uint16_t* d_compact = static_cast<uint16_t*>(carve(static_cast<size_t>(R) * hs * sizeof(uint16_t)));
  uint16_t* d_gu_out = static_cast<uint16_t*>(carve(static_cast<size_t>(R) * (2 * moe_is) * sizeof(uint16_t)));
  uint16_t* d_inter = static_cast<uint16_t*>(carve(static_cast<size_t>(R) * moe_is * sizeof(uint16_t)));
  uint16_t* d_dn_out = static_cast<uint16_t*>(carve(static_cast<size_t>(R) * hs * sizeof(uint16_t)));

  // Host-side scratch for per-expert counts (token lists stay on device).
  std::vector<int32_t> counts_h(E, 0);
  int32_t* d_counts = nullptr;
  int32_t* d_token_list = nullptr;
  if (cudaMallocAsync(&d_counts, E * sizeof(int32_t), stream) != cudaSuccess)
    return Status::Fail("cudaMallocAsync counts");
  if (cudaMallocAsync(&d_token_list, static_cast<size_t>(E) * M * sizeof(int32_t),
                      stream) != cudaSuccess) {
    cudaFreeAsync(d_counts, stream);
    return Status::Fail("cudaMallocAsync token_list");
  }
  cudaMemsetAsync(d_counts, 0, E * sizeof(int32_t), stream);

  // 1. Build per-expert token lists.
  {
    const int total = M * k;
    const int blocks = (total + kBlock - 1) / kBlock;
    BuildTokenListsKernel<<<blocks, kBlock, 0, stream>>>(
        expert_ids, M, k, E, d_counts, d_token_list);
  }
  if (cudaGetLastError() != cudaSuccess) {
    cudaFreeAsync(d_counts, stream);
    cudaFreeAsync(d_token_list, stream);
    return Status::Fail("token-list launch");
  }

  // 2. Read counts to host (one sync). Token lists stay on device.
  if (cudaMemcpyAsync(counts_h.data(), d_counts, E * sizeof(int32_t),
                      cudaMemcpyDeviceToHost, stream) != cudaSuccess) {
    cudaFreeAsync(d_counts, stream);
    cudaFreeAsync(d_token_list, stream);
    return Status::Fail("memcpy counts");
  }
  if (cudaStreamSynchronize(stream) != cudaSuccess) {
    cudaFreeAsync(d_counts, stream);
    cudaFreeAsync(d_token_list, stream);
    return Status::Fail("stream sync");
  }

  // 3. Per-expert BF16 GEMM chain.
  for (int e = 0; e < E; ++e) {
    const int M_e = counts_h[e];
    if (M_e <= 0) continue;

    // Gather this expert's M_e token rows into the start of the compact
    // buffer (rows 0..M_e-1).
    {
      const int total = M_e * hs;
      const int blocks = (total + kBlock - 1) / kBlock;
      GatherKernel<<<blocks, kBlock, 0, stream>>>(x, d_token_list, M_e, e, k, M,
                                                  hs, d_compact);
    }
    if (cudaGetLastError() != cudaSuccess) {
      cudaFreeAsync(d_counts, stream);
      cudaFreeAsync(d_token_list, stream);
      return Status::Fail("gather launch");
    }

    // gate/up GEMM: [M_e, 2*moe_is] = act [M_e, hs] * W_gu [2*moe_is, hs]^T.
    // Expert e's gate/up slice is weights.gu + e*(2*moe_is*hs).
    auto r1 = model::Bf16Gemm(d_compact, weights.gu + static_cast<size_t>(e) * (2 * moe_is) * hs,
                              d_gu_out, M_e, 2 * moe_is, hs, 1.0f, 0.0f, gemm_ws,
                              gemm_ws_bytes, stream);
    if (r1.status != CUBLAS_STATUS_SUCCESS || !r1.has_algo) {
      cudaFreeAsync(d_counts, stream);
      cudaFreeAsync(d_token_list, stream);
      return Status::Fail("gate/up GEMM failed");
    }

    // SwiGLU: [M_e, moe_is].
    {
      const int total = M_e * moe_is;
      const int blocks = (total + kBlock - 1) / kBlock;
      SwiGLUKernel<<<blocks, kBlock, 0, stream>>>(d_gu_out, d_inter, M_e, moe_is);
    }
    if (cudaGetLastError() != cudaSuccess) {
      cudaFreeAsync(d_counts, stream);
      cudaFreeAsync(d_token_list, stream);
      return Status::Fail("swiglu launch");
    }

    // down GEMM: [M_e, hs] = inter [M_e, moe_is] * W_dn [hs, moe_is]^T.
    // Expert e's down slice is weights.down + e*(hs*moe_is).
    auto r2 = model::Bf16Gemm(d_inter, weights.down + static_cast<size_t>(e) * hs * moe_is,
                              d_dn_out, M_e, hs, moe_is, 1.0f, 0.0f, gemm_ws,
                              gemm_ws_bytes, stream);
    if (r2.status != CUBLAS_STATUS_SUCCESS || !r2.has_algo) {
      cudaFreeAsync(d_counts, stream);
      cudaFreeAsync(d_token_list, stream);
      return Status::Fail("down GEMM failed");
    }

    // Scatter-add router_w * dn_out into y.
    {
      const int total = M_e * hs;
      const int blocks = (total + kBlock - 1) / kBlock;
      ScatterAddKernel<<<blocks, kBlock, 0, stream>>>(
          d_dn_out, d_token_list, router_w, k, M, hs, M_e, e, y);
    }
    if (cudaGetLastError() != cudaSuccess) {
      cudaFreeAsync(d_counts, stream);
      cudaFreeAsync(d_token_list, stream);
      return Status::Fail("scatter launch");
    }
  }

  cudaFreeAsync(d_counts, stream);
  cudaFreeAsync(d_token_list, stream);
  return Status();
}

Status MoeBf16Forward(const uint16_t* x, const MoeBf16Weights& routed,
                      const model::MoEExtraWeights& extra, uint16_t* y, int T,
                      int k, void* workspace, size_t workspace_bytes,
                      void* gemm_ws, size_t gemm_ws_bytes, cudaStream_t stream) {
  const int hs = extra.hs;
  const int E = extra.E;
  const int shared_is = extra.shared_is;
  const int moe_is = routed.moe_is;
  if (T <= 0) return Status();
  if (k <= 0 || k > 16) return Status::Fail("k out of range");
  const size_t need = MoeBf16WorkspaceBytes(T, k, hs, moe_is, E) +
                      ((static_cast<size_t>(T) * E * sizeof(uint16_t) +
                        static_cast<size_t>(T) * k * (sizeof(int32_t) + sizeof(float)) +
                        static_cast<size_t>(T) * hs * sizeof(float) +
                        static_cast<size_t>(T) * (2 * shared_is) * sizeof(uint16_t) +
                        static_cast<size_t>(T) * shared_is * sizeof(uint16_t) +
                        static_cast<size_t>(T) * hs * sizeof(uint16_t) + 7) &
                       ~size_t(7));
  if (workspace_bytes < need) return Status::Fail("workspace too small");

  // Carve workspace: routed region first, then scratch.
  uint8_t* base = static_cast<uint8_t*>(workspace);
  const size_t routed_bytes = MoeBf16WorkspaceBytes(T, k, hs, moe_is, E);
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
  auto r0 = model::Bf16Gemm(x, extra.gate, d_logits, T, E, hs, 1.0f, 0.0f,
                            gemm_ws, gemm_ws_bytes, stream);
  if (r0.status != CUBLAS_STATUS_SUCCESS || !r0.has_algo)
    return Status::Fail("router GEMM failed");
  // 2. top-k + softmax.
  RouterTopkKernel<<<T, kBlock, 0, stream>>>(d_logits, d_eid, d_rw, T, E, k);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("topk launch");
  // 3. routed experts (BF16).
  if (cudaMemsetAsync(d_routed, 0, static_cast<size_t>(T) * hs * sizeof(float),
                      stream) != cudaSuccess)
    return Status::Fail("memset routed");
  s = MoeBf16RoutedForward(x, d_eid, d_rw, d_routed, routed, routed_ws,
                           gemm_ws, gemm_ws_bytes, T, k, stream);
  if (!s.ok()) return s;
  // 4. shared gate/up.
  auto r1 = model::Bf16Gemm(x, extra.shared_gu, d_gu, T, 2 * shared_is, hs,
                            1.0f, 0.0f, gemm_ws, gemm_ws_bytes, stream);
  if (r1.status != CUBLAS_STATUS_SUCCESS || !r1.has_algo)
    return Status::Fail("shared gu GEMM failed");
  // 5. SwiGLU.
  {
    const int total = T * shared_is;
    SharedSwiGLUKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        d_gu, d_swiglu, T, shared_is);
  }
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("swiglu launch");
  // 6. shared down.
  auto r2 = model::Bf16Gemm(d_swiglu, extra.shared_down, d_shared_down, T, hs,
                            shared_is, 1.0f, 0.0f, gemm_ws, gemm_ws_bytes,
                            stream);
  if (r2.status != CUBLAS_STATUS_SUCCESS || !r2.has_algo)
    return Status::Fail("shared down GEMM failed");
  // 7. combine.
  MoECombineKernel<<<T, kBlock, 0, stream>>>(d_routed, d_shared_down, x,
                                             extra.shared_gate_scalar, y, T,
                                             hs);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("combine launch");
  return Status();
}

}  // namespace mtp
}  // namespace q4t
