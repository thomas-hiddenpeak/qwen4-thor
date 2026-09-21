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

// Owns the six independent intermediates on the caller stream. Keep their
// allocation and release order stable; GEMM workspace is borrowed separately.
struct LinearAttentionScratch {
  explicit LinearAttentionScratch(cudaStream_t stream) : stream_(stream) {}
  LinearAttentionScratch(const LinearAttentionScratch&) = delete;
  LinearAttentionScratch& operator=(const LinearAttentionScratch&) = delete;
  ~LinearAttentionScratch() {
    cudaFreeAsync(qkv_raw, stream_);
    cudaFreeAsync(qkv, stream_);
    cudaFreeAsync(z, stream_);
    cudaFreeAsync(a, stream_);
    cudaFreeAsync(beta, stream_);
    cudaFreeAsync(y_ssm, stream_);
  }

  uint16_t* qkv_raw = nullptr;
  uint16_t* qkv = nullptr;
  uint16_t* z = nullptr;
  uint16_t* a = nullptr;
  uint16_t* beta = nullptr;
  uint16_t* y_ssm = nullptr;

 private:
  cudaStream_t stream_;
};

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

// Fused block-wide sum of two independent lanes over blockDim.x threads (a
// multiple of 32) via warp shuffles + a SINGLE __syncthreads. The Gated
// DeltaNet recurrence reduced k_sq and q_sq with two halving-tree
// BlockReduceSum calls == ~2*log2(bd) barriers per token; here the whole
// per-token norm costs one barrier. `scratch` must hold >= 2*(blockDim.x/32)
// floats. Returned to every thread (x = sum of val.x, y = sum of val.y). The
// prefill and MTP verify kernels both use this, so their norms stay identical;
// the warp-butterfly order differs from the halving tree by ~1e-7 rel, far
// inside the reference tolerance.
__device__ __forceinline__ float2 BlockReduceSum2Warp(float2 v, float* scratch) {
#pragma unroll
  for (int off = 16; off > 0; off >>= 1) {
    v.x += __shfl_down_sync(0xffffffffu, v.x, off);
    v.y += __shfl_down_sync(0xffffffffu, v.y, off);
  }
  const int warp = threadIdx.x >> 5;
  const int lane = threadIdx.x & 31;
  const int nw = blockDim.x >> 5;
  if (lane == 0) {
    scratch[warp] = v.x;
    scratch[nw + warp] = v.y;
  }
  __syncthreads();
  float2 r = make_float2(0.0f, 0.0f);
  for (int w = 0; w < nw; ++w) {
    r.x += scratch[w];
    r.y += scratch[nw + w];
  }
  return r;
}

// One Gated DeltaNet recurrence token, shared by the prefill and MTP multi-seq
// verify kernels so their per-token math is byte-identical (the verify path is
// validated as an exact copy of prefill; MoE routing downstream amplifies any
// divergence past the l2_rel < 0.01 gate). Thread j owns value column j; the
// two kd reductions use 4 accumulators to break the 128-deep FMA dependency
// chain (4x ILP hides shared-memory latency at the ~25% occupancy this state-
// heavy kernel is capped to). The 4-way reassociation is ~1e-6 rel, far inside
// the reference tolerance.
//
// kS_j = sum_i k_hat[i] * S[i][j].
__device__ __forceinline__ float GdnKSum(const float* __restrict__ k_hat_s,
                                         const float* __restrict__ S_smem,
                                         int vd_pad, int j, int kd, int kd4) {
  float a0 = 0.0f, a1 = 0.0f, a2 = 0.0f, a3 = 0.0f;
  int i = 0;
  for (; i < kd4; i += 4) {
    a0 += k_hat_s[i + 0] * S_smem[(i + 0) * vd_pad + j];
    a1 += k_hat_s[i + 1] * S_smem[(i + 1) * vd_pad + j];
    a2 += k_hat_s[i + 2] * S_smem[(i + 2) * vd_pad + j];
    a3 += k_hat_s[i + 3] * S_smem[(i + 3) * vd_pad + j];
  }
  float s = (a0 + a1) + (a2 + a3);
  for (; i < kd; ++i) s += k_hat_s[i] * S_smem[i * vd_pad + j];
  return s;
}

