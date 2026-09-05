// Linear attention (Gated DeltaNet SSM) — the qwen4_exp "linear_attention"
// decoder layer's attention block.
//
// qwen4_exp inherits Qwen3.5's GatedDeltaNet. Per-token math (one sequence,
// causal, recurrent form):
//
//   x [T, hs] (already the attn_hyper_connection.mix output; NO layernorm —
//   the trunk residual handles normalization)
//
//   1. in_proj_qkv : qkv  = x @ W_qkv^T   [T, in_qkv]  (in_qkv = 2*qk + v)
//      in_proj_z   : z    = x @ W_z^T     [T, v]
//      in_proj_a   : a    = x @ W_a^T     [T, nv]      (raw decay)
//      in_proj_b   : beta = x @ W_b^T     [T, nv]      (raw gate)
//   2. causal conv1d (kernel `conv_k`) over the in_qkv channels, SiLU out,
//      persistent conv_state [in_qkv, conv_k-1] per sequence.
//   3. Gated DeltaNet recurrence over the q/k/v heads (see .cu for the exact
//      per-token rule). Persistent ssm_state [nv, kd, vd] per sequence,
//      stored FP32 to match the reference (transformers keeps the recurrent
//      state in float32 end-to-end; a BF16 state quantized at every chunk
//      boundary drifts measurably from the reference at the prefill->decode
//      handoff).
//   4. y = per-head RMSNorm(y_ssm) * silu(z)   (norm weight = `norm.weight`,
//      plain (not centered) scale, one weight per value head of `vd` dims).
//   5. out = y @ W_out^T   [T, hs]
//
// qwen4_exp linear layers have NO attention output gate (unlike full
// attention, which multiplies by sigmoid(gate)); the block output is `out`
// directly.
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <string>

#include "q4t/io/weight_loader.h"
#include "q4t/status.h"

namespace q4t {
namespace model {

// Device weights of one linear_attention (GatedDeltaNet) block. BF16
// (uint16) row-major except A_log / dt_bias which the checkpoint stores as
// BF16 too (shape [nv]); we keep them BF16 to match the reference, which
// reads them as BF16.
struct LinearAttentionWeights {
  int hidden_size = 2560;
  int nkh = 16;  // linear_num_key_heads
  int nv = 48;  // linear_num_value_heads
  int kd = 128;  // linear_key_head_dim
  int vd = 128;  // linear_value_head_dim
  int conv_k = 4;  // linear_conv_kernel_dim
  float eps = 1e-6f;

  int qk_dim() const { return nkh * kd; }  // 2048
  int v_dim() const { return nv * vd; }  // 6144
  int in_qkv() const { return 2 * qk_dim() + v_dim(); }  // 10240

  uint16_t* in_proj_qkv = nullptr;  // [in_qkv, hs]
  uint16_t* in_proj_z = nullptr;  // [v_dim, hs]
  uint16_t* in_proj_a = nullptr;  // [nv, hs]
  uint16_t* in_proj_b = nullptr;  // [nv, hs]
  uint16_t* conv1d = nullptr;  // [in_qkv, conv_k] (checkpoint [in_qkv,1,conv_k])
  uint16_t* out_proj = nullptr;  // [hs, v_dim]
  uint16_t* norm = nullptr;  // [vd] (per value-head RMSNorm weight)
  uint16_t* A_log = nullptr;  // [nv]
  uint16_t* dt_bias = nullptr;  // [nv]

  void Free();
};

// Load one linear_attention block's weights from `loader` under
//   {prefix}.in_proj_qkv.weight
//   {prefix}.in_proj_z.weight
//   {prefix}.in_proj_a.weight
//   {prefix}.in_proj_b.weight
//   {prefix}.conv1d.weight
//   {prefix}.out_proj.weight
//   {prefix}.norm.weight
//   {prefix}.A_log
//   {prefix}.dt_bias
// e.g. prefix = "model.language_model.layers.2.linear_attn".
Status LoadLinearAttention(const io::WeightLoader& loader, const std::string& prefix,
                           int hidden_size, int nkh, int nv, int kd, int vd,
                           int conv_k, float eps, LinearAttentionWeights* out,
                           cudaStream_t stream);

// Run one linear_attention block forward for a single sequence.
//
//   x          : device row-major [T, hs] uint16 (BF16)
//   out        : device row-major [T, hs] uint16 (out)
//   ssm_state  : device [nv, kd, vd] float (FP32), in-place persistent state
//   conv_state : device [in_qkv, conv_k-1] uint16 (BF16), in-place
//   T          : number of tokens (this chunk)
//   workspace  : scratch device buffer (>= ~64 MiB) for the projection GEMMs
//                (passed to cuBLASLt as its internal scratch; intermediates are
//                allocated separately, so the buffer must not be reused for
//                anything else during the call)
Status LinearAttentionForward(const LinearAttentionWeights& w, const uint16_t* x,
                              uint16_t* out, float* ssm_state,
                              uint16_t* conv_state, int T, void* workspace,
                              size_t workspace_bytes, cudaStream_t stream);

}  // namespace model
}  // namespace q4t
