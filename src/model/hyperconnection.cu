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
// (1 + weight). One block per row; each group reduced independently.
__global__ void GroupedRmsNormKernel(const uint16_t* __restrict__ x,
                                     const uint16_t* __restrict__ weight,
                                     uint16_t* __restrict__ out, int T, int hc,
                                     int hs, float eps) {
  const int t = blockIdx.x;
  if (t >= T) return;
  const uint16_t* row = x + static_cast<size_t>(t) * hc * hs;
  uint16_t* orow = out + static_cast<size_t>(t) * hc * hs;
  __shared__ float s_part[kBlock];
  for (int b = 0; b < hc; ++b) {
    const uint16_t* g = row + b * hs;
    float acc = 0.0f;
    for (int i = threadIdx.x; i < hs; i += blockDim.x) {
      const float v = Bf16ToFloat(g[i]);
      acc += v * v;
    }
    s_part[threadIdx.x] = acc;
    __syncthreads();
    for (int off = blockDim.x / 2; off > 0; off >>= 1) {
      if (threadIdx.x < off) s_part[threadIdx.x] += s_part[threadIdx.x + off];
      __syncthreads();
    }
    const float rs = rsqrtf(s_part[0] / hs + eps);
    __syncthreads();
    for (int i = threadIdx.x; i < hs; i += blockDim.x) {
      const float v = Bf16ToFloat(g[i]);
      const float w = Bf16ToFloat(weight[b * hs + i]);
      orow[b * hs + i] = FloatToBf16(v * rs * (1.0f + w));
    }
    __syncthreads();
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

// inject = 2*sigmoid(inject_raw/hc) in place on [T, hc] BF16.
__global__ void InjectGateKernel(uint16_t* __restrict__ inject, int T, int hc,
                                 float inv_hc) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= T * hc) return;
  const float v = Bf16ToFloat(inject[i]) * inv_hc;
  inject[i] = FloatToBf16(2.0f / (1.0f + __expf(-v)));
}

// out[t, b*hs+c] = R[t, b*hs+c] + block_output[t, c] * inject[t, b].
__global__ void CombineKernel(const uint16_t* __restrict__ block_output,
                              const uint16_t* __restrict__ R,
                              const uint16_t* __restrict__ inject,
                              uint16_t* __restrict__ out, int T, int hc, int hs) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * hc * hs) return;
  const int t = idx / (hc * hs);
  const int rem = idx % (hc * hs);
  const int b = rem / hs;
  const int c = rem % hs;
  const float r = Bf16ToFloat(R[idx]);
  const float bo = Bf16ToFloat(block_output[static_cast<size_t>(t) * hs + c]);
  const float inj = Bf16ToFloat(inject[static_cast<size_t>(t) * hc + b]);
  out[idx] = FloatToBf16(r + bo * inj);
}

Status CheckGemm(const Bf16GemmResult& r) {
  if (r.status != CUBLAS_STATUS_SUCCESS || !r.has_algo) {
    return Status::Fail(std::string("Bf16Gemm failed (status=") +
                        std::to_string((int)r.status) + ")");
  }
  return Status();
}

}  // namespace

void HyperConnectionWeights::Free() {
  if (hc_norm) cudaFree(hc_norm);
  if (mix_down) cudaFree(mix_down);
  if (mix_up) cudaFree(mix_up);
  if (block_inject) cudaFree(block_inject);
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
  if (stream != nullptr &&
      cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("stream sync failed");
  }
  return Status();
}

Status HyperConnectionMix(const HyperConnectionWeights& w,
                          const uint16_t* hyper_input, uint16_t* mixed,
                          uint16_t* normed, int T, void* workspace,
                          size_t workspace_bytes, cudaStream_t stream) {
  const int hc = w.hc_count, hs = w.hidden_size, lr = w.lowrank;
  const int hc_dim = hc * hs;
  if (T <= 0) return Status();
  const float inv_hc = 1.0f / hc;

  // 1. Grouped RMSNorm.
  GroupedRmsNormKernel<<<T, kBlock, 0, stream>>>(hyper_input, w.hc_norm, normed,
                                                 T, hc, hs, w.eps);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("rmsnorm launch");

  uint16_t* d_down = nullptr;  // [T, lr]
  uint16_t* d_up = nullptr;  // [T, hc_dim]
  if (cudaMalloc(&d_down, static_cast<size_t>(T) * lr * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc down");
  if (cudaMalloc(&d_up, static_cast<size_t>(T) * hc_dim * sizeof(uint16_t)) !=
      cudaSuccess) {
    cudaFree(d_down);
    return Status::Fail("cudaMalloc up");
  }

  // 2. down = normed @ W_down^T  ([T, hc_dim] x [lr, hc_dim]^T -> [T, lr]).
  Status s = CheckGemm(Bf16Gemm(normed, w.mix_down, d_down, T, lr, hc_dim,
                                1.0f, 0.0f, workspace, workspace_bytes, stream));
  if (!s.ok()) {
    cudaFree(d_down);
    cudaFree(d_up);
    return s;
  }
  // 3. silu(down)/hc in place.
  {
    const int total = T * lr;
    SiluDivKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        d_down, total, inv_hc);
  }
  // 4. up = silu_down @ W_up^T  ([T, lr] x [hc_dim, lr]^T -> [T, hc_dim]).
  s = CheckGemm(Bf16Gemm(d_down, w.mix_up, d_up, T, hc_dim, lr, 1.0f, 0.0f,
                         workspace, workspace_bytes, stream));
  if (!s.ok()) {
    cudaFree(d_down);
    cudaFree(d_up);
    return s;
  }
  // 5. gate + mean.
  {
    const int total = T * hs;
    MixGateKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        d_up, normed, mixed, T, hc, hs);
  }
  cudaFree(d_down);
  cudaFree(d_up);
  return Status();
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
  const float inv_hc = 1.0f / hc;
  uint16_t* d_inject = nullptr;  // [T, hc]
  if (cudaMalloc(&d_inject, static_cast<size_t>(T) * hc * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc inject");
  // 1. inject_raw = normed @ W_inject^T  ([T, hc_dim] x [hc, hc_dim]^T -> [T,
  // hc]).
  Status s = CheckGemm(Bf16Gemm(normed, w.block_inject, d_inject, T, hc,
                                hc_dim, 1.0f, 0.0f, workspace, workspace_bytes,
                                stream));
  if (!s.ok()) {
    cudaFree(d_inject);
    return s;
  }
  // 2. 2*sigmoid(inject/hc) in place.
  {
    const int total = T * hc;
    InjectGateKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        d_inject, T, hc, inv_hc);
  }
  // 3. out = R + block_output * inject.
  {
    const int total = T * hc_dim;
    CombineKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        block_output, hyper_input, d_inject, out, T, hc, hs);
  }
  cudaFree(d_inject);
  return Status();
}

}  // namespace model
}  // namespace q4t
