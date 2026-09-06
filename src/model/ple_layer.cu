// PLE layer forward implementation. See ple_layer.h.
//
//   1. key   = embeddings @ key_proj^T    [T, hc*hs]   (Bf16Gemm)
//   2. value = embeddings @ value_proj^T  [T, hs]       (Bf16Gemm)
//   3. key_n   = GroupedGemmaRMSNorm(key, norm_key)     (kernel)
//   4. query_n = GroupedGemmaRMSNorm(hyper_input, norm_query) (kernel)
//   5. gate = sigmoid(sqrt(|dot(key_n, query_n)/sqrt(hs)|))  [T, hc] (kernel)
//   6. gated_value[b,c] = gate[b] * value[c]              [T, hc*hs] (kernel)
//   7. gated_n = GroupedGemmaRMSNorm(gated_value, norm_conv) (kernel)
//   8. conv_out = silu(depthwise-causal-conv(gated_n))    [T, hc*hs] (kernel)
//   9. out = gated_value + conv_out                       (kernel)
#include "q4t/model/ple_layer.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/model/linear.h"

namespace q4t {
namespace model {

namespace {

constexpr int kBlock = 256;
constexpr size_t kGemmScratch = 32 * 1024 * 1024;  // 32 MiB cuBLASLt scratch

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
__device__ __forceinline__ float Silu(float v) {
  return v / (1.0f + __expf(-v));
}

// GroupedGemmaRMSNorm: per-branch (group of `hs`) RMSNorm, then scale by
// (1 + weight). One block per row; each group reduced independently.
// (Same op as the hyper-connection norm; re-implemented here to keep this
// translation unit self-contained.)
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

// gate[t, b] = sigmoid( sqrt(|raw|) * sign(raw) ),
// raw = (sum_c key_n[t, b*hs+c] * query_n[t, b*hs+c]) / sqrt(hs).
// One thread per (t, b); loops over hs.
__global__ void PleGateKernel(const uint16_t* __restrict__ key_n,
                              const uint16_t* __restrict__ query_n,
                              uint16_t* __restrict__ gate, int T, int hc,
                              int hs, float inv_sqrt_hs) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * hc) return;
  const int t = idx / hc;
  const int b = idx % hc;
  const uint16_t* k = key_n + static_cast<size_t>(t) * hc * hs + b * hs;
  const uint16_t* q = query_n + static_cast<size_t>(t) * hc * hs + b * hs;
  float acc = 0.0f;
  for (int c = 0; c < hs; ++c) {
    acc += Bf16ToFloat(k[c]) * Bf16ToFloat(q[c]);
  }
  float raw = acc * inv_sqrt_hs;
  float g = sqrtf(fabsf(raw) + 1e-6f) * (raw < 0.0f ? -1.0f : 1.0f);
  gate[idx] = FloatToBf16(1.0f / (1.0f + __expf(-g)));
}

// gated_value[t, b*hs+c] = gate[t, b] * value[t, c]. One thread per element;
// value is broadcast over the hc branches.
__global__ void GatedValueKernel(const uint16_t* __restrict__ gate,
                                 const uint16_t* __restrict__ value,
                                 uint16_t* __restrict__ out, int T, int hc,
                                 int hs) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * hc * hs) return;
  const int t = idx / (hc * hs);
  const int rem = idx % (hc * hs);
  const int b = rem / hs;
  const int c = rem % hs;
  const float g = Bf16ToFloat(gate[t * hc + b]);
  const float v = Bf16ToFloat(value[t * hs + c]);
  out[idx] = FloatToBf16(g * v);
}

// Depthwise causal convolution over the token sequence, then SiLU.
//   conv_out[t, c] = silu( sum_{j=0}^{K-1} conv1d[c, j] *
//                          gated_n[t - (K-1-j)*dilation, c] )
// Taps before the sequence start (src < 0) read from the persistent
// `state` ([C, state_len] BF16, oldest-first, state_len = (K-1)*dilation)
// instead of zero-padding: state[c, src + state_len] is the gated_n value at
// global position src (the reference _short_conv keeps this 9-element state
// via update_conv_state). For a fresh sequence the state is zero, so this
// reduces to zero-padding (the prefill path is unchanged).
// One thread per (t, c); loops over K taps.
__global__ void DepthwiseConvKernel(const uint16_t* __restrict__ x,
                                    const uint16_t* __restrict__ conv1d,
                                    const uint16_t* __restrict__ state,
                                    uint16_t* __restrict__ out, int T, int C,
                                    int K, int dilation, int state_len) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * C) return;
  const int t = idx / C;
  const int c = idx % C;
  float acc = 0.0f;
  for (int j = 0; j < K; ++j) {
    const int src = t - (K - 1 - j) * dilation;
    float xv;
    if (src < 0)
      xv = Bf16ToFloat(state[static_cast<size_t>(c) * state_len + (src + state_len)]);
    else
      xv = Bf16ToFloat(x[static_cast<size_t>(src) * C + c]);
    const float w = Bf16ToFloat(conv1d[c * K + j]);
    acc += w * xv;
  }
  out[idx] = FloatToBf16(Silu(acc));
}

