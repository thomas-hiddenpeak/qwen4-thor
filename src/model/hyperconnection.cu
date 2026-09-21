// Hyper-Connection (GatedResidual) implementation. See hyperconnection.h.
//
// mix:
//   1. GroupedGemmaRMSNorm(hyper_input) -> normed            (kernel)
//   2. down = normed @ W_down^T            [T, lowrank]      (Bf16Gemm)
//   3. silu(down / hc)                     [T, lowrank]     (kernel)
//   4. up = silu_down @ W_up^T             [T, hc*hs]        (Bf16Gemm)
//   5. gate = sigmoid(up); mixed = mean_b(gate*normed)       (kernel)
// combine:
//   1. inject_raw = normed @ W_inject^T   [T, hc]            (Bf16Gemm)
//   2. 2*sigmoid(inject_raw/hc)           [T, hc]            (kernel)
//   3. out = R + block_output * inject     [T, hc*hs]        (kernel)
#include "q4t/model/hyperconnection.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/model/linear.h"

namespace q4t {
namespace model {

bool HcInjectGevGated(const uint16_t* x, const uint16_t* w, uint16_t* y,
                     cudaStream_t stream);

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

// GroupedGemmaRMSNorm: per-branch (group of `hs`) RMSNorm, then scale by
// (1 + weight). One block per row; each BRANCH is owned by one warp (warp w
// handles branch w) so the hc branches reduce in parallel via warp shuffles
// instead of the old sequential block-wide barrier chain (44 __syncthreads).
// blockDim = 32 * hc. float4-vectorized over hs.
__global__ void GroupedRmsNormKernel(const uint16_t* __restrict__ x,
                                     const uint16_t* __restrict__ weight,
                                     uint16_t* __restrict__ out, int T, int hc,
                                     int hs, float eps) {
  const int t = blockIdx.x;
  if (t >= T) return;
  const int b = threadIdx.x >> 5;  // branch = warp id
  const int lane = threadIdx.x & 31;
  if (b >= hc) return;
  const uint16_t* g = x + (static_cast<size_t>(t) * hc + b) * hs;
  uint16_t* orow = out + (static_cast<size_t>(t) * hc + b) * hs;
  const uint16_t* wg = weight + static_cast<size_t>(b) * hs;
  const float4* gv = reinterpret_cast<const float4*>(g);
  const float4* wv = reinterpret_cast<const float4*>(wg);
  float4* ov = reinterpret_cast<float4*>(orow);
  const int n8 = hs / 8;  // float4 = 16 B = 8 bf16
  // Pass 1: sum of squares for this branch.
  float acc = 0.f;
  for (int i = lane; i < n8; i += 32) {
    const __nv_bfloat162* p = reinterpret_cast<const __nv_bfloat162*>(&gv[i]);
    float2 v0 = __bfloat1622float2(p[0]);
    float2 v1 = __bfloat1622float2(p[1]);
    float2 v2 = __bfloat1622float2(p[2]);
    float2 v3 = __bfloat1622float2(p[3]);
    acc += v0.x * v0.x + v0.y * v0.y + v1.x * v1.x + v1.y * v1.y +
           v2.x * v2.x + v2.y * v2.y + v3.x * v3.x + v3.y * v3.y;
  }
  for (int i = n8 * 8 + lane; i < hs; i += 32) {
    const float v = Bf16ToFloat(g[i]);
    acc += v * v;
  }
#pragma unroll
  for (int off = 16; off > 0; off >>= 1)
    acc += __shfl_xor_sync(0xffffffffu, acc, off);
  const float rs = rsqrtf(acc / hs + eps);
  // Pass 2: normalize + scale, write back.
  for (int i = lane; i < n8; i += 32) {
    const __nv_bfloat162* p = reinterpret_cast<const __nv_bfloat162*>(&gv[i]);
    const __nv_bfloat162* q = reinterpret_cast<const __nv_bfloat162*>(&wv[i]);
    float2 v0 = __bfloat1622float2(p[0]);
    float2 v1 = __bfloat1622float2(p[1]);
    float2 v2 = __bfloat1622float2(p[2]);
    float2 v3 = __bfloat1622float2(p[3]);
    float2 w0 = __bfloat1622float2(q[0]);
    float2 w1 = __bfloat1622float2(q[1]);
    float2 w2 = __bfloat1622float2(q[2]);
    float2 w3 = __bfloat1622float2(q[3]);
    v0.x *= rs * (1.f + w0.x);
    v0.y *= rs * (1.f + w0.y);
    v1.x *= rs * (1.f + w1.x);
    v1.y *= rs * (1.f + w1.y);
    v2.x *= rs * (1.f + w2.x);
    v2.y *= rs * (1.f + w2.y);
    v3.x *= rs * (1.f + w3.x);
    v3.y *= rs * (1.f + w3.y);
    float4 o;
    __nv_bfloat162* op = reinterpret_cast<__nv_bfloat162*>(&o);
    op[0] = __floats2bfloat162_rn(v0.x, v0.y);
    op[1] = __floats2bfloat162_rn(v1.x, v1.y);
    op[2] = __floats2bfloat162_rn(v2.x, v2.y);
    op[3] = __floats2bfloat162_rn(v3.x, v3.y);
    ov[i] = o;
  }
  for (int i = n8 * 8 + lane; i < hs; i += 32) {
    const float v = Bf16ToFloat(g[i]);
    const float w = Bf16ToFloat(wg[i]);
    orow[i] = FloatToBf16(v * rs * (1.f + w));
  }
}

// silu(x / hc) in place on a [T, lowrank] BF16 buffer.
// Matches SGLang _mix_compute: F.silu(F.linear(...) / hc) — divide FIRST,
// then silu (NOT silu(x)/hc; the two differ because silu is nonlinear).
__global__ void SiluDivKernel(uint16_t* __restrict__ x, int total, float inv_hc) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= total) return;
  const float v = Bf16ToFloat(x[i]) * inv_hc;
  x[i] = FloatToBf16(v / (1.0f + __expf(-v)));
}

