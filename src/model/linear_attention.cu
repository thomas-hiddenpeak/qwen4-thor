// Linear attention (Gated DeltaNet SSM) implementation. See
// linear_attention.h for the math. Kernels mirror the qwen35-thor reference
// (reference/qwen35-thor/src/engine/light_ops.cu): a causal conv1d (SiLU) over
// the in_qkv channels, the Gated DeltaNet recurrence (S held in shared memory,
// one block per value head), a fused per-head RMSNorm * sigmoid(z) gate
// (output_gate_type), and the output projection.
#include "q4t/model/linear_attention.h"

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
__device__ __forceinline__ float Sigmoid(float v) {
  return 1.0f / (1.0f + __expf(-v));
}

// Block-wide sum reduction over blockDim.x threads (result in shared slot 0).
// `s` must hold at least blockDim.x floats.
__device__ __forceinline__ float BlockReduceSum(float val, float* s) {
  s[threadIdx.x] = val;
  __syncthreads();
  for (int off = blockDim.x / 2; off > 0; off >>= 1) {
    if (threadIdx.x < off) s[threadIdx.x] += s[threadIdx.x + off];
    __syncthreads();
  }
  return s[0];
}

Status CheckGemm(const Bf16GemmResult& r) {
  if (r.status != CUBLAS_STATUS_SUCCESS || !r.has_algo) {
    return Status::Fail(std::string("Bf16Gemm failed (status=") +
                        std::to_string((int)r.status) + ")");
  }
  return Status();
}

// Debug: when Q4T_LIN_DUMP=<tag>, copy the linear-attention intermediates of
// this call to <tag>_m<idx>.{x,qkv_raw,qkv,y_ssm}.bin (BF16, row-major) so a
// prefill vs decode comparison can find the first diverging stage. No-op
// unless the env var is set.
//
// Each forward calls LinearAttentionForward once per linear layer, in layer
// order, so idx (0-based, reset when the tag changes) is the linear-layer
// index. Without this, every layer overwrote the same <tag>.{...}.bin and the
// LAST linear layer won — which silently mislabeled layer-0 intermediates as
// the final layer's (a real artifact that corrupted an earlier localization).
void DumpLinearIntermediates(const char* tag, const uint16_t* x, int T,
                             int hs, const uint16_t* qkv_raw,
                             const uint16_t* qkv, const uint16_t* y_ssm,
                             int in_qkv, int v_dim) {
  const char* e = std::getenv("Q4T_LIN_DUMP");
  if (!e || !*e) return;
  static std::string last_tag;
  static int idx = 0;
  if (std::string(e) != last_tag) {
    last_tag = e;
    idx = 0;
  }
  const int li = idx++;
  const std::string base = std::string(e) + "_m" + std::to_string(li);
  auto w = [&](const char* name, const uint16_t* src, size_t n) {
    std::string p = base + "." + name + ".bin";
    std::vector<uint16_t> h(n);
    cudaMemcpy(h.data(), src, n * sizeof(uint16_t), cudaMemcpyDeviceToHost);
    FILE* f = std::fopen(p.c_str(), "wb");
    if (f) {
      std::fwrite(h.data(), sizeof(uint16_t), n, f);
      std::fclose(f);
    }
  };
  w("x", x, static_cast<size_t>(T) * hs);
  w("qkv_raw", qkv_raw, static_cast<size_t>(T) * in_qkv);
  w("qkv", qkv, static_cast<size_t>(T) * in_qkv);
  w("y_ssm", y_ssm, static_cast<size_t>(T) * v_dim);
  (void)tag;
}