// Update the PLE short-conv state to the last `state_len` gated_n values of
// (old state + this chunk). Mirrors Conv1dUpdateStateKernel in
// linear_attention.cu, but with state_len = (K-1)*dilation (9 for K=4, d=3)
// instead of conv_k-1.
//
//   T >= state_len: state[k] = input[T - state_len + k]  (whole window from
//              the chunk; the prefill path).
//   T <  state_len (DECODE, e.g. T = 1): the window slides by T. Let
//              shift = state_len - T.
//                state[k] = state[k + T]      for k <  shift  (slide the old
//                                            window; reads a higher index,
//                                            so ascending k is in-place safe)
//                state[k] = input[k - shift]  for k >= shift  (the new tokens)
//              e.g. [0,0,0,0,0,g0,g1,g2,g3] + g4 -> [0,0,0,0,g0,g1,g2,g3,g4].
__global__ void PleConvUpdateStateKernel(uint16_t* __restrict__ state,
                                         const uint16_t* __restrict__ input,
                                         int T, int C, int state_len) {
  const int c = blockIdx.x * blockDim.x + threadIdx.x;
  if (c >= C) return;
  const int shift = state_len - T;  // > 0 only in the decode path (T < state_len)
  if (shift > 0) {
    for (int k = 0; k < shift; ++k)
      state[static_cast<size_t>(c) * state_len + k] =
          state[static_cast<size_t>(c) * state_len + k + T];
    for (int k = shift; k < state_len; ++k)
      state[static_cast<size_t>(c) * state_len + k] =
          input[static_cast<size_t>(k - shift) * C + c];
  } else {
    for (int k = 0; k < state_len; ++k) {
      const int src = T - state_len + k;
      if (src >= 0)
        state[static_cast<size_t>(c) * state_len + k] =
            input[static_cast<size_t>(src) * C + c];
    }
  }
}

// out[t, c] = gated_value[t, c] + conv_out[t, c].
__global__ void PleAddKernel(const uint16_t* __restrict__ a,
                             const uint16_t* __restrict__ b, uint16_t* __restrict__ out,
                             int total) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= total) return;
  out[i] = FloatToBf16(Bf16ToFloat(a[i]) + Bf16ToFloat(b[i]));
}

Status CheckGemm(const Bf16GemmResult& r) {
  if (r.status != CUBLAS_STATUS_SUCCESS || !r.has_algo) {
    return Status::Fail(std::string("Bf16Gemm failed (status=") +
                        std::to_string((int)r.status) + ")");
  }
  return Status();
}

// Debug: when Q4T_MLP_DUMP=<tag>, copy the PLE short-conv INPUT (gated_n,
// [T, hc_dim] BF16) and OUTPUT (d_conv, [T, hc_dim] BF16) to
// <tag>.{ple_gated_n,ple_conv_out}.bin. This isolates the PLE conv: if
// ple_gated_n is identical between the batch and incremental paths but
// ple_conv_out diverges, the divergence is the conv's missing left context
// (the C++ DepthwiseConvKernel zero-pads src<0 and has NO persistent state,
// whereas the reference _short_conv keeps a 9-element state via
// update_conv_state). Only the PLE layer calls this, so no layer index.
void DumpPleConv(const uint16_t* gated_n, const uint16_t* conv_out, int T,
                 int hc_dim) {
  const char* e = std::getenv("Q4T_MLP_DUMP");
  if (!e || !*e) return;
  auto w = [&](const char* name, const uint16_t* src) {
    std::string p = std::string(e) + "." + name + ".bin";
    std::vector<uint16_t> h(static_cast<size_t>(T) * hc_dim);
    cudaMemcpy(h.data(), src, h.size() * sizeof(uint16_t),
               cudaMemcpyDeviceToHost);
    FILE* f = std::fopen(p.c_str(), "wb");
    if (f) {
      std::fwrite(h.data(), sizeof(uint16_t), h.size(), f);
      std::fclose(f);
    }
  };
  w("ple_gated_n", gated_n);
  w("ple_conv_out", conv_out);
}

inline size_t AlignUp(size_t n, size_t a) { return (n + a - 1) & ~(a - 1); }

}  // namespace