// Rank-1 state update S[i][j] = alpha*S + beta*k_hat[i]*delta (in place) and
// y_j = sum_i S_new[i][j] * q_hat[i]. 4 y-accumulators break the y chain; the
// per-i S loads/stores are independent and pipeline freely.
__device__ __forceinline__ float GdnUpdateY(float* __restrict__ S_smem,
                                            const float* __restrict__ k_hat_s,
                                            const float* __restrict__ q_hat_s,
                                            int vd_pad, int j, int kd, int kd4,
                                            float alpha_v, float beta_v,
                                            float delta_j) {
  float y0 = 0.0f, y1 = 0.0f, y2 = 0.0f, y3 = 0.0f;
  int i = 0;
  for (; i < kd4; i += 4) {
    const float os0 = S_smem[(i + 0) * vd_pad + j];
    const float os1 = S_smem[(i + 1) * vd_pad + j];
    const float os2 = S_smem[(i + 2) * vd_pad + j];
    const float os3 = S_smem[(i + 3) * vd_pad + j];
    const float ns0 = alpha_v * os0 + (beta_v * k_hat_s[i + 0]) * delta_j;
    const float ns1 = alpha_v * os1 + (beta_v * k_hat_s[i + 1]) * delta_j;
    const float ns2 = alpha_v * os2 + (beta_v * k_hat_s[i + 2]) * delta_j;
    const float ns3 = alpha_v * os3 + (beta_v * k_hat_s[i + 3]) * delta_j;
    S_smem[(i + 0) * vd_pad + j] = ns0;
    S_smem[(i + 1) * vd_pad + j] = ns1;
    S_smem[(i + 2) * vd_pad + j] = ns2;
    S_smem[(i + 3) * vd_pad + j] = ns3;
    y0 += ns0 * q_hat_s[i + 0];
    y1 += ns1 * q_hat_s[i + 1];
    y2 += ns2 * q_hat_s[i + 2];
    y3 += ns3 * q_hat_s[i + 3];
  }
  float y = (y0 + y1) + (y2 + y3);
  for (; i < kd; ++i) {
    const float os = S_smem[i * vd_pad + j];
    const float ns = alpha_v * os + (beta_v * k_hat_s[i]) * delta_j;
    S_smem[i * vd_pad + j] = ns;
    y += ns * q_hat_s[i];
  }
  return y;
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

// Fused causal conv1d (SiLU) + per-token conv checkpoints.
// Grid: dim3(ch_blocks, T + num_ckpt).
//   z <  T: compute conv output for token z (SiLU(conv)).
//   z >= T: write the conv checkpoint for token (z - T).
// Both branches only READ input + the pre-update conv_state and write different
// outputs (output / ckpt), so they are race-free in one kernel (saves a launch
// vs the separate CausalConv1d + ConvCheckpoint). When num_ckpt == 0 the grid is
// dim3(ch_blocks, T) and this degenerates to the plain causal conv1d.
//   input  : [T, channels] (original, unmodified)
//   output : [T, channels] (SiLU(conv)); may alias input (per-(t,ch) safe)
//   old_state : [channels, conv_k-1] (pre-update history, read-only here)
//   ckpt   : [num_ckpt, channels, conv_k-1] (may be null when num_ckpt == 0)
//   w      : [channels, conv_k]
__global__ void CausalConv1dWithCkptKernel(
    const uint16_t* __restrict__ input, uint16_t* __restrict__ output,
    const uint16_t* __restrict__ old_state, uint16_t* __restrict__ ckpt,
    const uint16_t* __restrict__ w, int T, int channels, int conv_k,
    int num_ckpt) {
  const int ch = blockIdx.x * blockDim.x + threadIdx.x;
  if (ch >= channels) return;
  const int z = blockIdx.y;
  const int hist = conv_k - 1;
  if (z < T) {
    float wv[4];
#pragma unroll
    for (int k = 0; k < 4; ++k)
      wv[k] = (k < conv_k) ? Bf16ToFloat(w[ch * conv_k + k]) : 0.0f;
    float acc = 0.0f;
#pragma unroll
    for (int k = 0; k < 4; ++k) {
      if (k >= conv_k) break;
      const int src_t = z - (hist - k);
      float val;
      if (src_t < 0)
        val = Bf16ToFloat(old_state[ch * hist + (src_t + hist)]);
      else
        val = Bf16ToFloat(input[static_cast<size_t>(src_t) * channels + ch]);
      acc += val * wv[k];
    }
    output[static_cast<size_t>(z) * channels + ch] = FloatToBf16(Silu(acc));
  } else {
    const int t = z - T;
    uint16_t* dst = ckpt + (static_cast<size_t>(t) * channels + ch) * hist;
    for (int k = 0; k < hist; ++k) {
      const int s = t - hist + 1 + k;  // batch-relative token index
      dst[k] = (s >= 0) ? input[static_cast<size_t>(s) * channels + ch]
                        : old_state[ch * hist + (s + hist)];
    }
  }
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

// Causal conv1d (SiLU) for B2 multi-sequence decode. Grid: dim3(ch_blocks, B).
// Each block handles one token (blockIdx.y = t) over a range of channels.
// Token t belongs to sequence d_seq_id[t]; its conv history lives ENTIRELY in
// that sequence's conv_state slice (each packed token is the NEXT decode token
// of its own sequence, so the causal window needs no cross-token input — only
// the per-sequence old_state and the current input[t]). This mirrors one
// decode step of CausalConv1dWithCkptKernel (T=1), with old_state selected by
// d_seq_id[t] and the input row at the packed index t.
//   input  : [B, channels] (raw projection, unmodified)
//   output : [B, channels] (SiLU(conv))
//   old_state : [max_seq, channels, conv_k-1] (pooled, read-only here)
//   w      : [channels, conv_k]
__global__ void CausalConv1dMultiSeqKernel(
    const uint16_t* __restrict__ input, uint16_t* __restrict__ output,
    const uint16_t* __restrict__ old_state, const uint16_t* __restrict__ w,
    const int* __restrict__ d_seq_id, int channels, int conv_k) {
  const int ch = blockIdx.x * blockDim.x + threadIdx.x;
  if (ch >= channels) return;
  const int t = blockIdx.y;
  const int hist = conv_k - 1;
  const uint16_t* seq_state =
      old_state + static_cast<size_t>(d_seq_id[t]) * channels * hist;
  float wv[4];
#pragma unroll
  for (int k = 0; k < 4; ++k)
    wv[k] = (k < conv_k) ? Bf16ToFloat(w[ch * conv_k + k]) : 0.0f;
  float acc = 0.0f;
#pragma unroll
  for (int k = 0; k < 4; ++k) {
    if (k >= conv_k) break;
    const int src_t = -(hist - k);  // decode: this token is the sequence's next
    float val;
    if (src_t < 0)
      val = Bf16ToFloat(seq_state[ch * hist + (src_t + hist)]);
    else
      // The single non-negative tap (k == conv_k-1) is the CURRENT token,
      // which lives at packed index t (NOT 0 — that would be token 0's row).
      val = Bf16ToFloat(input[static_cast<size_t>(t) * channels + ch]);
    acc += val * wv[k];
  }
  output[static_cast<size_t>(t) * channels + ch] = FloatToBf16(Silu(acc));
}

// Update conv_state for B2 multi-sequence decode. Grid: dim3(ch_blocks, B).
// Each block slides the conv_state of sequence d_seq_id[t] by one token:
// state[k] = state[k+1] for k < hist-1, state[hist-1] = input[t]. Mirrors the
// decode branch (shift = hist - T, T = 1) of Conv1dUpdateStateKernel.
__global__ void Conv1dUpdateStateMultiSeqKernel(
    uint16_t* __restrict__ state, const uint16_t* __restrict__ input,
    const int* __restrict__ d_seq_id, int channels, int conv_k) {
  const int ch = blockIdx.x * blockDim.x + threadIdx.x;
  if (ch >= channels) return;
  const int t = blockIdx.y;
  const int hist = conv_k - 1;
  uint16_t* seq_state =
      state + static_cast<size_t>(d_seq_id[t]) * channels * hist;
  for (int k = 0; k < hist - 1; ++k)
    seq_state[ch * hist + k] = seq_state[ch * hist + k + 1];
  seq_state[ch * hist + hist - 1] =
      input[static_cast<size_t>(t) * channels + ch];
}

// Causal conv1d (SiLU) for MTP multi-sequence VERIFY. Sequence-major layout:
// packed token index t = b*T + tt (sequence b, local token tt). Grid
// dim3(ch_blocks, B*T). Each block (ch, t) computes the conv for token t:
// taps from the per-sequence old_state (positions before the sequence's
// packed range) AND from the packed input at the sequence's earlier local
// tokens (b*T + tt - d). This is the multi-sequence analogue of the single-
// sequence prefill branch of CausalConv1dWithCkptKernel (src_t = tt - (hist -
// k), reading input for in-sequence taps and old_state for pre-sequence taps),
// with the state slice selected by d_seq_id[t].
__global__ void CausalConv1dMultiSeqCausalKernel(
    const uint16_t* __restrict__ input, uint16_t* __restrict__ output,
    const uint16_t* __restrict__ old_state, const uint16_t* __restrict__ w,
    const int* __restrict__ d_seq_id, int channels, int conv_k, int T,
    const int* __restrict__ token_local) {
  const int ch = blockIdx.x * blockDim.x + threadIdx.x;
  if (ch >= channels) return;
  const int t = blockIdx.y;  // packed token index = b*tps + tt
  // Local token within its sequence. Uniform (MTP): t % T. Ragged (batched
  // prefill): token_local[t]. `t - tt` is the sequence's packed start offset in
  // BOTH cases, so the tap index `t - (tt - src_t)` = seq_start + src_t holds.
  const int tt = token_local ? token_local[t] : (t % T);
  const int hist = conv_k - 1;
  const uint16_t* seq_state =
      old_state + static_cast<size_t>(d_seq_id[t]) * channels * hist;
  float wv[4];
#pragma unroll
  for (int k = 0; k < 4; ++k)
    wv[k] = (k < conv_k) ? Bf16ToFloat(w[ch * conv_k + k]) : 0.0f;
  float acc = 0.0f;
#pragma unroll
  for (int k = 0; k < 4; ++k) {
    if (k >= conv_k) break;
    const int src_t = tt - (hist - k);  // prefill-style causal tap
    float val;
    if (src_t < 0)
      val = Bf16ToFloat(seq_state[ch * hist + (src_t + hist)]);
    else
      val = Bf16ToFloat(
          input[static_cast<size_t>(t - (tt - src_t)) * channels + ch]);
    acc += val * wv[k];
  }
  output[static_cast<size_t>(t) * channels + ch] = FloatToBf16(Silu(acc));
}

// Update conv_state for MTP multi-sequence VERIFY. Sequence-major: packed
// token t = b*T + tt. Grid dim3(ch_blocks, B). Each block (ch, b) updates
// sequence b's conv_state to the last `hist` inputs of (old history + the
// sequence's T packed tokens): state[k] = the value at local token (T - hist +
// k), read from old_state (negative) or the packed input. Mirrors the prefill
// branch (T >= hist) of Conv1dUpdateStateKernel.
__global__ void Conv1dUpdateStateMultiSeqCausalKernel(
    uint16_t* __restrict__ state, const uint16_t* __restrict__ input,
    const int* __restrict__ d_seq_id, int channels, int conv_k, int T,
    const int* __restrict__ seq_offset) {
  const int ch = blockIdx.x * blockDim.x + threadIdx.x;
  if (ch >= channels) return;
  const int b = blockIdx.y;
  const int hist = conv_k - 1;
  // Sequence b's packed [off, off+len) rows. Uniform (MTP): off=b*T, len=T.
  // Ragged (batched prefill): cu_seqlens seq_offset[b..b+1].
  const int off = seq_offset ? seq_offset[b] : b * T;
  const int len = seq_offset ? (seq_offset[b + 1] - off) : T;
  const int seq = d_seq_id[off];  // all len tokens of block b share seq b
  uint16_t* seq_state =
      state + static_cast<size_t>(seq) * channels * hist;
  for (int k = 0; k < hist; ++k) {
    const int src_t = len - hist + k;  // local token index (prefill branch)
    uint16_t val;
    if (src_t >= 0)
      val = input[static_cast<size_t>(off + src_t) * channels + ch];
    else
      val = seq_state[ch * hist + (src_t + hist)];  // pre-sequence history
    seq_state[ch * hist + k] = val;
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
  float* s_part = q_hat_s + kd;  // reduction scratch (warp-partial sums)

  const int ss_base = h_v * kd * vd;
  const float q_scale = rsqrtf(static_cast<float>(kd));
  const int kd4 = kd & ~3;  // 4-way unroll bound for the inner kd reductions
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
    // One warp-shuffle reduction for both norms (one barrier vs ~14).
    const float2 sq =
        BlockReduceSum2Warp(make_float2(local_k_sq, local_q_sq), s_part);

    // L2-style normalization (NOT RMSNorm): 1/sqrt(sum(x^2) + eps). The
    // reference (qwen35-thor gated_delta_net_*_kernel) does not divide by kd;
    // q additionally carries a 1/sqrt(kd) scale.
    const float k_norm = rsqrtf(sq.x + 1e-6f);
    const float q_norm = rsqrtf(sq.y + 1e-6f) * q_scale;
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
    const float kS_j = GdnKSum(k_hat_s, S_smem, vd_pad, j, kd, kd4);
    const float v_j = Bf16ToFloat(qkv[v_base + j]);
    const float delta_j = v_j - alpha_v * kS_j;
    const float y_j = GdnUpdateY(S_smem, k_hat_s, q_hat_s, vd_pad, j, kd, kd4,
                                 alpha_v, beta_v, delta_j);

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

// ---- Register-state Gated DeltaNet prefill (Q4T_GDN_REG) ----
// The default GatedDeltaNetKernel launches only nv (=48) blocks — one per value
// head — on 20 SMs, and holds the FP32 state S[kd,vd] (66 KB) in shared, so it
// is both parallelism-starved (2.4 block/SM) and shared-occupancy-capped (~25%)
// on a per-token serial recurrence whose cost is latency, not DRAM bandwidth
// (the state lives on-chip; DRAM is only qkv+y). The vd columns of the
// recurrence are fully independent (S[i,j] is used only within column j), so
// this variant parallelizes across them: each WARP owns ROWS vd columns and
// distributes the kd dimension across its 32 lanes (npt=kd/32=4 each), holding
// the state in REGISTERS (s[ROWS][4]) with warp-shuffle reductions for the two
// per-token dot products. grid = (vd/(4*ROWS), nv) -> 768 blocks at ROWS=2
// (16x the default), zero shared, ~64 reg -> high occupancy to hide the serial
// dependency's latency. Mirrors ds4/DwarfStar gdn_scan. Math is identical to
// GatedDeltaNetKernel (verified term-by-term); the reduction order differs
// (warp-shuffle vs the shared-memory kernel), so results match within the bf16
// l2_rel tolerance, not bit-exactly (like the chunked path). kd==vd==128 only,
// single-sequence, no MTP checkpoint.

// Pre-normalize q/k in place (L2, no /kd; q also carries 1/sqrt(kd)) for the
// register-state scan below, so the scan's many warps-per-head do NOT each
// recompute the norm (folding it into the scan cost +12.7% of prefill). One
// warp per (t, h_k); 32 lanes cover kd (NPT each). Writes BF16 back to the q/k
// slots of qkv (v untouched); the extra BF16 round on the normalized value is
// within tolerance (the source q/k were already BF16).
__global__ void GdnRegPrepNormKernel(uint16_t* __restrict__ qkv, int T, int nkh,
                                     int kd, int token_stride) {
  constexpr int NPT = 4;  // kd / 32 (kd == 128)
  const int gwarp = (blockIdx.x * blockDim.x + threadIdx.x) >> 5;
  const int lane = threadIdx.x & 31;
  if (gwarp >= T * nkh) return;
  const int t = gwarp / nkh, h_k = gwarp % nkh;
  const int q_base = t * token_stride + h_k * kd + lane * NPT;
  const int k_base = t * token_stride + nkh * kd + h_k * kd + lane * NPT;
  float qq[NPT], kk[NPT], qs = 0.0f, ks = 0.0f;
#pragma unroll
  for (int p = 0; p < NPT; ++p) {
    qq[p] = Bf16ToFloat(qkv[q_base + p]);
    qs += qq[p] * qq[p];
    kk[p] = Bf16ToFloat(qkv[k_base + p]);
    ks += kk[p] * kk[p];
  }
#pragma unroll
  for (int off = 16; off > 0; off >>= 1) {
    qs += __shfl_xor_sync(0xffffffffu, qs, off);
    ks += __shfl_xor_sync(0xffffffffu, ks, off);
  }
  const float q_norm = rsqrtf(qs + 1e-6f) * rsqrtf(static_cast<float>(kd));
  const float k_norm = rsqrtf(ks + 1e-6f);
#pragma unroll
  for (int p = 0; p < NPT; ++p) {
    qkv[q_base + p] = FloatToBf16(qq[p] * q_norm);
    qkv[k_base + p] = FloatToBf16(kk[p] * k_norm);
  }
}

template <int ROWS>
__global__ void GatedDeltaNetRegKernel(
    const uint16_t* __restrict__ qkv, const uint16_t* __restrict__ a_raw,
    const uint16_t* __restrict__ dt_bias, const uint16_t* __restrict__ A_log,
    const uint16_t* __restrict__ beta_raw, float* __restrict__ ssm,
    uint16_t* __restrict__ y, int T, int nkh, int kd, int nv_per_kh, int vd,
    int token_stride, int nv) {
  constexpr int NPT = 4;  // kd / 32 (kd == 128)
  const int warps_per_block = blockDim.x >> 5;
  const int warp = threadIdx.x >> 5;
  const int lane = threadIdx.x & 31;
  const int h_v = blockIdx.y;
  const int h_k = h_v / nv_per_kh;
  const int j0 = (blockIdx.x * warps_per_block + warp) * ROWS;  // first vd col
  if (j0 >= vd) return;
  const int k0 = lane * NPT;  // this lane's kd sub-range [k0, k0+NPT)
  const int ss_base = h_v * kd * vd;

  // State s[r][p] = S[k0+p, j0+r], resident in registers.
  float s[ROWS][NPT];
#pragma unroll
  for (int r = 0; r < ROWS; ++r)
#pragma unroll
    for (int p = 0; p < NPT; ++p)
      s[r][p] = ssm[ss_base + (k0 + p) * vd + (j0 + r)];

  const float bias = Bf16ToFloat(dt_bias[h_v]);
  const float a_l = Bf16ToFloat(A_log[h_v]);

  for (int t = 0; t < T; ++t) {
    const int q_base = t * token_stride + h_k * kd + k0;
    const int k_base = t * token_stride + nkh * kd + h_k * kd + k0;
    // q/k are pre-normalized in place by GdnRegPrepNormKernel (one warp per
    // (t, h_k)), so the scan just loads them — no redundant per-warp L2 norm
    // (which, computed per warp, cost +12.7% of prefill; see docs/log).
    // The dispatch requires kd=vd=128. The allocated qkv base, token stride,
    // head offsets and lane*4 offset are all aligned for four BF16 values.
    const uint2 k_bits = *reinterpret_cast<const uint2*>(qkv + k_base);
    const uint2 q_bits = *reinterpret_cast<const uint2*>(qkv + q_base);
    const float kk[NPT] = {
        Bf16ToFloat(static_cast<uint16_t>(k_bits.x)),
        Bf16ToFloat(static_cast<uint16_t>(k_bits.x >> 16)),
        Bf16ToFloat(static_cast<uint16_t>(k_bits.y)),
        Bf16ToFloat(static_cast<uint16_t>(k_bits.y >> 16))};
    const float qq[NPT] = {
        Bf16ToFloat(static_cast<uint16_t>(q_bits.x)),
        Bf16ToFloat(static_cast<uint16_t>(q_bits.x >> 16)),
        Bf16ToFloat(static_cast<uint16_t>(q_bits.y)),
        Bf16ToFloat(static_cast<uint16_t>(q_bits.y >> 16))};
    const float a_val = Bf16ToFloat(a_raw[t * nv + h_v]);
    const float ab = a_val + bias;
    const float dt_v = (ab > 20.0f) ? ab : log1pf(expf(ab));
    const float alpha = expf(-dt_v * expf(a_l));
    const float beta =
        1.0f / (1.0f + expf(-Bf16ToFloat(beta_raw[t * nv + h_v])));

    const int v_base = t * token_stride + 2 * nkh * kd + h_v * vd;
#pragma unroll
    for (int r = 0; r < ROWS; ++r) {
      const float v_j = Bf16ToFloat(qkv[v_base + j0 + r]);
      float u = 0.0f;  // sum_i k_hat[i] * S_old[i, j]
#pragma unroll
      for (int p = 0; p < NPT; ++p) u += kk[p] * s[r][p];
#pragma unroll
      for (int off = 16; off > 0; off >>= 1)
        u += __shfl_xor_sync(0xffffffffu, u, off);
      const float delta = (v_j - alpha * u) * beta;
      float o = 0.0f;  // sum_i S_new[i, j] * q_hat[i]
#pragma unroll
      for (int p = 0; p < NPT; ++p) {
        s[r][p] = alpha * s[r][p] + kk[p] * delta;
        o += s[r][p] * qq[p];
      }
#pragma unroll
      for (int off = 16; off > 0; off >>= 1)
        o += __shfl_xor_sync(0xffffffffu, o, off);
      if (lane == 0)
        y[(static_cast<size_t>(t) * nv + h_v) * vd + j0 + r] = FloatToBf16(o);
    }
  }
#pragma unroll
  for (int r = 0; r < ROWS; ++r)
#pragma unroll
    for (int p = 0; p < NPT; ++p)
      ssm[ss_base + (k0 + p) * vd + (j0 + r)] = s[r][p];
}

// ---- Chunked tensor-core Gated DeltaNet prefill (Q4T_GDN_CHUNKED) ----
// Replaces the per-token serial matvecs with chunk-parallel bf16 tensor-core
// GEMMs (the flash-linear-attention chunked delta rule, derived directly from
// the sequential recurrence above so the numerics match). Validated vs the
// sequential golden at bf16 GEMM precision (~3e-3 l2_rel); see
// tools/gdn_chunk_proto.cu / gdn_chunked_ref.py. Specialized for kd=vd=128.
__device__ __forceinline__ void MmaBf16Gdn(float& c0, float& c1, float& c2,
                                           float& c3, uint32_t a0, uint32_t a1,
                                           uint32_t a2, uint32_t a3, uint32_t b0,
                                           uint32_t b1) {
  asm volatile(
      "mma.sync.aligned.m16n8k16.row.col.f32.bf16.bf16.f32 "
      "{%0,%1,%2,%3}, {%4,%5,%6,%7}, {%8,%9}, {%0,%1,%2,%3};\n"
      : "+f"(c0), "+f"(c1), "+f"(c2), "+f"(c3)
      : "r"(a0), "r"(a1), "r"(a2), "r"(a3), "r"(b0), "r"(b1));
}
// Cout[M,N] f32 = A[M,K] @ B[K,N]; A row-major [M,K], Bt row-major [N,K]
// (Bt[n][k]=B[k][n], i.e. B^T). All warps share the (m-tile,n-tile) grid.
// K%16==0; the operand buffers are sized to the M,N tile multiples (16/8).
__device__ void GdnBlockGemm(float* Cout, const uint16_t* A,
                             const uint16_t* Bt, int M, int N, int K) {
  const int wid = threadIdx.x >> 5, nw = blockDim.x >> 5, lane = threadIdx.x & 31;
  const int group = lane >> 2, k0 = (lane & 3) * 2, col = (lane & 3) * 2;
  const int nmt = (M + 15) / 16, nnt = (N + 7) / 8;
  for (int idx = wid; idx < nmt * nnt; idx += nw) {
    const int mt = idx / nnt, nt = idx % nnt, mb = mt * 16, nb = nt * 8;
    float c0 = 0, c1 = 0, c2 = 0, c3 = 0;
    for (int kt = 0; kt < K / 16; ++kt) {
      const int kb = kt * 16;
      const uint16_t* a = A + (mb + group) * K + kb + k0;
      const uint16_t* a8 = A + (mb + group + 8) * K + kb + k0;
      const uint16_t* b = Bt + (nb + group) * K + kb + k0;
      MmaBf16Gdn(c0, c1, c2, c3,
                 *reinterpret_cast<const uint32_t*>(a),
                 *reinterpret_cast<const uint32_t*>(a8),
                 *reinterpret_cast<const uint32_t*>(a + 8),
                 *reinterpret_cast<const uint32_t*>(a8 + 8),
                 *reinterpret_cast<const uint32_t*>(b),
                 *reinterpret_cast<const uint32_t*>(b + 8));
    }
    if (mb + group < M) {
      if (nb + col < N) Cout[(mb + group) * N + nb + col] = c0;
      if (nb + col + 1 < N) Cout[(mb + group) * N + nb + col + 1] = c1;
    }
    if (mb + group + 8 < M) {
      if (nb + col < N) Cout[(mb + group + 8) * N + nb + col] = c2;
      if (nb + col + 1 < N) Cout[(mb + group + 8) * N + nb + col + 1] = c3;
    }
  }
}

// Grid dim3(nv, 2): block (h_v, s) owns value head h_v and dv columns
// [s*64, s*64+64) (the dv columns are independent). 128 threads, dynamic
// shared. State held transposed S_T[dvh,kd] and delta transposed delta_T so
// the mma B-operands need no transpose. Only used when ssm_ckpt is absent.
__global__ void GatedDeltaNetChunkedKernel(
    const uint16_t* __restrict__ qkv, const uint16_t* __restrict__ a_raw,
    const uint16_t* __restrict__ dt_bias, const uint16_t* __restrict__ A_log,
    const uint16_t* __restrict__ beta_raw, float* __restrict__ ssm,
    uint16_t* __restrict__ y, int T, int nkh, int kd, int nv_per_kh, int vd,
    int token_stride, int nv) {
  if (kd != 128 || vd != 128) return;
  constexpr int KD = 128, VD = 128, CK = 32;
  const int DVH = VD / gridDim.y;  // dv columns per block (vd-split)
  const int h_v = blockIdx.x, h_k = h_v / nv_per_kh, col0 = blockIdx.y * DVH;
  const int tid = threadIdx.x;
  const float q_scale = rsqrtf((float)KD);
  const float bias = Bf16ToFloat(dt_bias[h_v]);
  const float expA = expf(Bf16ToFloat(A_log[h_v]));
  const int ss_base = h_v * KD * VD;

  extern __shared__ unsigned char arena[];
  size_t o = 0;
  auto A32 = [&](size_t nn) -> float* {
    float* p = reinterpret_cast<float*>(arena + o); o += nn * 4; return p; };
  auto A16 = [&](size_t nn) -> uint16_t* {
    uint16_t* p = reinterpret_cast<uint16_t*>(arena + o); o += nn * 2; return p; };
  float* S_T = A32(DVH * KD);
  float* Sdel = A32(DVH * KD);
  float* KS0 = A32(CK * DVH);
  float* QS0 = A32(CK * DVH);
  float* Vc = A32(CK * DVH);
  float* Am = A32(CK * CK);
  float* QK = A32(CK * CK);
  float* deltaT = A32(DVH * CK);
  float* Ydel = A32(CK * DVH);
  float* Gsh = A32(CK);
  float* betaSh = A32(CK);
  float* gtok = A32(CK);
  float* knorm = A32(CK);
  float* qnorm = A32(CK);
  uint16_t* S_Tb = A16(DVH * KD);
  uint16_t* Kc = A16(CK * KD);
  uint16_t* Qc = A16(CK * KD);
  uint16_t* deltaTb = A16(DVH * CK);
  uint16_t* Pb = A16(CK * CK);
  uint16_t* KTs = A16(KD * CK);

  for (int e = tid; e < DVH * KD; e += blockDim.x) {
    int jh = e / KD, i = e % KD;
    S_T[e] = ssm[ss_base + i * VD + col0 + jh];
  }
  __syncthreads();

  for (int c0 = 0; c0 < T; c0 += CK) {
    const int n = min(CK, T - c0);
    if (tid < CK) {
      if (tid < n) {
        const int gt = c0 + tid;
        const int kb = gt * token_stride + nkh * kd + h_k * kd;
        const int qb = gt * token_stride + h_k * kd;
        float ks = 0.f, qs = 0.f;
        for (int i = 0; i < KD; ++i) {
          float kv = Bf16ToFloat(qkv[kb + i]); ks += kv * kv;
          float qv = Bf16ToFloat(qkv[qb + i]); qs += qv * qv;
        }
        knorm[tid] = rsqrtf(ks + 1e-6f);
        qnorm[tid] = rsqrtf(qs + 1e-6f) * q_scale;
        float ab = Bf16ToFloat(a_raw[gt * nv + h_v]) + bias;
        float dtv = (ab > 20.f) ? ab : log1pf(expf(ab));
        gtok[tid] = -dtv * expA;
        betaSh[tid] = 1.f / (1.f + expf(-Bf16ToFloat(beta_raw[gt * nv + h_v])));
      } else {
        knorm[tid] = 0.f; qnorm[tid] = 0.f; gtok[tid] = 0.f; betaSh[tid] = 0.f;
      }
    }
    __syncthreads();
    if (tid == 0) {
      float acc = 0.f;
      for (int r = 0; r < CK; ++r) { acc += gtok[r]; Gsh[r] = acc; }
    }
    for (int e = tid; e < CK * KD; e += blockDim.x) {
      int r = e / KD, i = e % KD;
      if (r < n) {
        const int kb = (c0 + r) * token_stride + nkh * kd + h_k * kd;
        const int qb = (c0 + r) * token_stride + h_k * kd;
        Kc[e] = FloatToBf16(Bf16ToFloat(qkv[kb + i]) * knorm[r]);
        Qc[e] = FloatToBf16(Bf16ToFloat(qkv[qb + i]) * qnorm[r]);
      } else { Kc[e] = 0; Qc[e] = 0; }
    }
    for (int e = tid; e < CK * DVH; e += blockDim.x) {
      int r = e / DVH, c = e % DVH;
      const int vb = (c0 + r) * token_stride + 2 * nkh * kd + h_v * vd;
      Vc[e] = (r < n) ? Bf16ToFloat(qkv[vb + col0 + c]) : 0.f;
    }
    for (int e = tid; e < DVH * KD; e += blockDim.x) S_Tb[e] = FloatToBf16(S_T[e]);
    __syncthreads();
    GdnBlockGemm(Am, Kc, Kc, CK, CK, KD);    // k_hat k_hat^T
    GdnBlockGemm(QK, Qc, Kc, CK, CK, KD);    // q_hat k_hat^T
    GdnBlockGemm(KS0, Kc, S_Tb, CK, DVH, KD);
    GdnBlockGemm(QS0, Qc, S_Tb, CK, DVH, KD);
    __syncthreads();
    for (int e = tid; e < CK * CK; e += blockDim.x) {
      int r = e / CK, p = e % CK;
      Am[e] = (p < r && r < n) ? Am[e] * expf(Gsh[r] - Gsh[p]) * betaSh[p] : 0.f;
    }
    __syncthreads();
    if (tid < DVH) {  // forward substitution, one dv-column per thread
      int j = tid;
      for (int r = 0; r < n; ++r) {
        float rhs = Vc[r * DVH + j] - expf(Gsh[r]) * KS0[r * DVH + j];
        float acc = 0.f;
        for (int p = 0; p < r; ++p) acc += Am[r * CK + p] * deltaT[j * CK + p];
        deltaT[j * CK + r] = rhs - acc;
      }
      for (int r = n; r < CK; ++r) deltaT[j * CK + r] = 0.f;
      for (int r = 0; r < CK; ++r) deltaTb[j * CK + r] = FloatToBf16(deltaT[j * CK + r]);
    }
    for (int e = tid; e < CK * CK; e += blockDim.x) {
      int t = e / CK, r = e % CK;
      float p = (r <= t && t < n) ? QK[e] * expf(Gsh[t] - Gsh[r]) * betaSh[r] : 0.f;
      Pb[e] = FloatToBf16(p);
    }
    const float Glast = Gsh[n - 1];
    for (int e = tid; e < KD * CK; e += blockDim.x) {
      int i = e / CK, r = e % CK;
      float s = (r < n) ? expf(Glast - Gsh[r]) * betaSh[r] * Bf16ToFloat(Kc[r * KD + i]) : 0.f;
      KTs[e] = FloatToBf16(s);
    }
    __syncthreads();
    GdnBlockGemm(Ydel, Pb, deltaTb, CK, DVH, CK);   // P @ delta
    GdnBlockGemm(Sdel, deltaTb, KTs, DVH, KD, CK);  // delta^T @ KD
    __syncthreads();
    for (int e = tid; e < n * DVH; e += blockDim.x) {
      int r = e / DVH, j = e % DVH;
      y[((size_t)(c0 + r) * nv + h_v) * vd + col0 + j] =
          FloatToBf16(expf(Gsh[r]) * QS0[r * DVH + j] + Ydel[r * DVH + j]);
    }
    for (int e = tid; e < DVH * KD; e += blockDim.x)
      S_T[e] = expf(Glast) * S_T[e] + Sdel[e];
    __syncthreads();
  }
  for (int e = tid; e < DVH * KD; e += blockDim.x) {
    int jh = e / KD, i = e % KD;
    ssm[ss_base + i * VD + col0 + jh] = S_T[e];
  }
}

// Gated DeltaNet recurrence for B2 multi-sequence decode. Grid: dim3(nv, B).
// Each block handles ONE value head (blockIdx.x) for ONE token (blockIdx.y =
// token index t). Token t belongs to sequence d_seq_id[t]; its recurrent
// state slice is ssm[d_seq_id[t] * nv*kd*vd + h_v*kd*vd + ...]. The block does
// a single decode step: load that sequence's state, apply the recurrence for
// token t, write the state back, and emit y[t]. Tokens are independent (each
// owns a distinct sequence slice), so there is NO cross-token recurrence —
// unlike the prefill kernel, which serializes T tokens of ONE sequence.
//
// The per-token math is byte-identical to one iteration of
// GatedDeltaNetKernel (same normalization, alpha/beta, delta rule), so a
// packed multi-sequence decode step reproduces the per-sequence single-token
// decode bit-for-bit.
//
//   qkv     : [B, in_qkv] (q | k | v, post-conv), token_stride = in_qkv
//   a, beta : [B, nv]
//   d_seq_id: [B] device int, the sequence id of each token
//   ssm     : [max_seq, nv, kd, vd] FP32 (pooled, in-place)
//   y       : [B, nv, vd]
__global__ void __launch_bounds__(128, 2) GatedDeltaNetDecodeKernel(
    const uint16_t* __restrict__ qkv, const uint16_t* __restrict__ a_raw,
    const uint16_t* __restrict__ dt_bias, const uint16_t* __restrict__ A_log,
    const uint16_t* __restrict__ beta_raw, float* __restrict__ ssm,
    uint16_t* __restrict__ y, int nkh, int kd, int nv_per_kh, int vd,
    int token_stride, int nv, const int* __restrict__ d_seq_id) {
  const int h_v = blockIdx.x;
  const int t = blockIdx.y;  // token index
  const int h_k = h_v / nv_per_kh;
  const int j = threadIdx.x;  // vd index
  extern __shared__ float smem[];
  const int vd_pad = vd + 1;
  float* S_smem = smem;
  float* k_hat_s = S_smem + kd * vd_pad;
  float* q_hat_s = k_hat_s + kd;
  float* s_part = q_hat_s + kd;  // [128] reduction scratch
  float* s_norms = s_part + 128;  // [2] k_sq, q_sq

  const int seq = d_seq_id[t];
  const int ss_base = seq * nv * kd * vd + h_v * kd * vd;
  const float q_scale = rsqrtf(static_cast<float>(kd));
  // Load initial state S[kd, vd] for this sequence's value head.
  for (int i = 0; i < kd; ++i) S_smem[i * vd_pad + j] = ssm[ss_base + i * vd + j];
  __syncthreads();

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

  // Write final state (FP32 SMEM -> FP32 GMEM) for this sequence's head.
  for (int i = 0; i < kd; ++i) ssm[ss_base + i * vd + j] = S_smem[i * vd_pad + j];
}

// Conv checkpoint for MTP multi-sequence VERIFY. Sequence-major: packed token
// t = b*T + tt. Grid dim3(ch_blocks, B * num_ckpt): each block (ch, z) where
// z = b*num_ckpt + tt writes the conv state after sequence b's local token tt
// (the window of (conv_k-1) values ending at tt, from the packed input or the
// per-sequence old_state). Layout: [max_seq, num_ckpt, channels, conv_k-1]
// (per-layer slice), indexed by the POOLED seq id + local token tt so a
// partial accept can restore this sequence's conv state via D2D.
__global__ void Conv1dCheckpointMultiSeqKernel(
    const uint16_t* __restrict__ input, const uint16_t* __restrict__ old_state,
    uint16_t* __restrict__ ckpt, const int* __restrict__ d_seq_id,
    int channels, int conv_k, int T, int num_ckpt) {
  const int ch = blockIdx.x * blockDim.x + threadIdx.x;
  if (ch >= channels) return;
  const int z = blockIdx.y;  // = b * num_ckpt + tt
  const int b = z / num_ckpt;
  const int tt = z % num_ckpt;
  const int hist = conv_k - 1;
  const int seq = d_seq_id[b * T];
  const uint16_t* seq_state =
      old_state + static_cast<size_t>(seq) * channels * hist;
  uint16_t* dst = ckpt +
                  ((static_cast<size_t>(seq) * num_ckpt + tt) * channels + ch) *
                      hist;
  for (int k = 0; k < hist; ++k) {
    const int s = tt - hist + 1 + k;  // local token index (can be negative)
    dst[k] = (s >= 0) ? input[static_cast<size_t>(b * T + s) * channels + ch]
                      : seq_state[ch * hist + (s + hist)];
  }
}

// Gated DeltaNet recurrence for MTP multi-sequence VERIFY. Sequence-major
// layout: packed token index t = b*T + tt (sequence b, local token tt). Grid
// dim3(nv, B): each block handles ONE value head (blockIdx.x) for ONE sequence
// (blockIdx.y = b), serializing the sequence's T tokens (b*T .. b*T+T-1) with
// the in-place S recurrence — the multi-sequence analogue of the single-
// sequence prefill GatedDeltaNetKernel, with the state slice selected by
// d_seq_id[b*T] and the per-token math byte-identical to one iteration of
// GatedDeltaNetKernel. token_stride = T * in_qkv (the packed row stride for a
// local token within its sequence).
//
// Checkpoints (ssm_ckpt, when non-null): layout [max_seq, num_ckpt, nv, kd,
// vd] (per-layer slice), indexed by the POOLED seq id + local token tt; after
// local token tt (tt < num_ckpt) the block saves S for sequence b's slice.
//
//   qkv     : [B*T, in_qkv] (q | k | v, post-conv), sequence-major
//   a, beta : [B*T, nv]
//   d_seq_id: [B*T] device int (d_seq_id[b*T] = sequence b's id)
//   ssm     : [max_seq, nv, kd, vd] FP32 (pooled, in-place)
//   y       : [B*T, nv, vd]
__global__ void __launch_bounds__(128, 2) GatedDeltaNetMultiSeqCausalKernel(
    const uint16_t* __restrict__ qkv, const uint16_t* __restrict__ a_raw,
    const uint16_t* __restrict__ dt_bias, const uint16_t* __restrict__ A_log,
    const uint16_t* __restrict__ beta_raw, float* __restrict__ ssm,
    uint16_t* __restrict__ y, int T, int nkh, int kd, int nv_per_kh, int vd,
    int in_qkv, int nv, const int* __restrict__ d_seq_id,
    float* __restrict__ ssm_ckpt, int num_ckpt,
    const int* __restrict__ seq_offset) {
  const int h_v = blockIdx.x;
  const int b = blockIdx.y;  // sequence index
  const int j = threadIdx.x;  // vd index
  extern __shared__ float smem[];
  const int vd_pad = vd + 1;
  float* S_smem = smem;
  float* k_hat_s = S_smem + kd * vd_pad;
  float* q_hat_s = k_hat_s + kd;
  float* s_part = q_hat_s + kd;  // reduction scratch (warp-partial sums)

  // Sequence b's packed [off, off+len) rows. Uniform (MTP): off=b*T, len=T.
  // Ragged (batched prefill): cu_seqlens seq_offset[b..b+1].
  const int off = seq_offset ? seq_offset[b] : b * T;
  const int len = seq_offset ? (seq_offset[b + 1] - off) : T;
  const int seq = d_seq_id[off];
  const int ss_base = seq * nv * kd * vd + h_v * kd * vd;
  const float q_scale = rsqrtf(static_cast<float>(kd));
  const int kd4 = kd & ~3;  // 4-way unroll bound (matches GatedDeltaNetKernel)
  // Load initial state S[kd, vd] for this sequence's value head.
  for (int i = 0; i < kd; ++i) S_smem[i * vd_pad + j] = ssm[ss_base + i * vd + j];
  __syncthreads();

  for (int tt = 0; tt < len; ++tt) {
    const int t = off + tt;  // packed token index
    const int h_k = h_v / nv_per_kh;
    const int q_base = t * in_qkv + h_k * kd;
    const int k_base = t * in_qkv + nkh * kd + h_k * kd;

    float local_k_sq = 0.0f;
    float local_q_sq = 0.0f;
    for (int i = threadIdx.x; i < kd; i += blockDim.x) {
      const float kv = Bf16ToFloat(qkv[k_base + i]);
      local_k_sq += kv * kv;
      const float qv = Bf16ToFloat(qkv[q_base + i]);
      local_q_sq += qv * qv;
    }
    // One warp-shuffle reduction for both norms (one barrier vs ~14).
    const float2 sq =
        BlockReduceSum2Warp(make_float2(local_k_sq, local_q_sq), s_part);
    const float k_norm = rsqrtf(sq.x + 1e-6f);
    const float q_norm = rsqrtf(sq.y + 1e-6f) * q_scale;
    for (int i = threadIdx.x; i < kd; i += blockDim.x) {
      k_hat_s[i] = Bf16ToFloat(qkv[k_base + i]) * k_norm;
      q_hat_s[i] = Bf16ToFloat(qkv[q_base + i]) * q_norm;
    }
    __syncthreads();

    const float a_val = Bf16ToFloat(a_raw[t * nv + h_v]);
    const float bias = Bf16ToFloat(dt_bias[h_v]);
    const float a_l = Bf16ToFloat(A_log[h_v]);
    const float ab = a_val + bias;
    const float dt_v = (ab > 20.0f) ? ab : log1pf(expf(ab));
    const float alpha_v = expf(-dt_v * expf(a_l));
    const float beta_v =
        1.0f / (1.0f + expf(-Bf16ToFloat(beta_raw[t * nv + h_v])));

    const int v_base = t * in_qkv + 2 * nkh * kd + h_v * vd;
    const float kS_j = GdnKSum(k_hat_s, S_smem, vd_pad, j, kd, kd4);
    const float v_j = Bf16ToFloat(qkv[v_base + j]);
    const float delta_j = v_j - alpha_v * kS_j;
    const float y_j = GdnUpdateY(S_smem, k_hat_s, q_hat_s, vd_pad, j, kd, kd4,
                                 alpha_v, beta_v, delta_j);

    const int y_base = (t * nv + h_v) * vd;
    y[y_base + j] = FloatToBf16(y_j);
    // Per-sequence per-token checkpoint: layout [max_seq, num_ckpt, nv, kd, vd]
    // (per-layer slice). Index by the POOLED seq id and the local token index
    // tt, so a partial accept can restore this sequence's state at the
    // accepted-prefix boundary via D2D.
    if (ssm_ckpt && tt < num_ckpt) {
      const size_t ck_off =
          ((static_cast<size_t>(seq) * num_ckpt + tt) * nv + h_v) * kd * vd;
      for (int i = 0; i < kd; ++i)
        ssm_ckpt[ck_off + i * vd + j] = S_smem[i * vd_pad + j];
    }
    __syncthreads();
  }

  // Write final state (FP32 SMEM -> FP32 GMEM) for this sequence's head.
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
  in_proj_qkv_fp8.Free();
  in_proj_z_fp8.Free();
  out_proj_fp8.Free();
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
  // FP8 (e4m3) decode shadows for the large projections (gated by
  // Q4T_FP8_PROJ). in_proj_a/b (N=nv, tiny + recurrence-sensitive) stay BF16.
  if (!BuildFp8Shadow(out->in_proj_qkv, in_qkv, hidden_size,
                      &out->in_proj_qkv_fp8, Fp8Part::kGdn, stream))
    return Status::Fail("in_proj_qkv FP8 shadow");
  if (!BuildFp8Shadow(out->in_proj_z, v_dim, hidden_size, &out->in_proj_z_fp8,
                      Fp8Part::kGdn, stream))
    return Status::Fail("in_proj_z FP8 shadow");
  if (!BuildFp8Shadow(out->out_proj, hidden_size, v_dim, &out->out_proj_fp8,
                      Fp8Part::kGdn, stream))
    return Status::Fail("out_proj FP8 shadow");
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
                              uint16_t* conv_ckpt, int num_ckpt,
                              const int* d_seq_id, int tokens_per_seq,
                              const RaggedBatch* ragged) {
  const int hs = w.hidden_size;
  const int nkh = w.nkh, nv = w.nv, kd = w.kd, vd = w.vd, conv_k = w.conv_k;
  const int qk = nkh * kd;
  const int v_dim = nv * vd;
  const int in_qkv = 2 * qk + v_dim;
  if (T <= 0) return Status();
  // MTP multi-sequence VERIFY (tokens_per_seq > 1 with d_seq_id): the packed
  // [T, ...] rows are SEQUENCE-MAJOR (B sequences x tokens_per_seq tokens,
  // t = b*Tps + tt). The conv/GDN kernels run a per-sequence CAUSAL chain over
  // the tokens of each sequence (prefill semantics) with the per-sequence
  // state slice selected by d_seq_id. tokens_per_seq == 1 (or 0) with d_seq_id
  // is the B2 multi-sequence DECODE path (one token per sequence, no in-batch
  // chain); d_seq_id == null is the single-sequence path (bit-identical).
  const bool multi_seq_causal = d_seq_id && (tokens_per_seq > 1 || ragged);
  const int B = ragged ? ragged->B
                       : (multi_seq_causal ? (T / tokens_per_seq) : T);
  // Ragged (batched-prefill) packing: per-token local position + per-sequence
  // cu_seqlens replace the uniform tokens_per_seq stride. Null in the uniform
  // (MTP) path, which keeps the kernels bit-identical.
  const int* rag_offset = ragged ? ragged->seq_offset : nullptr;
  const int* rag_local = ragged ? ragged->token_local : nullptr;

  // Intermediates are allocated separately (NOT carved from `workspace`): the
  // same `workspace` buffer is handed to cuBLASLt as its internal scratch,
  // which would otherwise clobber our GEMM outputs. This mirrors the HC/MoE
  // modules, which cudaMalloc their intermediates and pass `workspace` purely
  // as the GEMM scratch.
  //   qkv_raw [T,in_qkv] (raw projection = conv input + conv_state source)
  //   qkv     [T,in_qkv] (SiLU(conv) result)
  //   z [T,v_dim] | a [T,nv] | beta [T,nv] | y_ssm [T,v_dim]
  LinearAttentionScratch scratch(stream);
  auto& d_qkv_raw = scratch.qkv_raw;
  auto& d_qkv = scratch.qkv;
  auto& d_z = scratch.z;
  auto& d_a = scratch.a;
  auto& d_beta = scratch.beta;
  auto& d_y_ssm = scratch.y_ssm;
  // Async frees stay ordered on the caller stream, including partial
  // allocation failures and every early return below.
  auto alloc = [&](uint16_t** p, size_t elems) -> Status {
    if (cudaMallocAsync(reinterpret_cast<void**>(p),
                        elems * sizeof(uint16_t), stream) != cudaSuccess) {
      return Status::Fail("cudaMallocAsync failed");
    }
    return Status();
  };
  Status s;
  if (!(s = alloc(&d_qkv_raw, static_cast<size_t>(T) * in_qkv))) {
    return s;
  }
  if (!(s = alloc(&d_qkv, static_cast<size_t>(T) * in_qkv))) {
    return s;
  }
  if (!(s = alloc(&d_z, static_cast<size_t>(T) * v_dim))) {
    return s;
  }
  if (!(s = alloc(&d_a, static_cast<size_t>(T) * nv))) {
    return s;
  }
  if (!(s = alloc(&d_beta, static_cast<size_t>(T) * nv))) {
    return s;
  }
  if (!(s = alloc(&d_y_ssm, static_cast<size_t>(T) * v_dim))) {
    return s;
  }

  // 1. Projections (all BF16 GEMMs; large ones use the FP8 decode shadow).
  s = CheckGemm(ProjGemm(x, w.in_proj_qkv, &w.in_proj_qkv_fp8, d_qkv_raw, T,
                         in_qkv, hs, 1.0f, 0.0f, workspace, workspace_bytes,
                         stream));
  if (!s.ok()) {
    return s;
  }
  s = CheckGemm(ProjGemm(x, w.in_proj_z, &w.in_proj_z_fp8, d_z, T, v_dim, hs,
                         1.0f, 0.0f, workspace, workspace_bytes, stream));
  if (!s.ok()) {
    return s;
  }
  s = CheckGemm(Bf16Gemm(x, w.in_proj_a, d_a, T, nv, hs, 1.0f, 0.0f, workspace,
                         workspace_bytes, stream));
  if (!s.ok()) {
    return s;
  }
  s = CheckGemm(Bf16Gemm(x, w.in_proj_b, d_beta, T, nv, hs, 1.0f, 0.0f, workspace,
                         workspace_bytes, stream));
  if (!s.ok()) {
    return s;
  }

  // 2. Causal conv1d (SiLU) over in_qkv channels: d_qkv = SiLU(conv(d_qkv_raw)),
  // conv_state updated from the RAW projection output (d_qkv_raw). The conv
  // output + per-token MTP checkpoints are computed in ONE fused kernel (the
  // checkpoint rows only read the pre-update state, so they must precede the
  // state update below).
  {
    const int ch_blocks = (in_qkv + kBlock - 1) / kBlock;
    if (multi_seq_causal) {
      // MTP multi-sequence verify: per-sequence causal conv chain (sequence-
      // major packed rows), conv_state is the pooled [max_seq, ...] base.
      CausalConv1dMultiSeqCausalKernel<<<dim3(ch_blocks, T), kBlock, 0, stream>>>(
          d_qkv_raw, d_qkv, conv_state, w.conv1d, d_seq_id, in_qkv, conv_k,
          tokens_per_seq, rag_local);
      if (cudaGetLastError() != cudaSuccess) {
        return Status::Fail("conv1d multi-seq causal launch");
      }
      // Per-sequence per-token conv checkpoints (before the state update; the
      // checkpoint rows only read the pre-update state + packed input).
      if (conv_ckpt) {
        Conv1dCheckpointMultiSeqKernel<<<dim3(ch_blocks, B * num_ckpt), kBlock,
                                         0, stream>>>(
            d_qkv_raw, conv_state, conv_ckpt, d_seq_id, in_qkv, conv_k,
            tokens_per_seq, num_ckpt);
        if (cudaGetLastError() != cudaSuccess) {
          return Status::Fail("conv1d multi-seq ckpt launch");
        }
      }
      Conv1dUpdateStateMultiSeqCausalKernel<<<dim3(ch_blocks, B), kBlock, 0,
                                              stream>>>(
          conv_state, d_qkv_raw, d_seq_id, in_qkv, conv_k, tokens_per_seq,
          rag_offset);
      if (cudaGetLastError() != cudaSuccess) {
        return Status::Fail("conv1d multi-seq causal state launch");
      }
    } else if (d_seq_id) {
      // B2 multi-sequence decode: each token t uses its own sequence's conv
      // slice (d_seq_id[t]); conv_state is the pooled [max_seq, ...] base.
      CausalConv1dMultiSeqKernel<<<dim3(ch_blocks, T), kBlock, 0, stream>>>(
          d_qkv_raw, d_qkv, conv_state, w.conv1d, d_seq_id, in_qkv, conv_k);
      if (cudaGetLastError() != cudaSuccess) {
        return Status::Fail("conv1d multi-seq launch");
      }
      Conv1dUpdateStateMultiSeqKernel<<<dim3(ch_blocks, T), kBlock, 0, stream>>>(
          conv_state, d_qkv_raw, d_seq_id, in_qkv, conv_k);
      if (cudaGetLastError() != cudaSuccess) {
        return Status::Fail("conv1d multi-seq state launch");
      }
    } else {
      const int z_blocks = T + (conv_ckpt ? num_ckpt : 0);
      CausalConv1dWithCkptKernel<<<dim3(ch_blocks, z_blocks), kBlock, 0, stream>>>(
          d_qkv_raw, d_qkv, conv_state, conv_ckpt, w.conv1d, T, in_qkv, conv_k,
          conv_ckpt ? num_ckpt : 0);
      if (cudaGetLastError() != cudaSuccess) {
        return Status::Fail("conv1d launch");
      }
      Conv1dUpdateStateKernel<<<ch_blocks, kBlock, 0, stream>>>(
          conv_state, d_qkv_raw, T, in_qkv, conv_k);
      if (cudaGetLastError() != cudaSuccess) {
        return Status::Fail("conv1d state launch");
      }
    }
  }

  // 3. Gated DeltaNet recurrence.
  {
    const int threads = 128;
    const int vd_pad = vd + 1;
    const size_t smem_bytes =
        static_cast<size_t>(kd * vd_pad + 2 * kd + 128 + 2) * sizeof(float);
    if (multi_seq_causal) {
      // MTP multi-sequence verify: grid (nv, B), each block one value head for
      // one sequence, serializing the sequence's tokens_per_seq tokens with
      // the in-place S recurrence; ssm_state is the pooled [max_seq, ...] base.
      cudaError_t smem_err = cudaFuncSetAttribute(
          GatedDeltaNetMultiSeqCausalKernel,
          cudaFuncAttributeMaxDynamicSharedMemorySize,
          static_cast<int>(smem_bytes));
      if (smem_err != cudaSuccess) {
        return Status::Fail("gdn multi-seq causal smem attr");
      }
      GatedDeltaNetMultiSeqCausalKernel<<<dim3(nv, B), threads, smem_bytes,
                                           stream>>>(
          d_qkv, d_a, w.dt_bias, w.A_log, d_beta, ssm_state, d_y_ssm,
          tokens_per_seq, nkh, kd, nv / nkh, vd, in_qkv, nv, d_seq_id,
          ssm_ckpt, num_ckpt, rag_offset);
      if (cudaGetLastError() != cudaSuccess) {
        return Status::Fail("gdn multi-seq causal launch");
      }
    } else if (d_seq_id) {
      // B2 multi-sequence decode: grid (nv, T), each block one value head for
      // one token; ssm_state is the pooled [max_seq, nv, kd, vd] base.
      cudaError_t smem_err = cudaFuncSetAttribute(
          GatedDeltaNetDecodeKernel,
          cudaFuncAttributeMaxDynamicSharedMemorySize,
          static_cast<int>(smem_bytes));
      if (smem_err != cudaSuccess) {
        return Status::Fail("gdn multi-seq smem attr");
      }
      GatedDeltaNetDecodeKernel<<<dim3(nv, T), threads, smem_bytes, stream>>>(
          d_qkv, d_a, w.dt_bias, w.A_log, d_beta, ssm_state, d_y_ssm, nkh, kd,
          nv / nkh, vd, in_qkv, nv, d_seq_id);
      if (cudaGetLastError() != cudaSuccess) {
        return Status::Fail("gdn multi-seq launch");
      }
    } else {
      // Optional chunked tensor-core prefill (Q4T_GDN_CHUNKED). Only for the
      // no-checkpoint single-seq path at kd=vd=128; the per-token serial kernel
      // stays the default fallback.
      // Register-state warp-per-vd-column GDN prefill (ds4-style) fed by a
      // one-shot q/k prep kernel. Measured +12% single-stream prefill (GDN
      // kernel 4.20->2.67s = -36% at T=8000, zero register spill) vs the
      // shared-state kernel, whose S[kd,vd] in shared caps occupancy and whose
      // nv=48 blocks starve the 20 SMs. ON by default at ROWS=8 (the sweep
      // optimum: enough vd columns per warp to hide the warp-reduce latency via
      // ILP); Q4T_GDN_REG=1/2/4 override, =0 falls back to the shared kernel.
      // Read per call (not static) so the batched/multi-seq equivalence tests
      // can force =0 via the env (getenv is negligible next to a 36-layer
      // prefill).
      const int gdn_reg_rows = []() {
        const char* e = std::getenv("Q4T_GDN_REG");
        if (!e || !*e) return 8;  // default ON
        const int r = atoi(e);
        return (r == 1 || r == 2 || r == 4 || r == 8) ? r : 0;
      }();
      static const bool use_chunked = []() {
        const char* e = std::getenv("Q4T_GDN_CHUNKED");
        return e && *e && e[0] != '0';
      }();
      if (gdn_reg_rows && kd == 128 && vd == 128 && ssm_ckpt == nullptr) {
        const int wpb = 4;  // warps per block (128 threads)
        const dim3 grid(vd / (wpb * gdn_reg_rows), nv);
        const int blk = wpb * 32;
        // Pre-normalize q/k once (one warp per (t, h_k)) so the scan's warps
        // don't each redundantly recompute the L2 norm (+12.7% prefill).
        {
          const int pblk = 128;                 // 4 warps/block
          const int pgrid = (T * nkh + 3) / 4;  // ceil(T*nkh warps / 4)
          GdnRegPrepNormKernel<<<pgrid, pblk, 0, stream>>>(d_qkv, T, nkh, kd,
                                                           in_qkv);
          if (cudaGetLastError() != cudaSuccess) {
            return Status::Fail("gdn reg prep launch");
          }
        }
#define Q4T_GDN_REG_LAUNCH(ROWS)                                            \
  GatedDeltaNetRegKernel<ROWS><<<grid, blk, 0, stream>>>(                   \
      d_qkv, d_a, w.dt_bias, w.A_log, d_beta, ssm_state, d_y_ssm, T, nkh,   \
      kd, nv / nkh, vd, in_qkv, nv)
        switch (gdn_reg_rows) {
          case 1: Q4T_GDN_REG_LAUNCH(1); break;
          case 2: Q4T_GDN_REG_LAUNCH(2); break;
          case 4: Q4T_GDN_REG_LAUNCH(4); break;
          default: Q4T_GDN_REG_LAUNCH(8); break;
        }
#undef Q4T_GDN_REG_LAUNCH
        if (cudaGetLastError() != cudaSuccess) {
          return Status::Fail("gdn reg launch");
        }
      } else if (use_chunked && kd == 128 && vd == 128 && ssm_ckpt == nullptr) {
        // vd-split: more blocks + less shared/block -> higher occupancy.
        // Q4T_GDN_SPLIT (default 4): dv columns per block = vd / split.
        static const int split = []() {
          const char* e = std::getenv("Q4T_GDN_SPLIT");
          int s = e ? atoi(e) : 4;
          return (s == 2 || s == 4) ? s : 4;
        }();
        const int DVH = vd / split, CK = 32;
        const size_t ch_smem =
            static_cast<size_t>(DVH * kd * 2 + CK * DVH * 3 + CK * CK * 2 +
                                DVH * CK + CK * DVH + CK * 5) * sizeof(float) +
            static_cast<size_t>(DVH * kd + CK * kd * 2 + DVH * CK + CK * CK +
                                kd * CK) * sizeof(uint16_t);
        cudaError_t smem_err = cudaFuncSetAttribute(
            GatedDeltaNetChunkedKernel,
            cudaFuncAttributeMaxDynamicSharedMemorySize,
            static_cast<int>(ch_smem));
        if (smem_err != cudaSuccess) {
          return Status::Fail("gdn chunked smem attr");
        }
        GatedDeltaNetChunkedKernel<<<dim3(nv, split), 128, ch_smem, stream>>>(
            d_qkv, d_a, w.dt_bias, w.A_log, d_beta, ssm_state, d_y_ssm, T, nkh,
            kd, nv / nkh, vd, in_qkv, nv);
        if (cudaGetLastError() != cudaSuccess) {
          return Status::Fail("gdn chunked launch");
        }
      } else {
        cudaError_t smem_err = cudaFuncSetAttribute(
            GatedDeltaNetKernel, cudaFuncAttributeMaxDynamicSharedMemorySize,
            static_cast<int>(smem_bytes));
        if (smem_err != cudaSuccess) {
          return Status::Fail("gdn smem attr");
        }
        GatedDeltaNetKernel<<<nv, threads, smem_bytes, stream>>>(
            d_qkv, d_a, w.dt_bias, w.A_log, d_beta, ssm_state, d_y_ssm, T, nkh,
            kd, nv / nkh, vd, in_qkv, nv, ssm_ckpt, num_ckpt);
        if (cudaGetLastError() != cudaSuccess) {
          return Status::Fail("gdn launch");
        }
      }
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
      return Status::Fail("norm gate launch");
    }
  }

  // 5. Output projection.
  s = CheckGemm(ProjGemm(d_y_ssm, w.out_proj, &w.out_proj_fp8, out, T, hs,
                         v_dim, 1.0f, 0.0f, workspace, workspace_bytes,
                         stream));
  if (!s.ok()) return s;
  return Status();
}

}  // namespace model
}  // namespace q4t