// gate = sigmoid(up); mixed[t, c] = (1/hc) * sum_b gate[t, b*hs+c] *
// normed[t, b*hs+c]. One thread per (t, c); loops over hc branches.
__global__ void MixGateKernel(const uint16_t* __restrict__ up,
                              const uint16_t* __restrict__ normed,
                              uint16_t* __restrict__ mixed, int T, int hc,
                              int hs) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * hs) return;
  const int t = idx / hs;
  const int c = idx % hs;
  const uint16_t* uprow = up + static_cast<size_t>(t) * hc * hs;
  const uint16_t* nrow = normed + static_cast<size_t>(t) * hc * hs;
  float acc = 0.0f;
  for (int b = 0; b < hc; ++b) {
    const float g = 1.0f / (1.0f + __expf(-Bf16ToFloat(uprow[b * hs + c])));
    acc += g * Bf16ToFloat(nrow[b * hs + c]);
  }
  mixed[idx] = FloatToBf16(acc / hc);
}

// Precompute the inject gate 2*sigmoid(inject_raw/hc) in place, once per
// (t, b). The gate does not depend on the channel c, so folding it out of
// CombineWithGateKernel removes an hs-fold redundant sigmoid recompute there
// (CombineWithGate ran well below MixGate's memory bandwidth despite an almost
// identical access pattern; the per-c gate recompute was the gap). Bit-
// identical: the BF16 round-trip is the same one the fused kernel did in-reg.
__global__ void ApplyInjectGateKernel(uint16_t* __restrict__ inject, int total,
                                      float inv_hc) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= total) return;
  const float v = Bf16ToFloat(inject[i]) * inv_hc;
  inject[i] = FloatToBf16(2.0f / (1.0f + __expf(-v)));
}

// Fused inject-gate + combine: out[t, b*hs+c] = R[t, b*hs+c] +
// block_output[t, c] * gate[t, b], where gate = 2*sigmoid(inject_raw[t,b]/hc)
// was precomputed into inject_gated by ApplyInjectGateKernel (one sigmoid per
// (t,b) instead of per (t,b,c)). The gate went through the same BF16 round-trip
// as the old in-register recompute, so the result is bit-identical.
__global__ void CombineWithGateKernel(
    const uint16_t* __restrict__ block_output, const uint16_t* __restrict__ R,
    const uint16_t* __restrict__ inject_gated, uint16_t* __restrict__ out,
    int T, int hc, int hs) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * hc * hs) return;
  const int t = idx / (hc * hs);
  const int rem = idx % (hc * hs);
  const int b = rem / hs;
  const int c = rem % hs;
  const float inj = Bf16ToFloat(inject_gated[static_cast<size_t>(t) * hc + b]);
  const float r = Bf16ToFloat(R[idx]);
  const float bo = Bf16ToFloat(block_output[static_cast<size_t>(t) * hs + c]);
  out[idx] = FloatToBf16(r + bo * inj);
}

Status CheckGemm(const Bf16GemmResult& r) {
  if (r.status != CUBLAS_STATUS_SUCCESS || !r.has_algo) {
    return Status::Fail(std::string("Bf16Gemm failed (status=") +
                        std::to_string((int)r.status) + ")");
  }
  return Status();
}