// Causal conv1d over `channels` (the in_qkv channels), kernel width conv_k,
// SiLU activation. Prefill-parallel: all tokens computed at once from the
// input + the persistent conv_state (history for t < conv_k-1).
//   input  : [T, channels] (original, unmodified)
//   output : [T, channels] (SiLU(conv)); may alias input (per-(t,ch) safe)
//   state  : [channels, conv_k-1] (last conv_k-1 inputs, oldest first)
//   w      : [channels, conv_k]
__global__ void CausalConv1dKernel(const uint16_t* __restrict__ input,
                                   uint16_t* __restrict__ output,
                                   uint16_t* __restrict__ state,
                                   const uint16_t* __restrict__ w, int T,
                                   int channels, int conv_k) {
  const int ch = blockIdx.x * blockDim.x + threadIdx.x;
  const int t = blockIdx.y;
  if (ch >= channels || t >= T) return;
  const int hist = conv_k - 1;
  float wv[4];
#pragma unroll
  for (int k = 0; k < 4; ++k)
    wv[k] = (k < conv_k) ? Bf16ToFloat(w[ch * conv_k + k]) : 0.0f;
  float acc = 0.0f;
#pragma unroll
  for (int k = 0; k < 4; ++k) {
    if (k >= conv_k) break;
    const int src_t = t - (hist - k);
    float val;
    if (src_t < 0)
      val = Bf16ToFloat(state[ch * hist + (src_t + hist)]);
    else
      val = Bf16ToFloat(input[static_cast<size_t>(src_t) * channels + ch]);
    acc += val * wv[k];
  }
  output[static_cast<size_t>(t) * channels + ch] = FloatToBf16(Silu(acc));
}

// Update conv_state to the last `hist` inputs of (old history + chunk).
//
//   T >= hist: state[k] = input[T - hist + k]  (whole window from the chunk;
//              the prefill path, unchanged).
//   T <  hist (DECODE, e.g. T = 1): the window slides by T. Let
//              shift = hist - T.
//                state[k] = state[k + T]      for k <  shift  (slide the old
//                                            window; reads a higher index,
//                                            so ascending k is in-place safe)
//                state[k] = input[k - shift]  for k >= shift  (the new tokens)
//              e.g. [a,b,c] + d -> [b,c,d]. The old behavior wrote only the
//              last slot, leaving [a,b,d] — a stale entry that leaks the first
//              prefill token into every later step's conv window.
__global__ void Conv1dUpdateStateKernel(uint16_t* __restrict__ state,
                                        const uint16_t* __restrict__ input,
                                        int T, int channels, int conv_k) {
  const int ch = blockIdx.x * blockDim.x + threadIdx.x;
  if (ch >= channels) return;
  const int hist = conv_k - 1;
  const int shift = hist - T;  // > 0 only in the decode path (T < hist)
  if (shift > 0) {
    for (int k = 0; k < shift; ++k)
      state[ch * hist + k] = state[ch * hist + k + T];
    for (int k = shift; k < hist; ++k)
      state[ch * hist + k] =
          input[static_cast<size_t>(k - shift) * channels + ch];
  } else {
    for (int k = 0; k < hist; ++k) {
      const int src_t = T - hist + k;
      if (src_t >= 0)
        state[ch * hist + k] =
            input[static_cast<size_t>(src_t) * channels + ch];
    }
  }
}

// Build per-token conv-state checkpoints for MTP speculative verify:
// ckpt[t][ch][k] = the conv state AFTER processing token t = the last `hist`
// inputs ending at t (x[s]=input[s] if s>=0 else old_state[s+hist]). Must run
// BEFORE Conv1dUpdateStateKernel (which overwrites old_state). One thread per
// (checkpoint t, channel). Layout matches conv_state: [num_ckpt, channels, hist].
__global__ void ConvCheckpointKernel(const uint16_t* __restrict__ input,
                                     const uint16_t* __restrict__ old_state,
                                     uint16_t* __restrict__ ckpt, int channels,
                                     int conv_k, int num_ckpt) {
  const int ch = blockIdx.x * blockDim.x + threadIdx.x;
  const int t = blockIdx.y;
  if (ch >= channels || t >= num_ckpt) return;
  const int hist = conv_k - 1;
  uint16_t* dst = ckpt + (static_cast<size_t>(t) * channels + ch) * hist;
  for (int k = 0; k < hist; ++k) {
    const int s = t - hist + 1 + k;  // batch-relative token index
    dst[k] = (s >= 0) ? input[static_cast<size_t>(s) * channels + ch]
                      : old_state[ch * hist + (s + hist)];
  }
}

