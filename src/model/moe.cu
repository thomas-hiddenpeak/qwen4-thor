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

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/model/linear.h"
#include "q4t/quant/moe_gemm.h"

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
__device__ __forceinline__ uint16_t FloatToBf16(float f) {
  const __nv_bfloat16 b = __float2bfloat16_rn(f);
  return *reinterpret_cast<const uint16_t*>(&b);
}

// Top-k selection + softmax over the selected k logits. One block per token;
// thread 0 does the sequential scan (E is small, 512).
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
                  size_t gemm_ws_bytes, cudaStream_t stream) {
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
  // 3. routed experts (NVFP4).
  if (cudaMemsetAsync(d_routed, 0, static_cast<size_t>(T) * hs * sizeof(float),
                      stream) != cudaSuccess)
    return Status::Fail("memset routed");
  s = quant::MoERoutedForward(x, d_eid, d_rw, d_routed, routed, routed_ws,
                              gemm_ws, gemm_ws_bytes, T, k, stream);
  if (!s.ok()) return s;
  // 4. shared gate/up.
  s = CheckGemm(Bf16Gemm(x, extra.shared_gu, d_gu, T, 2 * shared_is, hs, 1.0f,
                         0.0f, gemm_ws, gemm_ws_bytes, stream));
  if (!s.ok()) return s;
  // 5. SwiGLU.
  {
    const int total = T * shared_is;
    SwiGLUKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        d_gu, d_swiglu, T, shared_is);
  }
  // 6. shared down.
  s = CheckGemm(Bf16Gemm(d_swiglu, extra.shared_down, d_shared_down, T, hs,
                         shared_is, 1.0f, 0.0f, gemm_ws, gemm_ws_bytes, stream));
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