Status PrepareInjectGate(const HyperConnectionWeights& w,
                         const uint16_t* normed, uint16_t* inject, int T,
                         void* workspace, size_t workspace_bytes,
                         cudaStream_t stream) {
  const int hc = w.hc_count, hs = w.hidden_size;
  const bool fused_gate = T == 1 && hc == 4 && hs == 2560 &&
                          ((reinterpret_cast<uintptr_t>(normed) |
                            reinterpret_cast<uintptr_t>(w.block_inject)) &
                           15u) == 0;
  if (fused_gate) {
    if (!HcInjectGevGated(normed, w.block_inject, inject, stream))
      return Status::Fail("HC gated projection launch failed");
  } else {
    Status s =
        CheckGemm(Bf16Gemm(normed, w.block_inject, inject, T, hc, hc * hs, 1.0f,
                           0.0f, workspace, workspace_bytes, stream));
    if (!s) return s;
    const int total = T * hc;
    ApplyInjectGateKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        inject, total, 1.0f / hc);
  }
  const cudaError_t error = cudaGetLastError();
  if (error != cudaSuccess) return Status::Fail(cudaGetErrorString(error));
  return {};
}
}  // namespace

void HyperConnectionWeights::Free() {
  if (hc_norm) cudaFree(hc_norm);
  if (mix_down) cudaFree(mix_down);
  if (mix_up) cudaFree(mix_up);
  if (block_inject) cudaFree(block_inject);
  mix_down_fp8.Free();
  mix_up_fp8.Free();
  hc_norm = mix_down = mix_up = block_inject = nullptr;
}

Status LoadHyperConnection(const io::WeightLoader& loader,
                           const std::string& prefix, int hc_count,
                           int hidden_size, int lowrank, float eps,
                           bool use_combine, HyperConnectionWeights* out,
                           cudaStream_t stream) {
  if (hc_count <= 0 || hidden_size <= 0 || lowrank <= 0) {
    return Status::Fail("invalid HC dims");
  }
  out->hc_count = hc_count;
  out->hidden_size = hidden_size;
  out->lowrank = lowrank;
  out->eps = eps;
  out->use_combine = use_combine;
  const int hc_dim = hc_count * hidden_size;

  auto alloc = [](uint16_t** p, size_t bytes) -> Status {
    if (cudaMalloc(reinterpret_cast<void**>(p), bytes) != cudaSuccess) {
      return Status::Fail("cudaMalloc failed");
    }
    return Status();
  };
  auto load = [&loader, stream](const std::string& name, uint16_t* dst,
                                size_t bytes) -> Status {
    std::vector<uint16_t> host(bytes / sizeof(uint16_t));
    Status s = loader.ReadTensor(name, host.data());
    if (!s.ok()) return s;
    if (cudaMemcpyAsync(dst, host.data(), bytes, cudaMemcpyHostToDevice,
                        stream) != cudaSuccess) {
      return Status::Fail("H2D failed");
    }
    return Status();
  };

  Status s;
  if (!(s = alloc(&out->hc_norm, hc_dim * sizeof(uint16_t)))) return s;
  if (!(s = alloc(&out->mix_down,
                  static_cast<size_t>(lowrank) * hc_dim * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->mix_up,
                  static_cast<size_t>(hc_dim) * lowrank * sizeof(uint16_t))))
    return s;
  if (use_combine) {
    if (!(s = alloc(&out->block_inject,
                    static_cast<size_t>(hc_count) * hc_dim * sizeof(
                        uint16_t))))
      return s;
  }

  if (!(s = load(prefix + ".hc_norm.weight", out->hc_norm,
                 hc_dim * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".input_mix_weight_down.weight", out->mix_down,
                 static_cast<size_t>(lowrank) * hc_dim * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".input_mix_weight_up.weight", out->mix_up,
                 static_cast<size_t>(hc_dim) * lowrank * sizeof(uint16_t))))
    return s;
  if (use_combine) {
    if (!(s = load(prefix + ".block_inject_weight.weight", out->block_inject,
                   static_cast<size_t>(hc_count) * hc_dim * sizeof(uint16_t))))
      return s;
  }
  // FP8 (e4m3) decode shadows for the low-rank mix projections (gated by
  // Q4T_FP8_PROJ / Q4T_FP8_HC). block_inject (N=hc) is tiny -> BF16.
  if (!BuildFp8Shadow(out->mix_down, lowrank, hc_dim, &out->mix_down_fp8,
                      Fp8Part::kHc, stream))
    return Status::Fail("mix_down FP8 shadow");
  if (!BuildFp8Shadow(out->mix_up, hc_dim, lowrank, &out->mix_up_fp8,
                      Fp8Part::kHc, stream))
    return Status::Fail("mix_up FP8 shadow");
  if (stream != nullptr &&
      cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("stream sync failed");
  }
  return Status();
}