// Gated DeltaNet recurrence. One block per value head (nv blocks), 128 threads
// (one per vd element). S[kd, vd] held in shared memory (FP32). Mirrors
// qwen35-thor gated_delta_net_prefill_kernel.
//
// Per token t, value head h_v (key head h_k = h_v / nv_per_kh):
//   k_hat = k / sqrt(sum(k^2) + eps)            (L2-style, no /kd)
//   q_hat = q / sqrt(sum(q^2) + eps) * 1/sqrt(kd)
//   alpha = exp(-softplus(a + dt_bias) * exp(A_log))
//   beta  = sigmoid(beta_raw)
//   kS[j] = sum_i k_hat[i] * S[i, j]
//   delta[j] = v[j] - alpha * kS[j]
//   S[i, j] = alpha * S[i, j] + beta * k_hat[i] * delta[j]
//   y[j] = sum_i S_new[i, j] * q_hat[i]
//
//   qkv  : [T, in_qkv] (q | k | v, post-conv), token_stride = in_qkv
//   a    : [T, nv] (raw decay), beta : [T, nv] (raw gate)
//   dt_bias, A_log : [nv]
//   ssm  : [nv, kd, vd] (BF16, in-place persistent state)
//   y    : [T, nv, vd] (out)
__global__ void __launch_bounds__(128, 2) GatedDeltaNetKernel(
    const uint16_t* __restrict__ qkv, const uint16_t* __restrict__ a_raw,
    const uint16_t* __restrict__ dt_bias, const uint16_t* __restrict__ A_log,
    const uint16_t* __restrict__ beta_raw, float* __restrict__ ssm,
    uint16_t* __restrict__ y, int T, int nkh, int kd, int nv_per_kh, int vd,
    int token_stride, int nv, float* __restrict__ ssm_ckpt, int num_ckpt) {
  const int h_v = blockIdx.x;
  const int h_k = h_v / nv_per_kh;
  const int j = threadIdx.x;  // vd index
  extern __shared__ float smem[];
  const int vd_pad = vd + 1;
  float* S_smem = smem;
  float* k_hat_s = S_smem + kd * vd_pad;
  float* q_hat_s = k_hat_s + kd;
  float* s_part = q_hat_s + kd;  // [128] reduction scratch
  float* s_norms = s_part + 128;  // [2] k_sq, q_sq

  const int ss_base = h_v * kd * vd;
  const float q_scale = rsqrtf(static_cast<float>(kd));
  // Load initial state S[kd, vd] -> S_smem (FP32 GMEM -> FP32 SMEM). The
  // persistent state is FP32 to match the reference (transformers keeps the
  // GatedDeltaNet recurrent state in float32 end-to-end); a BF16 state
  // quantized at every chunk boundary drifts measurably from the reference.
  for (int i = 0; i < kd; ++i) S_smem[i * vd_pad + j] = ssm[ss_base + i * vd + j];
  __syncthreads();

  for (int t = 0; t < T; ++t) {
    const int q_base = t * token_stride + h_k * kd;
    const int k_base = t * token_stride + nkh * kd + h_k * kd;

    float local_k_sq = 0.0f;
    float local_q_sq = 0.0f;
    for (int i = threadIdx.x; i < kd; i += blockDim.x) {
      const float kv = Bf16ToFloat(qkv[k_base + i]);
      local_k_sq += kv * kv;
      const float qv = Bf16ToFloat(qkv[q_base + i]);
      local_q_sq += qv * qv;
    }
    s_norms[0] = BlockReduceSum(local_k_sq, s_part);
    __syncthreads();
    s_norms[1] = BlockReduceSum(local_q_sq, s_part);
    __syncthreads();

    // L2-style normalization (NOT RMSNorm): 1/sqrt(sum(x^2) + eps). The
    // reference (qwen35-thor gated_delta_net_*_kernel) does not divide by kd;
    // q additionally carries a 1/sqrt(kd) scale.
    const float k_norm = rsqrtf(s_norms[0] + 1e-6f);
    const float q_norm = rsqrtf(s_norms[1] + 1e-6f) * q_scale;
    for (int i = threadIdx.x; i < kd; i += blockDim.x) {
      k_hat_s[i] = Bf16ToFloat(qkv[k_base + i]) * k_norm;
      q_hat_s[i] = Bf16ToFloat(qkv[q_base + i]) * q_norm;
    }
    __syncthreads();

    const float a_val = Bf16ToFloat(a_raw[t * nv + h_v]);
    const float bias = Bf16ToFloat(dt_bias[h_v]);
    const float a_l = Bf16ToFloat(A_log[h_v]);
    const float ab = a_val + bias;
    // softplus(ab) = log(1 + e^ab); exp2f(ab*LOG2E) == expf(ab). (The reference
    // writes exp2f(x*LOG2E); using expf(x) directly is identical and avoids the
    // common expf(x*LOG2E) == e^(LOG2E*x) mistake.)
    const float dt_v = (ab > 20.0f) ? ab : log1pf(expf(ab));
    const float alpha_v = expf(-dt_v * expf(a_l));
    const float beta_v =
        1.0f / (1.0f + expf(-Bf16ToFloat(beta_raw[t * nv + h_v])));

    const int v_base = t * token_stride + 2 * nkh * kd + h_v * vd;
    float kS_j = 0.0f;
    for (int i = 0; i < kd; ++i)
      kS_j += k_hat_s[i] * S_smem[i * vd_pad + j];

    const float v_j = Bf16ToFloat(qkv[v_base + j]);
    const float delta_j = v_j - alpha_v * kS_j;

    float y_j = 0.0f;
    for (int i = 0; i < kd; ++i) {
      const float beta_k_i = beta_v * k_hat_s[i];
      const float old_s = S_smem[i * vd_pad + j];
      const float new_s = alpha_v * old_s + beta_k_i * delta_j;
      S_smem[i * vd_pad + j] = new_s;
      y_j += new_s * q_hat_s[i];
    }

    const int y_base = (t * nv + h_v) * vd;
    y[y_base + j] = FloatToBf16(y_j);
    // MTP speculative verify: save per-token SSM state so a partial accept can
    // restore checkpoint[accept_count] via D2D instead of re-running the
    // forward. Layout: [num_ckpt, nv, kd, vd]; this block owns head h_v.
    if (ssm_ckpt && t < num_ckpt) {
      const size_t ck_off = static_cast<size_t>(t) * nv * kd * vd + ss_base;
      for (int i = 0; i < kd; ++i)
        ssm_ckpt[ck_off + i * vd + j] = S_smem[i * vd_pad + j];
    }
    __syncthreads();
  }

  // Write final state (FP32 SMEM -> FP32 GMEM).
  for (int i = 0; i < kd; ++i) ssm[ss_base + i * vd + j] = S_smem[i * vd_pad + j];
}