size_t PleLayerWorkspaceBytes(int T, int hc, int hs) {
  const int hc_dim = hc * hs;
  const size_t hc_bytes = static_cast<size_t>(T) * hc_dim * sizeof(uint16_t);
  const size_t hs_bytes = static_cast<size_t>(T) * hs * sizeof(uint16_t);
  const size_t gate_bytes = static_cast<size_t>(T) * hc * sizeof(uint16_t);
  // Mirror the carve in PleLayerForward exactly (each offset AlignUp(256)).
  size_t off = 0;
  auto carve = [&](size_t bytes) {
    off = AlignUp(off, 256);
    off += bytes;
  };
  carve(hc_bytes);  // key
  carve(hs_bytes);  // value
  carve(hc_bytes);  // key_n
  carve(hc_bytes);  // query_n
  carve(gate_bytes);  // gate
  carve(hc_bytes);  // gated
  carve(hc_bytes);  // gated_n
  carve(hc_bytes);  // conv
  carve(kGemmScratch);  // gemm scratch
  return off;
}

void PleLayerWeights::Free() {
  if (key_proj) cudaFree(key_proj);
  if (value_proj) cudaFree(value_proj);
  if (norm_key) cudaFree(norm_key);
  if (norm_query) cudaFree(norm_query);
  if (norm_conv) cudaFree(norm_conv);
  if (conv1d) cudaFree(conv1d);
  key_proj = value_proj = norm_key = norm_query = norm_conv = conv1d = nullptr;
}

Status LoadPleLayer(const io::WeightLoader& loader, const std::string& prefix,
                    int hc_count, int hidden_size, int ple_embed_dim,
                    int conv_kernel, int conv_dilation, float eps,
                    PleLayerWeights* out, cudaStream_t stream) {
  if (hc_count <= 0 || hidden_size <= 0 || ple_embed_dim <= 0 ||
      conv_kernel <= 0) {
    return Status::Fail("invalid PLE dims");
  }
  out->hc_count = hc_count;
  out->hidden_size = hidden_size;
  out->ple_embed_dim = ple_embed_dim;
  out->conv_kernel = conv_kernel;
  out->conv_dilation = conv_dilation;
  out->eps = eps;
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
  if (!(s = alloc(&out->key_proj,
                  static_cast<size_t>(hc_dim) * ple_embed_dim * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->value_proj,
                  static_cast<size_t>(hidden_size) * ple_embed_dim * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->norm_key, hc_dim * sizeof(uint16_t)))) return s;
  if (!(s = alloc(&out->norm_query, hc_dim * sizeof(uint16_t)))) return s;
  if (!(s = alloc(&out->norm_conv, hc_dim * sizeof(uint16_t)))) return s;
  if (!(s = alloc(&out->conv1d,
                  static_cast<size_t>(hc_dim) * conv_kernel * sizeof(uint16_t))))
    return s;

  if (!(s = load(prefix + ".key_proj.weight", out->key_proj,
                 static_cast<size_t>(hc_dim) * ple_embed_dim * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".value_proj.weight", out->value_proj,
                 static_cast<size_t>(hidden_size) * ple_embed_dim * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".norm_key.weight", out->norm_key,
                 hc_dim * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".norm_query.weight", out->norm_query,
                 hc_dim * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".norm_conv.weight", out->norm_conv,
                 hc_dim * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".conv1d.weight", out->conv1d,
                 static_cast<size_t>(hc_dim) * conv_kernel * sizeof(uint16_t))))
    return s;
  if (stream != nullptr &&
      cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("stream sync failed");
  }
  return Status();
}