Status HyperConnectionMix(const HyperConnectionWeights& w,
                          const uint16_t* hyper_input, uint16_t* mixed,
                          uint16_t* normed, int T, void* workspace,
                          size_t workspace_bytes, cudaStream_t stream,
                          const HyperConnectionMixScratch* mix_scratch) {
  const int hc = w.hc_count, hs = w.hidden_size, lr = w.lowrank;
  const int hc_dim = hc * hs;
  if (T <= 0) return Status();
  const float inv_hc = 1.0f / hc;
  const size_t down_bytes = static_cast<size_t>(T) * lr * sizeof(uint16_t);
  const size_t up_bytes = static_cast<size_t>(T) * hc_dim * sizeof(uint16_t);
  if (mix_scratch && (!mix_scratch->down || !mix_scratch->up ||
                      mix_scratch->down_bytes < down_bytes ||
                      mix_scratch->up_bytes < up_bytes)) {
    return Status::Fail("HC mix scratch too small or missing");
  }

  // 1. Grouped RMSNorm (warp-per-branch: blockDim = 32*hc).
  GroupedRmsNormKernel<<<T, 32 * hc, 0, stream>>>(hyper_input, w.hc_norm,
                                                   normed, T, hc, hs, w.eps);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("rmsnorm launch");

  uint16_t* d_down = mix_scratch ? mix_scratch->down : nullptr;
  uint16_t* d_up = mix_scratch ? mix_scratch->up : nullptr;
  if (!mix_scratch) {
    if (cudaMallocAsync(&d_down, down_bytes, stream) != cudaSuccess)
      return Status::Fail("cudaMallocAsync down");
    if (cudaMallocAsync(&d_up, up_bytes, stream) != cudaSuccess) {
      cudaFreeAsync(d_down, stream);
      return Status::Fail("cudaMallocAsync up");
    }
  }
  auto release_owned = [&] {
    if (!mix_scratch) {
      cudaFreeAsync(d_down, stream);
      cudaFreeAsync(d_up, stream);
    }
  };

  // 2. down = normed @ W_down^T  ([T, hc_dim] x [lr, hc_dim]^T -> [T, lr]).
  Status s = CheckGemm(ProjGemm(normed, w.mix_down, &w.mix_down_fp8, d_down, T,
                                lr, hc_dim, 1.0f, 0.0f, workspace,
                                workspace_bytes, stream));
  if (!s.ok()) {
    release_owned();
    return s;
  }
  // 3. silu(down/hc) in place, with BF16 output.
  {
    const int total = T * lr;
    SiluDivKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        d_down, total, inv_hc);
  }
  // 4. up = silu_down @ W_up^T  ([T, lr] x [hc_dim, lr]^T -> [T, hc_dim]).
  s = CheckGemm(ProjGemm(d_down, w.mix_up, &w.mix_up_fp8, d_up, T, hc_dim, lr,
                         1.0f, 0.0f, workspace, workspace_bytes, stream));
  if (!s.ok()) {
    release_owned();
    return s;
  }
  // 5. gate + mean.
  {
    const int total = T * hs;
    MixGateKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        d_up, normed, mixed, T, hc, hs);
  }
  release_owned();
  return Status();
}

GatedResidualFrame::~GatedResidualFrame() { Release(); }

cudaError_t GatedResidualFrame::Release() {
  cudaError_t error = cudaSuccess;
  if (inject_gate_ && owns_gate_) error = cudaFreeAsync(inject_gate_, stream_);
  inject_gate_ = nullptr;
  owns_gate_ = false;
  residual_ = nullptr;
  tokens_ = hc_ = hs_ = 0;
  stream_ = nullptr;
  ready_ = false;
  return error;
}