// Fused per-head RMSNorm * gate(z) gate. One block per (token, value head),
// 128 threads (one per vd). The gate activation is `output_gate_type` from the
// config (sigmoid for qwen4_exp), NOT the conv1d activation (silu). Mirrors
// transformers Qwen4ExpTextRMSNormGated: weight * rmsnorm(x) * ACT(gate).
//   y_ssm : [T, nv, vd] (in-place: becomes rmsnorm(y_ssm) * sigmoid(z))
//   z     : [T, nv, vd]
//   weight: [vd] (plain scale, not centered)
__global__ void NormGateKernel(uint16_t* __restrict__ y_ssm,
                               const uint16_t* __restrict__ z,
                               const uint16_t* __restrict__ weight,
                               float eps, int nv, int vd) {
  const int token = blockIdx.x;
  const int head = blockIdx.y;
  const int tid = threadIdx.x;
  const int off = (token * nv + head) * vd;
  __shared__ float s_inv_rms;
  __shared__ float s_part[128];

  float sum_sq = 0.0f;
  for (int i = tid; i < vd; i += blockDim.x) {
    const float v = Bf16ToFloat(y_ssm[off + i]);
    sum_sq += v * v;
  }
  sum_sq = BlockReduceSum(sum_sq, s_part);
  if (tid == 0) s_inv_rms = rsqrtf(sum_sq / vd + eps);
  __syncthreads();
  const float inv_rms = s_inv_rms;
  for (int i = tid; i < vd; i += blockDim.x) {
    const float y_val = Bf16ToFloat(y_ssm[off + i]);
    const float w = Bf16ToFloat(weight[i]);
    const float normalized = y_val * inv_rms * w;
    const float z_val = Bf16ToFloat(z[off + i]);
    y_ssm[off + i] = FloatToBf16(normalized * Sigmoid(z_val));
  }
}

}  // namespace