Status PleLayerForward(const PleLayerWeights& w, const uint16_t* embeddings,
                       const uint16_t* hyper_input, uint16_t* out, int T,
                       uint16_t* conv_state, void* workspace,
                       size_t workspace_bytes, cudaStream_t stream) {
  const int hc = w.hc_count, hs = w.hidden_size, pe = w.ple_embed_dim;
  const int hc_dim = hc * hs;
  const int K = w.conv_kernel, dil = w.conv_dilation;
  if (T <= 0) return Status();
  const float inv_sqrt_hs = 1.0f / std::sqrt(static_cast<float>(hs));

  // Carve the single workspace (256-byte aligned offsets; cuBLASLt requires
  // aligned scratch pointers).
  char* base = static_cast<char*>(workspace);
  size_t off = 0;
  auto carve = [&](size_t bytes) -> char* {
    off = AlignUp(off, 256);
    char* p = base + off;
    off += bytes;
    return p;
  };
  const size_t hc_bytes = static_cast<size_t>(T) * hc_dim * sizeof(uint16_t);
  const size_t hs_bytes = static_cast<size_t>(T) * hs * sizeof(uint16_t);
  const size_t gate_bytes = static_cast<size_t>(T) * hc * sizeof(uint16_t);
  char* d_key = carve(hc_bytes);
  char* d_value = carve(hs_bytes);
  char* d_key_n = carve(hc_bytes);
  char* d_query_n = carve(hc_bytes);
  char* d_gate = carve(gate_bytes);
  char* d_gated = carve(hc_bytes);
  char* d_gated_n = carve(hc_bytes);
  char* d_conv = carve(hc_bytes);
  char* d_gemm = carve(kGemmScratch);
  if (off > workspace_bytes) {
    return Status::Fail("PLE workspace too small");
  }

  // 1. key = embeddings @ key_proj^T  [T, pe] x [hc_dim, pe]^T -> [T, hc_dim].
  Status s = CheckGemm(Bf16Gemm(embeddings, w.key_proj,
                                reinterpret_cast<uint16_t*>(d_key), T, hc_dim,
                                pe, 1.0f, 0.0f, d_gemm, kGemmScratch, stream));
  if (!s.ok()) return s;
  // 2. value = embeddings @ value_proj^T  [T, pe] x [hs, pe]^T -> [T, hs].
  s = CheckGemm(Bf16Gemm(embeddings, w.value_proj,
                         reinterpret_cast<uint16_t*>(d_value), T, hs, pe, 1.0f,
                         0.0f, d_gemm, kGemmScratch, stream));
  if (!s.ok()) return s;
  // 3. key_n = GroupedGemmaRMSNorm(key, norm_key).
  GroupedRmsNormKernel<<<T, kBlock, 0, stream>>>(
      reinterpret_cast<uint16_t*>(d_key), w.norm_key,
      reinterpret_cast<uint16_t*>(d_key_n), T, hc, hs, w.eps);
  // 4. query_n = GroupedGemmaRMSNorm(hyper_input, norm_query).
  GroupedRmsNormKernel<<<T, kBlock, 0, stream>>>(
      hyper_input, w.norm_query, reinterpret_cast<uint16_t*>(d_query_n), T, hc,
      hs, w.eps);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("ple rmsnorm");
  // 5. gate.
  {
    const int total = T * hc;
    PleGateKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        reinterpret_cast<uint16_t*>(d_key_n),
        reinterpret_cast<uint16_t*>(d_query_n),
        reinterpret_cast<uint16_t*>(d_gate), T, hc, hs, inv_sqrt_hs);
  }
  // 6. gated_value.
  {
    const int total = T * hc_dim;
    GatedValueKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        reinterpret_cast<uint16_t*>(d_gate),
        reinterpret_cast<uint16_t*>(d_value),
        reinterpret_cast<uint16_t*>(d_gated), T, hc, hs);
  }
  // 7. gated_n = GroupedGemmaRMSNorm(gated_value, norm_conv).
  GroupedRmsNormKernel<<<T, kBlock, 0, stream>>>(
      reinterpret_cast<uint16_t*>(d_gated), w.norm_conv,
      reinterpret_cast<uint16_t*>(d_gated_n), T, hc, hs, w.eps);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("ple rmsnorm2");
  // 8. conv_out = silu(depthwise-causal-conv(gated_n)). The dilated conv
  //    (K, dilation) has receptive field (K-1)*dilation, so the persistent
  //    state holds that many gated_n values per channel (oldest-first).
  const int state_len = (K - 1) * dil;
  {
    const int total = T * hc_dim;
    DepthwiseConvKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        reinterpret_cast<uint16_t*>(d_gated_n), w.conv1d, conv_state,
        reinterpret_cast<uint16_t*>(d_conv), T, hc_dim, K, dil, state_len);
  }
  DumpPleConv(reinterpret_cast<uint16_t*>(d_gated_n),
              reinterpret_cast<uint16_t*>(d_conv), T, hc_dim);
  // 8b. Slide the state to the last state_len gated_n values (old + chunk).
  {
    const int blocks = (hc_dim + kBlock - 1) / kBlock;
    PleConvUpdateStateKernel<<<blocks, kBlock, 0, stream>>>(
        conv_state, reinterpret_cast<uint16_t*>(d_gated_n), T, hc_dim,
        state_len);
  }
  // 9. out = gated_value + conv_out.
  {
    const int total = T * hc_dim;
    PleAddKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        reinterpret_cast<uint16_t*>(d_gated),
        reinterpret_cast<uint16_t*>(d_conv), out, total);
  }
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("ple kernel");
  return Status();
}

}  // namespace model
}  // namespace q4t