Status GatedResidualFrame::Read(const HyperConnectionWeights& w,
                                const uint16_t* residual, uint16_t* mixed,
                                uint16_t* normed_scratch, int tokens,
                                void* workspace, size_t workspace_bytes,
                                cudaStream_t stream,
                                const HyperConnectionMixScratch* mix_scratch,
                                const GatedResidualGateStorage* gate_storage) {
  if (ready_ || inject_gate_)
    return Status::Fail("GR frame must be consumed before another Read");
  const bool needs_gate = tokens > 0 && w.use_combine && w.block_inject;
  const size_t gate_bytes =
      needs_gate ? static_cast<size_t>(tokens) * w.hc_count * sizeof(uint16_t)
                 : 0;
  if (needs_gate && gate_storage &&
      (!gate_storage->data || gate_storage->bytes < gate_bytes))
    return Status::Fail("GR gate storage too small or missing");
  Status s =
      HyperConnectionMix(w, residual, mixed, normed_scratch, tokens, workspace,
                         workspace_bytes, stream, mix_scratch);
  if (!s) return s;
  residual_ = residual;
  stream_ = stream;
  tokens_ = tokens;
  hc_ = w.hc_count;
  hs_ = w.hidden_size;
  if (needs_gate) {
    const cudaError_t pending = cudaGetLastError();
    if (pending != cudaSuccess) {
      Release();
      return Status::Fail(cudaGetErrorString(pending));
    }
    if (gate_storage) {
      inject_gate_ = gate_storage->data;
    } else {
      const cudaError_t error =
          cudaMallocAsync(&inject_gate_, gate_bytes, stream_);
      if (error != cudaSuccess) {
        Release();
        return Status::Fail(cudaGetErrorString(error));
      }
      owns_gate_ = true;
    }
    s = PrepareInjectGate(w, normed_scratch, inject_gate_, tokens, workspace,
                          workspace_bytes, stream_);
    if (!s) {
      Release();
      return s;
    }
  }
  ready_ = true;
  return {};
}

Status GatedResidualFrame::Write(const uint16_t* block_output,
                                 uint16_t* output) {
  if (!ready_) return Status::Fail("GR Write requires a ready frame");
  cudaError_t error = cudaSuccess;
  if (tokens_ > 0) {
    if (inject_gate_) {
      const int total = tokens_ * hc_ * hs_;
      CombineWithGateKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0,
                              stream_>>>(block_output, residual_, inject_gate_,
                                         output, tokens_, hc_, hs_);
      error = cudaGetLastError();
    } else {
      error = cudaMemcpyAsync(output, residual_,
                              static_cast<size_t>(tokens_) * hc_ * hs_ * 2,
                              cudaMemcpyDeviceToDevice, stream_);
    }
  }
  const cudaError_t release_error = Release();
  if (error != cudaSuccess) return Status::Fail(cudaGetErrorString(error));
  if (release_error != cudaSuccess)
    return Status::Fail(cudaGetErrorString(release_error));
  return {};
}
Status HyperConnectionCombine(const HyperConnectionWeights& w,
                              const uint16_t* block_output,
                              const uint16_t* hyper_input,
                              const uint16_t* normed, uint16_t* out, int T,
                              void* workspace, size_t workspace_bytes,
                              cudaStream_t stream) {
  const int hc = w.hc_count, hs = w.hidden_size;
  const int hc_dim = hc * hs;
  if (T <= 0) return Status();
  if (!w.use_combine || w.block_inject == nullptr) {
    // No combine (mixer): pass the residual through unchanged.
    if (cudaMemcpyAsync(out, hyper_input, static_cast<size_t>(T) * hc_dim *
                                              sizeof(uint16_t),
                        cudaMemcpyDeviceToDevice, stream) != cudaSuccess) {
      return Status::Fail("D2D copy failed");
    }
    return Status();
  }
  // Check for a pending async error from a prior kernel (e.g. an illegal
  // memory access in the attention path). A tiny 8-byte cudaMalloc failing
  // with 37 GB available is a classic sign of a poisoned context, not a
  // genuine OOM.
  const cudaError_t perr = cudaGetLastError();
  if (perr != cudaSuccess) {
    return Status::Fail(std::string("pending CUDA error before cudaMalloc "
                                    "inject: ") +
                        cudaGetErrorString(perr));
  }
  uint16_t* d_inject = nullptr;  // [T, hc]
  const cudaError_t merr =
      cudaMallocAsync(&d_inject, static_cast<size_t>(T) * hc * sizeof(uint16_t),
                      stream);
  if (merr != cudaSuccess) {
    return Status::Fail(std::string("cudaMallocAsync inject: ") +
                        cudaGetErrorString(merr));
  }
  Status s = PrepareInjectGate(w, normed, d_inject, T, workspace,
                               workspace_bytes, stream);
  if (!s) {
    cudaFreeAsync(d_inject, stream);
    return s;
  }
  const int total = T * hc_dim;
  CombineWithGateKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
      block_output, hyper_input, d_inject, out, T, hc, hs);
  cudaFreeAsync(d_inject, stream);
  return Status();
}

}  // namespace model
}  // namespace q4t