void LinearAttentionWeights::Free() {
  if (in_proj_qkv) cudaFree(in_proj_qkv);
  if (in_proj_z) cudaFree(in_proj_z);
  if (in_proj_a) cudaFree(in_proj_a);
  if (in_proj_b) cudaFree(in_proj_b);
  if (conv1d) cudaFree(conv1d);
  if (out_proj) cudaFree(out_proj);
  if (norm) cudaFree(norm);
  if (A_log) cudaFree(A_log);
  if (dt_bias) cudaFree(dt_bias);
  in_proj_qkv = in_proj_z = in_proj_a = in_proj_b = conv1d = nullptr;
  out_proj = norm = A_log = dt_bias = nullptr;
}

Status LoadLinearAttention(const io::WeightLoader& loader,
                           const std::string& prefix, int hidden_size, int nkh,
                           int nv, int kd, int vd, int conv_k, float eps,
                           LinearAttentionWeights* out, cudaStream_t stream) {
  if (hidden_size <= 0 || nkh <= 0 || nv <= 0 || kd <= 0 || vd <= 0 ||
      conv_k <= 1) {
    return Status::Fail("invalid linear attention dims");
  }
  out->hidden_size = hidden_size;
  out->nkh = nkh;
  out->nv = nv;
  out->kd = kd;
  out->vd = vd;
  out->conv_k = conv_k;
  out->eps = eps;
  const int qk = nkh * kd;
  const int v_dim = nv * vd;
  const int in_qkv = 2 * qk + v_dim;

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
  if (!(s = alloc(&out->in_proj_qkv,
                  static_cast<size_t>(in_qkv) * hidden_size * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->in_proj_z,
                  static_cast<size_t>(v_dim) * hidden_size * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->in_proj_a,
                  static_cast<size_t>(nv) * hidden_size * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->in_proj_b,
                  static_cast<size_t>(nv) * hidden_size * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->conv1d,
                  static_cast<size_t>(in_qkv) * conv_k * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->out_proj,
                  static_cast<size_t>(hidden_size) * v_dim * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->norm, static_cast<size_t>(vd) * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->A_log, static_cast<size_t>(nv) * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->dt_bias, static_cast<size_t>(nv) * sizeof(uint16_t))))
    return s;

  if (!(s = load(prefix + ".in_proj_qkv.weight", out->in_proj_qkv,
                 static_cast<size_t>(in_qkv) * hidden_size * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".in_proj_z.weight", out->in_proj_z,
                 static_cast<size_t>(v_dim) * hidden_size * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".in_proj_a.weight", out->in_proj_a,
                 static_cast<size_t>(nv) * hidden_size * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".in_proj_b.weight", out->in_proj_b,
                 static_cast<size_t>(nv) * hidden_size * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".conv1d.weight", out->conv1d,
                 static_cast<size_t>(in_qkv) * conv_k * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".out_proj.weight", out->out_proj,
                 static_cast<size_t>(hidden_size) * v_dim * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".norm.weight", out->norm,
                 static_cast<size_t>(vd) * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".A_log", out->A_log,
                 static_cast<size_t>(nv) * sizeof(uint16_t))))
    return s;
  if (!(s = load(prefix + ".dt_bias", out->dt_bias,
                 static_cast<size_t>(nv) * sizeof(uint16_t))))
    return s;
  if (stream != nullptr && cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("stream sync failed");
  }
  return Status();
}

Status LinearAttentionForward(const LinearAttentionWeights& w,
                              const uint16_t* x, uint16_t* out,
                              float* ssm_state, uint16_t* conv_state, int T,
                              void* workspace, size_t workspace_bytes,
                              cudaStream_t stream, float* ssm_ckpt,
                              uint16_t* conv_ckpt, int num_ckpt) {
  const int hs = w.hidden_size;
  const int nkh = w.nkh, nv = w.nv, kd = w.kd, vd = w.vd, conv_k = w.conv_k;
  const int qk = nkh * kd;
  const int v_dim = nv * vd;
  const int in_qkv = 2 * qk + v_dim;
  if (T <= 0) return Status();

  // Intermediates are allocated separately (NOT carved from `workspace`): the
  // same `workspace` buffer is handed to cuBLASLt as its internal scratch,
  // which would otherwise clobber our GEMM outputs. This mirrors the HC/MoE
  // modules, which cudaMalloc their intermediates and pass `workspace` purely
  // as the GEMM scratch.
  //   qkv_raw [T,in_qkv] (raw projection = conv input + conv_state source)
  //   qkv     [T,in_qkv] (SiLU(conv) result)
  //   z [T,v_dim] | a [T,nv] | beta [T,nv] | y_ssm [T,v_dim]
  uint16_t* d_qkv_raw = nullptr;
  uint16_t* d_qkv = nullptr;
  uint16_t* d_z = nullptr;
  uint16_t* d_a = nullptr;
  uint16_t* d_beta = nullptr;
  uint16_t* d_y_ssm = nullptr;
  // Async alloc/free: cudaFree synchronizes the device (drains the queue),
  // which dominated decode CPU time (~700 frees/step at ~100us each). The
  // memory-pool async variants are cheap and CUDA-Graphs-capturable.
  auto free_all = [&]() {
    cudaFreeAsync(d_qkv_raw, stream);
    cudaFreeAsync(d_qkv, stream);
    cudaFreeAsync(d_z, stream);
    cudaFreeAsync(d_a, stream);
    cudaFreeAsync(d_beta, stream);
    cudaFreeAsync(d_y_ssm, stream);
  };
  auto alloc = [&](uint16_t** p, size_t elems) -> Status {
    if (cudaMallocAsync(reinterpret_cast<void**>(p),
                        elems * sizeof(uint16_t), stream) != cudaSuccess) {
      return Status::Fail("cudaMallocAsync failed");
    }
    return Status();
  };
  Status s;
  if (!(s = alloc(&d_qkv_raw, static_cast<size_t>(T) * in_qkv))) {
    free_all();
    return s;
  }
  if (!(s = alloc(&d_qkv, static_cast<size_t>(T) * in_qkv))) {
    free_all();
    return s;
  }
  if (!(s = alloc(&d_z, static_cast<size_t>(T) * v_dim))) {
    free_all();
    return s;
  }
  if (!(s = alloc(&d_a, static_cast<size_t>(T) * nv))) {
    free_all();
    return s;
  }
  if (!(s = alloc(&d_beta, static_cast<size_t>(T) * nv))) {
    free_all();
    return s;
  }
  if (!(s = alloc(&d_y_ssm, static_cast<size_t>(T) * v_dim))) {
    free_all();
    return s;
  }

  // 1. Projections (all BF16 GEMMs).
  s = CheckGemm(Bf16Gemm(x, w.in_proj_qkv, d_qkv_raw, T, in_qkv, hs, 1.0f, 0.0f,
                         workspace, workspace_bytes, stream));
  if (!s.ok()) {
    free_all();
    return s;
  }
  s = CheckGemm(Bf16Gemm(x, w.in_proj_z, d_z, T, v_dim, hs, 1.0f, 0.0f, workspace,
                         workspace_bytes, stream));
  if (!s.ok()) {
    free_all();
    return s;
  }
  s = CheckGemm(Bf16Gemm(x, w.in_proj_a, d_a, T, nv, hs, 1.0f, 0.0f, workspace,
                         workspace_bytes, stream));
  if (!s.ok()) {
    free_all();
    return s;
  }
  s = CheckGemm(Bf16Gemm(x, w.in_proj_b, d_beta, T, nv, hs, 1.0f, 0.0f, workspace,
                         workspace_bytes, stream));
  if (!s.ok()) {
    free_all();
    return s;
  }

  // 2. Causal conv1d (SiLU) over in_qkv channels: d_qkv = SiLU(conv(d_qkv_raw)),
  // conv_state updated from the RAW projection output (d_qkv_raw).
  {
    const int ch_blocks = (in_qkv + kBlock - 1) / kBlock;
    CausalConv1dKernel<<<dim3(ch_blocks, T), kBlock, 0, stream>>>(
        d_qkv_raw, d_qkv, conv_state, w.conv1d, T, in_qkv, conv_k);
    if (cudaGetLastError() != cudaSuccess) {
      free_all();
      return Status::Fail("conv1d launch");
    }
    // Per-token conv checkpoints (before the state update overwrites old_state).
    if (conv_ckpt && num_ckpt > 0) {
      ConvCheckpointKernel<<<dim3(ch_blocks, num_ckpt), kBlock, 0, stream>>>(
          d_qkv_raw, conv_state, conv_ckpt, in_qkv, conv_k, num_ckpt);
      if (cudaGetLastError() != cudaSuccess) {
        free_all();
        return Status::Fail("conv ckpt launch");
      }
    }
    Conv1dUpdateStateKernel<<<ch_blocks, kBlock, 0, stream>>>(
        conv_state, d_qkv_raw, T, in_qkv, conv_k);
    if (cudaGetLastError() != cudaSuccess) {
      free_all();
      return Status::Fail("conv1d state launch");
    }
  }

  // 3. Gated DeltaNet recurrence.
  {
    const int threads = 128;
    const int vd_pad = vd + 1;
    const size_t smem_bytes =
        static_cast<size_t>(kd * vd_pad + 2 * kd + 128 + 2) * sizeof(float);
    cudaError_t smem_err = cudaFuncSetAttribute(
        GatedDeltaNetKernel, cudaFuncAttributeMaxDynamicSharedMemorySize,
        static_cast<int>(smem_bytes));
    if (smem_err != cudaSuccess) {
      free_all();
      return Status::Fail("gdn smem attr");
    }
    GatedDeltaNetKernel<<<nv, threads, smem_bytes, stream>>>(
        d_qkv, d_a, w.dt_bias, w.A_log, d_beta, ssm_state, d_y_ssm, T, nkh, kd,
        nv / nkh, vd, in_qkv, nv, ssm_ckpt, num_ckpt);
    if (cudaGetLastError() != cudaSuccess) {
      free_all();
      return Status::Fail("gdn launch");
    }
  }

  // Debug dump (before the in-place gate): x, qkv_raw, qkv, y_ssm (raw SSM
  // output). Gated by Q4T_LIN_DUMP.
  DumpLinearIntermediates("lin", x, T, hs, d_qkv_raw, d_qkv, d_y_ssm, in_qkv,
                          v_dim);

  // 4. Fused per-head RMSNorm * sigmoid(z) gate (in-place on d_y_ssm).
  {
    dim3 grid(T, nv);
    NormGateKernel<<<grid, 128, 0, stream>>>(d_y_ssm, d_z, w.norm, w.eps, nv,
                                             vd);
    if (cudaGetLastError() != cudaSuccess) {
      free_all();
      return Status::Fail("norm gate launch");
    }
  }

  // 5. Output projection.
  s = CheckGemm(Bf16Gemm(d_y_ssm, w.out_proj, out, T, hs, v_dim, 1.0f, 0.0f,
                         workspace, workspace_bytes, stream));
  free_all();
  if (!s.ok()) return s;
  return Status();
}

}  // namespace model
}  // namespace q4t
