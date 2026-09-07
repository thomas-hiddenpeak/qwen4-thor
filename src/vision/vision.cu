// Vision tower (Qwen3_VisionTransformer) — implementation. See vision.h.
//
// Pipeline (matches reference/vllm .../qwen3_vl.py and tools/vision_reference.py):
//   1. patch_embed: Conv3d(in=3, out=1152, k=(2,16,16), s=(2,16,16))
//   2. + pos_embed: bilinear-interpolated 2D positional embedding
//   3. 27 blocks: x += attn(LN(x)); x += mlp(LN(x))
//   4. merger: LayerNorm(1152) -> 2x2 spatial merge -> fc1 -> GELU -> fc2
//
// All GEMMs reuse model::Bf16Gemm (checkpoint row-major [N,K] weights).
#include "q4t/vision/vision.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/io/weight_loader.h"
#include "q4t/model/linear.h"

namespace q4t {
namespace vision {

namespace {

constexpr int kBlock = 256;
constexpr float kRopeTheta = 10000.0f;

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

// LayerNorm (with affine weight/bias, eps). One block per row of `rows`
// [rows, dim] BF16; weight/bias are [dim].
__global__ void LayerNormKernel(const uint16_t* __restrict__ x,
                                const uint16_t* __restrict__ weight,
                                const uint16_t* __restrict__ bias,
                                uint16_t* __restrict__ out, int rows, int dim,
                                float eps) {
  const int r = blockIdx.x;
  if (r >= rows) return;
  const uint16_t* row = x + static_cast<size_t>(r) * dim;
  uint16_t* orow = out + static_cast<size_t>(r) * dim;
  __shared__ float s_part[kBlock];
  float acc = 0.0f;
  for (int i = threadIdx.x; i < dim; i += blockDim.x) {
    const float v = Bf16ToFloat(row[i]);
    acc += v;
  }
  s_part[threadIdx.x] = acc;
  __syncthreads();
  for (int off = blockDim.x / 2; off > 0; off >>= 1) {
    if (threadIdx.x < off) s_part[threadIdx.x] += s_part[threadIdx.x + off];
    __syncthreads();
  }
  const float mean = s_part[0] / dim;
  __syncthreads();
  float var = 0.0f;
  for (int i = threadIdx.x; i < dim; i += blockDim.x) {
    const float d = Bf16ToFloat(row[i]) - mean;
    var += d * d;
  }
  s_part[threadIdx.x] = var;
  __syncthreads();
  for (int off = blockDim.x / 2; off > 0; off >>= 1) {
    if (threadIdx.x < off) s_part[threadIdx.x] += s_part[threadIdx.x + off];
    __syncthreads();
  }
  const float rs = rsqrtf(s_part[0] / dim + eps);
  __syncthreads();
  for (int i = threadIdx.x; i < dim; i += blockDim.x) {
    const float v = Bf16ToFloat(row[i]);
    const float w = Bf16ToFloat(weight[i]);
    const float b = Bf16ToFloat(bias[i]);
    orow[i] = FloatToBf16(w * (v - mean) * rs + b);
  }
}

// GELU (tanh approximation, gelu_pytorch_tanh) in place on `total` BF16.
__global__ void GeluTanhKernel(uint16_t* __restrict__ x, int total) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= total) return;
  const float v = Bf16ToFloat(x[i]);
  const float c = 0.7978845608028654f;  // sqrt(2/pi)
  const float inner = c * (v + 0.044715f * v * v * v);
  x[i] = FloatToBf16(0.5f * v * (1.0f + tanhf(inner)));
}

// out[r] = x[r] + y[r] (BF16 elementwise add), `total` elements.  x and
// out may alias (in-place add), so x is not __restrict__.
__global__ void AddBf16Kernel(const uint16_t* x,
                              const uint16_t* __restrict__ y,
                              uint16_t* out, int total) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= total) return;
  out[i] = FloatToBf16(Bf16ToFloat(x[i]) + Bf16ToFloat(y[i]));
}

// out[r, c] = x[r, c] + bias[c] (BF16), x is [rows, dim].  x and out may
// alias (in-place bias add), so x is not __restrict__.
__global__ void AddBiasBf16Kernel(const uint16_t* x,
                                  const uint16_t* __restrict__ bias,
                                  uint16_t* out, int rows, int dim) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  const int total = rows * dim;
  if (idx >= total) return;
  const int c = idx % dim;
  out[idx] = FloatToBf16(Bf16ToFloat(x[idx]) + Bf16ToFloat(bias[c]));
}

// 2D RoPE apply on one of q/k within a [L, 3H] qkv buffer (row stride
// 3H).  The target (q or k) occupies [L, H] = [L, nh, 72] starting at
// `dim_offset` within each row.  head_dim 72 = two 36-dim halves; each half
// is a neox rotation over 18 pairs (i, i+18) for i in [0,18). First half
// uses h_pos, second w_pos. One thread per pair (owns its two dims
// exclusively -> no read/write race).
// pos_ids: [L, 2] = (h_pos, w_pos) per patch (block-major order).
// cos_h/sin_h/cos_w/sin_w: [max_grid, 18] (indexed by pos).
__global__ void ApplyRope2DKernel(uint16_t* __restrict__ x, int L, int nh,
                                  int row_stride, int dim_offset,
                                  const int* __restrict__ pos_ids,
                                  const float* __restrict__ cos_h,
                                  const float* __restrict__ sin_h,
                                  const float* __restrict__ cos_w,
                                  const float* __restrict__ sin_w) {
  const int pairs_per_half = L * nh * 18;
  const int pp = blockIdx.x * blockDim.x + threadIdx.x;
  const int total = 2 * pairs_per_half;
  if (pp >= total) return;
  const int half = pp / pairs_per_half;  // 0 = h-half, 1 = w-half
  const int within = pp % pairs_per_half;
  const int i = within % 18;
  const int lh = within / 18;
  const int l = lh / nh;
  const int h = lh % nh;
  const int base = l * row_stride + dim_offset + h * 72;
  const int pos = (half == 0) ? pos_ids[l * 2 + 0] : pos_ids[l * 2 + 1];
  const float c = (half == 0) ? cos_h[pos * 18 + i] : cos_w[pos * 18 + i];
  const float s = (half == 0) ? sin_h[pos * 18 + i] : sin_w[pos * 18 + i];
  const int off = (half == 0) ? 0 : 36;
  const int d1 = off + i;
  const int d2 = off + i + 18;
  const float x1 = Bf16ToFloat(x[base + d1]);
  const float x2 = Bf16ToFloat(x[base + d2]);
  x[base + d1] = FloatToBf16(x1 * c - x2 * s);
  x[base + d2] = FloatToBf16(x2 * c + x1 * s);
}

// Bilinear-interpolated pos_embed: out[p, d] = sum_c w[p,c] * pos[rows[p,c], d].
__global__ void PosEmbedKernel(const uint16_t* __restrict__ pos,
                               const int* __restrict__ rows,
                               const float* __restrict__ weights,
                               uint16_t* __restrict__ out, int L, int H) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  const int total = L * H;
  if (idx >= total) return;
  const int p = idx / H;
  const int d = idx % H;
  float acc = 0.0f;
  for (int c = 0; c < 4; ++c) {
    acc += weights[p * 4 + c] *
           Bf16ToFloat(pos[static_cast<size_t>(rows[p * 4 + c]) * H + d]);
  }
  out[idx] = FloatToBf16(acc);
}

// Bidirectional attention for one image: qkv [L, 3H] (q|k|v each [H]),
// RoPE already applied to q,k.  Writes context [L, H].
// One block per (l, head).  Shared memory holds the L softmax scores (L <=
// 1024 -> 4KB), well within the 48KB limit.  Three phases:
//   1. cooperatively compute score[j] = q[l] . k[j] * scale
//   2. softmax over the row (max, exp, sum)
//   3. cooperatively compute ctx[l, head, d] = sum_j score[j] * v[j, d]
__global__ void AttentionKernel(const uint16_t* __restrict__ qkv,
                                uint16_t* __restrict__ ctx, int L, int nh,
                                int hd, int H, float scale) {
  const int l = blockIdx.x;
  const int head = blockIdx.y;
  if (l >= L) return;
  extern __shared__ float s_score[];  // [L]
  const int q_off = head * hd;
  const int k_off = H + head * hd;
  const int v_off = 2 * H + head * hd;
  const uint16_t* qrow = qkv + static_cast<size_t>(l) * 3 * H + q_off;
  // Phase 1: score[j] = dot(q[l], k[j]) * scale.
  for (int j = threadIdx.x; j < L; j += blockDim.x) {
    const uint16_t* krow = qkv + static_cast<size_t>(j) * 3 * H + k_off;
    float dot = 0.0f;
    for (int d = 0; d < hd; ++d) {
      dot += Bf16ToFloat(qrow[d]) * Bf16ToFloat(krow[d]);
    }
    s_score[j] = dot * scale;
  }
  __syncthreads();
  // Phase 2: row max, then exp + sum.
  float row_max = -1e30f;
  for (int j = 0; j < L; ++j) row_max = fmaxf(row_max, s_score[j]);
  float sum = 0.0f;
  for (int j = threadIdx.x; j < L; j += blockDim.x) {
    s_score[j] = __expf(s_score[j] - row_max);
    sum += s_score[j];
  }
  // Block-reduce the sum.
  __shared__ float s_part[kBlock];
  s_part[threadIdx.x] = sum;
  __syncthreads();
  for (int off = blockDim.x / 2; off > 0; off >>= 1) {
    if (threadIdx.x < off) s_part[threadIdx.x] += s_part[threadIdx.x + off];
    __syncthreads();
  }
  const float inv = 1.0f / s_part[0];
  for (int j = threadIdx.x; j < L; j += blockDim.x) s_score[j] *= inv;
  __syncthreads();
  // Phase 3: ctx[l, head, d] = sum_j score[j] * v[j, d].
  uint16_t* crow = ctx + static_cast<size_t>(l) * H + q_off;
  for (int d = threadIdx.x; d < hd; d += blockDim.x) {
    float acc = 0.0f;
    for (int j = 0; j < L; ++j) {
      acc += s_score[j] *
             Bf16ToFloat(qkv[static_cast<size_t>(j) * 3 * H + v_off + d]);
    }
    crow[d] = FloatToBf16(acc);
  }
}

// Host: build 2D RoPE cos/sin tables for a max grid position.
// Returns 4 device buffers (cos_h, sin_h, cos_w, sin_w), each [max_grid, 18].
bool BuildRopeTables(int max_grid, float** d_cos_h, float** d_sin_h,
                     float** d_cos_w, float** d_sin_w, int head_dim) {
  const int half = head_dim / 2;  // 36 (rotary_dim)
  const int neox = half / 2;      // 18 (neox-style pairs)
  std::vector<float> cos_h(max_grid * neox), sin_h(max_grid * neox);
  std::vector<float> cos_w(max_grid * neox), sin_w(max_grid * neox);
  for (int i = 0; i < neox; ++i) {
    const float inv_freq =
        1.0f / std::pow(kRopeTheta, 2.0f * i / half);
    for (int p = 0; p < max_grid; ++p) {
      const float ang = static_cast<float>(p) * inv_freq;
      cos_h[p * neox + i] = std::cos(ang);
      sin_h[p * neox + i] = std::sin(ang);
      cos_w[p * neox + i] = std::cos(ang);
      sin_w[p * neox + i] = std::sin(ang);
    }
  }
  const size_t bytes = max_grid * neox * sizeof(float);
  auto alloc = [](float** p, const float* h, size_t b) {
    if (cudaMalloc(reinterpret_cast<void**>(p), b) != cudaSuccess)
      return false;
    return cudaMemcpy(*p, h, b, cudaMemcpyHostToDevice) == cudaSuccess;
  };
  if (!alloc(d_cos_h, cos_h.data(), bytes)) return false;
  if (!alloc(d_sin_h, sin_h.data(), bytes)) return false;
  if (!alloc(d_cos_w, cos_w.data(), bytes)) return false;
  if (!alloc(d_sin_w, sin_w.data(), bytes)) return false;
  return true;
}

// Host: build bilinear pos-embed tables for one image (h x w grid).
// Patches are in block-major (spatial-merge) order:
//   p = (wb*(h/m)+hb)*(m*m) + mb*m + mj  ->  grid (i,j) = (hb*m+mb, wb*m+mj)
// d_rows: [L, 4] int (corner row indices into the 48x48 pos grid).
// d_weights: [L, 4] float (bilinear weights).
bool BuildPosTables(int h, int w, int grid, int* d_rows, float* d_weights) {
  const int L = h * w;
  const int m = 2;  // spatial_merge_size
  std::vector<int> rows(L * 4);
  std::vector<float> weights(L * 4);
  std::vector<float> h_idx(h), w_idx(w);
  for (int i = 0; i < h; ++i)
    h_idx[i] = (h > 1) ? static_cast<float>(i) * (grid - 1) / (h - 1) : 0.0f;
  for (int j = 0; j < w; ++j)
    w_idx[j] = (w > 1) ? static_cast<float>(j) * (grid - 1) / (w - 1) : 0.0f;
  // Block-major order matching numpy transpose(0,2,1,3,4):
  //   p = (hb*(w/m) + wb)*m*m + mb*m + mj  (hb slowest, then wb, mb, mj).
  for (int hb = 0; hb < h / m; ++hb) {
    for (int wb = 0; wb < w / m; ++wb) {
      for (int mb = 0; mb < m; ++mb) {
        for (int mj = 0; mj < m; ++mj) {
          const int p = (hb * (w / m) + wb) * (m * m) + mb * m + mj;
          const int i = hb * m + mb;
          const int j = wb * m + mj;
          const int hf = static_cast<int>(h_idx[i]);
          const int wf = static_cast<int>(w_idx[j]);
          const int hc = std::min(hf + 1, grid - 1);
          const int wc = std::min(wf + 1, grid - 1);
          const float dh = h_idx[i] - hf;
          const float dw = w_idx[j] - wf;
          const float w11 = dh * dw;
          const float w10 = dh - w11;
          const float w01 = dw - w11;
          const float w00 = 1.0f - dh - w01;
          rows[p * 4 + 0] = hf * grid + wf;
          rows[p * 4 + 1] = hf * grid + wc;
          rows[p * 4 + 2] = hc * grid + wf;
          rows[p * 4 + 3] = hc * grid + wc;
          weights[p * 4 + 0] = w00;
          weights[p * 4 + 1] = w01;
          weights[p * 4 + 2] = w10;
          weights[p * 4 + 3] = w11;
        }
      }
    }
  }
  if (cudaMemcpy(d_rows, rows.data(), L * 4 * sizeof(int),
                 cudaMemcpyHostToDevice) != cudaSuccess)
    return false;
  if (cudaMemcpy(d_weights, weights.data(), L * 4 * sizeof(float),
                 cudaMemcpyHostToDevice) != cudaSuccess)
    return false;
  return true;
}

// Host: build per-patch 2D position IDs in block-major order.
// d_pos_ids: [L, 2] = (h_pos, w_pos) for each patch in block-major order.
bool BuildPosIds(int h, int w, int* d_pos_ids) {
  const int L = h * w;
  const int m = 2;
  std::vector<int> ids(L * 2);
  for (int hb = 0; hb < h / m; ++hb) {
    for (int wb = 0; wb < w / m; ++wb) {
      for (int mb = 0; mb < m; ++mb) {
        for (int mj = 0; mj < m; ++mj) {
          const int p = (hb * (w / m) + wb) * (m * m) + mb * m + mj;
          ids[p * 2 + 0] = hb * m + mb;  // h_pos
          ids[p * 2 + 1] = wb * m + mj;  // w_pos
        }
      }
    }
  }
  return cudaMemcpy(d_pos_ids, ids.data(), L * 2 * sizeof(int),
                    cudaMemcpyHostToDevice) == cudaSuccess;
}

}  // namespace

void VisionWeights::Free() {
  auto freep = [](void* p) {
    if (p) cudaFree(p);
  };
  freep(d_patch_w);
  freep(d_patch_b);
  freep(d_pos_embed);
  for (auto* p : d_ln1_w) freep(p);
  for (auto* p : d_ln1_b) freep(p);
  for (auto* p : d_qkv_w) freep(p);
  for (auto* p : d_qkv_b) freep(p);
  for (auto* p : d_attn_proj_w) freep(p);
  for (auto* p : d_attn_proj_b) freep(p);
  for (auto* p : d_ln2_w) freep(p);
  for (auto* p : d_ln2_b) freep(p);
  for (auto* p : d_fc1_w) freep(p);
  for (auto* p : d_fc1_b) freep(p);
  for (auto* p : d_fc2_w) freep(p);
  for (auto* p : d_fc2_b) freep(p);
  freep(d_merger_ln_w);
  freep(d_merger_ln_b);
  freep(d_merger_fc1_w);
  freep(d_merger_fc1_b);
  freep(d_merger_fc2_w);
  freep(d_merger_fc2_b);
  d_patch_w = d_patch_b = d_pos_embed = nullptr;
  d_ln1_w.clear();
  d_ln1_b.clear();
  d_qkv_w.clear();
  d_qkv_b.clear();
  d_attn_proj_w.clear();
  d_attn_proj_b.clear();
  d_ln2_w.clear();
  d_ln2_b.clear();
  d_fc1_w.clear();
  d_fc1_b.clear();
  d_fc2_w.clear();
  d_fc2_b.clear();
  d_merger_ln_w = d_merger_ln_b = nullptr;
  d_merger_fc1_w = d_merger_fc1_b = nullptr;
  d_merger_fc2_w = d_merger_fc2_b = nullptr;
}

void VisionTower::Free() {
  w.Free();
  auto freep = [](void* p) {
    if (p) cudaFree(p);
  };
  freep(d_cos);
  freep(d_sin);
  freep(d_cos_w);
  freep(d_sin_w);
  freep(d_pos_rows);
  freep(d_pos_weights);
  freep(d_pos_ids);
  freep(d_ws);
  d_cos = d_sin = nullptr;
  d_cos_w = d_sin_w = nullptr;
  d_pos_rows = nullptr;
  d_pos_weights = nullptr;
  d_pos_ids = nullptr;
  d_ws = nullptr;
  ws_bytes = 0;
  max_grid = 0;
  host_scratch.clear();
}

bool VisionTower::Allocate(const std::vector<ImageShape>& shapes,
                           cudaStream_t stream) {
  (void)stream;
  int total_L = 0;
  int need_grid = 0;
  for (const auto& s : shapes) {
    total_L += s.L();
    need_grid = std::max(need_grid, std::max(s.h, s.w));
  }
  if (total_L <= 0) return false;
  const int H = cfg.hidden_size;
  const int I = cfg.intermediate_size;
  // Workspace layout (BF16), in elements:
  //   x        : total_L * H          (current block input / running trunk)
  //   x2       : total_L * H          (ping-pong for residual add)
  //   ln       : total_L * H          (layer-norm output)
  //   qkv      : total_L * 3*H        (qkv projection)
  //   ctx      : total_L * H          (attention context)
  //   attn_out : total_L * H          (attn proj output)
  //   mlp1     : total_L * I          (fc1 output)
  //   mlp2     : total_L * H          (fc2 output)
  //   pos      : total_L * H          (pos embed)
  //   merged   : (total_L/4) * 4*H    (merger input, max over images)
  //   gemm_ws  : cuBLASLt workspace
  size_t el = 0;
  el += static_cast<size_t>(total_L) * H;  // x
  el += static_cast<size_t>(total_L) * H;  // x2
  el += static_cast<size_t>(total_L) * H;  // ln
  el += static_cast<size_t>(total_L) * 3 * H;  // qkv
  el += static_cast<size_t>(total_L) * H;  // ctx
  el += static_cast<size_t>(total_L) * H;  // attn_out
  el += static_cast<size_t>(total_L) * I;  // mlp1
  el += static_cast<size_t>(total_L) * H;  // mlp2
  el += static_cast<size_t>(total_L) * H;  // pos
  const size_t gemm_ws = 32u * 1024u * 1024u;  // 32MB cuBLASLt workspace
  ws_bytes = el * sizeof(uint16_t) + gemm_ws;
  // Re-allocate the workspace (idempotent: free any prior allocation so the
  // tower can be reused across requests of differing image sizes).
  if (d_ws) cudaFree(d_ws);
  d_ws = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_ws), ws_bytes) != cudaSuccess)
    return false;
  // RoPE tables (float) for the max grid position. Built once and cached;
  // rebuilt only when a larger grid is requested. The tables are indexed by
  // position id (<= max_grid) so a single allocation covers every image whose
  // grid is <= max_grid.
  if (d_cos == nullptr || need_grid > max_grid) {
    if (d_cos) {
      cudaFree(d_cos);
      cudaFree(d_sin);
      cudaFree(d_cos_w);
      cudaFree(d_sin_w);
      d_cos = d_sin = nullptr;
      d_cos_w = d_sin_w = nullptr;
      max_grid = 0;
    }
    float* cos_h = nullptr;
    float* sin_h = nullptr;
    float* cos_w = nullptr;
    float* sin_w = nullptr;
    if (!BuildRopeTables(need_grid, &cos_h, &sin_h, &cos_w, &sin_w,
                         cfg.head_dim)) {
      return false;
    }
    d_cos = cos_h;
    d_sin = sin_h;
    d_cos_w = cos_w;
    d_sin_w = sin_w;
    max_grid = need_grid;
  }
  // Pos-embed tables (sized for the largest single image).
  int max_L = 0;
  for (const auto& s : shapes) max_L = std::max(max_L, s.L());
  if (d_pos_rows) cudaFree(d_pos_rows);
  if (d_pos_weights) cudaFree(d_pos_weights);
  if (cudaMalloc(reinterpret_cast<void**>(&d_pos_rows),
                 static_cast<size_t>(max_L) * 4 * sizeof(int)) !=
      cudaSuccess)
    return false;
  if (cudaMalloc(reinterpret_cast<void**>(&d_pos_weights),
                 static_cast<size_t>(max_L) * 4 * sizeof(float)) !=
      cudaSuccess)
    return false;
  if (d_pos_ids) cudaFree(d_pos_ids);
  if (cudaMalloc(reinterpret_cast<void**>(&d_pos_ids),
                 static_cast<size_t>(max_L) * 2 * sizeof(int)) !=
      cudaSuccess)
    return false;
  return true;
}

bool LoadVision(const io::WeightLoader& loader, const VisionConfig& cfg,
                VisionTower* tower, std::string* err, cudaStream_t stream) {
  tower->cfg = cfg;
  const int H = cfg.hidden_size;
  const int I = cfg.intermediate_size;
  const int in_dim = cfg.in_channels * cfg.temporal_patch_size *
                     cfg.patch_size * cfg.patch_size;  // 96
  auto alloc = [](uint16_t** p, size_t bytes) -> bool {
    return cudaMalloc(reinterpret_cast<void**>(p), bytes) == cudaSuccess;
  };
  auto load = [&loader, stream](const std::string& name, uint16_t* dst,
                                size_t bytes) -> bool {
    std::vector<uint16_t> host(bytes / sizeof(uint16_t));
    if (!loader.ReadTensor(name, host.data()).ok()) return false;
    return cudaMemcpyAsync(dst, host.data(), bytes, cudaMemcpyHostToDevice,
                           stream) == cudaSuccess;
  };
  auto set_err = [err](const std::string& s) {
    if (err) *err = s;
  };
  // patch_embed
  if (!alloc(&tower->w.d_patch_w,
             static_cast<size_t>(H) * in_dim * sizeof(uint16_t))) {
    set_err("alloc patch_w");
    return false;
  }
  if (!alloc(&tower->w.d_patch_b, static_cast<size_t>(H) * sizeof(uint16_t))) {
    set_err("alloc patch_b");
    return false;
  }
  if (!load("model.visual.patch_embed.proj.weight", tower->w.d_patch_w,
            static_cast<size_t>(H) * in_dim * sizeof(uint16_t))) {
    set_err("load patch_w");
    return false;
  }
  if (!load("model.visual.patch_embed.proj.bias", tower->w.d_patch_b,
            static_cast<size_t>(H) * sizeof(uint16_t))) {
    set_err("load patch_b");
    return false;
  }
  // pos_embed
  if (!alloc(&tower->w.d_pos_embed,
             static_cast<size_t>(cfg.num_position_embeddings) * H *
                 sizeof(uint16_t))) {
    set_err("alloc pos_embed");
    return false;
  }
  if (!load("model.visual.pos_embed.weight", tower->w.d_pos_embed,
            static_cast<size_t>(cfg.num_position_embeddings) * H *
                sizeof(uint16_t))) {
    set_err("load pos_embed");
    return false;
  }
  // blocks
  tower->w.d_ln1_w.reserve(cfg.depth);
  tower->w.d_ln1_b.reserve(cfg.depth);
  tower->w.d_qkv_w.reserve(cfg.depth);
  tower->w.d_qkv_b.reserve(cfg.depth);
  tower->w.d_attn_proj_w.reserve(cfg.depth);
  tower->w.d_attn_proj_b.reserve(cfg.depth);
  tower->w.d_ln2_w.reserve(cfg.depth);
  tower->w.d_ln2_b.reserve(cfg.depth);
  tower->w.d_fc1_w.reserve(cfg.depth);
  tower->w.d_fc1_b.reserve(cfg.depth);
  tower->w.d_fc2_w.reserve(cfg.depth);
  tower->w.d_fc2_b.reserve(cfg.depth);
  for (int i = 0; i < cfg.depth; ++i) {
    const std::string p = "model.visual.blocks." + std::to_string(i) + ".";
    uint16_t* ptrs[12] = {nullptr};
    const size_t sizes[12] = {
        static_cast<size_t>(H) * sizeof(uint16_t),  // ln1_w
        static_cast<size_t>(H) * sizeof(uint16_t),  // ln1_b
        static_cast<size_t>(3 * H) * H * sizeof(uint16_t),  // qkv_w
        static_cast<size_t>(3 * H) * sizeof(uint16_t),      // qkv_b
        static_cast<size_t>(H) * H * sizeof(uint16_t),  // attn_proj_w
        static_cast<size_t>(H) * sizeof(uint16_t),      // attn_proj_b
        static_cast<size_t>(H) * sizeof(uint16_t),  // ln2_w
        static_cast<size_t>(H) * sizeof(uint16_t),  // ln2_b
        static_cast<size_t>(I) * H * sizeof(uint16_t),  // fc1_w
        static_cast<size_t>(I) * sizeof(uint16_t),      // fc1_b
        static_cast<size_t>(H) * I * sizeof(uint16_t),  // fc2_w
        static_cast<size_t>(H) * sizeof(uint16_t),      // fc2_b
    };
    const char* names[12] = {
        "norm1.weight", "norm1.bias", "attn.qkv.weight", "attn.qkv.bias",
        "attn.proj.weight", "attn.proj.bias", "norm2.weight", "norm2.bias",
        "mlp.linear_fc1.weight", "mlp.linear_fc1.bias",
        "mlp.linear_fc2.weight", "mlp.linear_fc2.bias"};
    for (int k = 0; k < 12; ++k) {
      if (!alloc(&ptrs[k], sizes[k])) {
        set_err("alloc block tensor " + std::string(names[k]));
        return false;
      }
    }
    for (int k = 0; k < 12; ++k) {
      if (!load(p + names[k], ptrs[k], sizes[k])) {
        set_err("load " + p + names[k]);
        return false;
      }
    }
    tower->w.d_ln1_w.push_back(ptrs[0]);
    tower->w.d_ln1_b.push_back(ptrs[1]);
    tower->w.d_qkv_w.push_back(ptrs[2]);
    tower->w.d_qkv_b.push_back(ptrs[3]);
    tower->w.d_attn_proj_w.push_back(ptrs[4]);
    tower->w.d_attn_proj_b.push_back(ptrs[5]);
    tower->w.d_ln2_w.push_back(ptrs[6]);
    tower->w.d_ln2_b.push_back(ptrs[7]);
    tower->w.d_fc1_w.push_back(ptrs[8]);
    tower->w.d_fc1_b.push_back(ptrs[9]);
    tower->w.d_fc2_w.push_back(ptrs[10]);
    tower->w.d_fc2_b.push_back(ptrs[11]);
  }
  // merger
  const size_t mh = static_cast<size_t>(4 * H);
  if (!alloc(&tower->w.d_merger_ln_w,
             static_cast<size_t>(H) * sizeof(uint16_t))) {
    set_err("alloc merger_ln_w");
    return false;
  }
  if (!alloc(&tower->w.d_merger_ln_b,
             static_cast<size_t>(H) * sizeof(uint16_t))) {
    set_err("alloc merger_ln_b");
    return false;
  }
  if (!alloc(&tower->w.d_merger_fc1_w, mh * mh * sizeof(uint16_t))) {
    set_err("alloc merger_fc1_w");
    return false;
  }
  if (!alloc(&tower->w.d_merger_fc1_b, mh * sizeof(uint16_t))) {
    set_err("alloc merger_fc1_b");
    return false;
  }
  if (!alloc(&tower->w.d_merger_fc2_w,
             static_cast<size_t>(cfg.out_hidden_size) * mh * sizeof(uint16_t))) {
    set_err("alloc merger_fc2_w");
    return false;
  }
  if (!alloc(&tower->w.d_merger_fc2_b,
             static_cast<size_t>(cfg.out_hidden_size) * sizeof(uint16_t))) {
    set_err("alloc merger_fc2_b");
    return false;
  }
  if (!load("model.visual.merger.norm.weight", tower->w.d_merger_ln_w,
            static_cast<size_t>(H) * sizeof(uint16_t))) {
    set_err("load merger.norm.weight");
    return false;
  }
  if (!load("model.visual.merger.norm.bias", tower->w.d_merger_ln_b,
            static_cast<size_t>(H) * sizeof(uint16_t))) {
    set_err("load merger.norm.bias");
    return false;
  }
  if (!load("model.visual.merger.linear_fc1.weight", tower->w.d_merger_fc1_w,
            mh * mh * sizeof(uint16_t))) {
    set_err("load merger.linear_fc1.weight");
    return false;
  }
  if (!load("model.visual.merger.linear_fc1.bias", tower->w.d_merger_fc1_b,
            mh * sizeof(uint16_t))) {
    set_err("load merger.linear_fc1.bias");
    return false;
  }
  if (!load("model.visual.merger.linear_fc2.weight", tower->w.d_merger_fc2_w,
            static_cast<size_t>(cfg.out_hidden_size) * mh * sizeof(uint16_t))) {
    set_err("load merger.linear_fc2.weight");
    return false;
  }
  if (!load("model.visual.merger.linear_fc2.bias", tower->w.d_merger_fc2_b,
            static_cast<size_t>(cfg.out_hidden_size) * sizeof(uint16_t))) {
    set_err("load merger.linear_fc2.bias");
    return false;
  }
  return true;
}

bool VisionForward(const VisionTower& tower, const uint16_t* pixel_values,
                   const std::vector<ImageShape>& shapes, uint16_t* out,
                   std::string* err, cudaStream_t stream) {
  const VisionConfig& cfg = tower.cfg;
  const VisionWeights& w = tower.w;
  const int H = cfg.hidden_size;
  const int I = cfg.intermediate_size;
  const int nh = cfg.num_heads;
  const int hd = cfg.head_dim;
  const int m = cfg.spatial_merge_size;
  const int in_dim = cfg.in_channels * cfg.temporal_patch_size *
                     cfg.patch_size * cfg.patch_size;
  const int grid = static_cast<int>(std::sqrt(
      static_cast<double>(cfg.num_position_embeddings)));
  const float scale = 1.0f / std::sqrt(static_cast<float>(hd));

  int total_L = 0;
  for (const auto& s : shapes) total_L += s.L();
  if (total_L <= 0 || tower.d_ws == nullptr) {
    if (err) *err = "no workspace";
    return false;
  }

  // Workspace layout (BF16 elements).
  const size_t gemm_ws_bytes = 32u * 1024u * 1024u;
  size_t el = 0;
  auto* x = tower.d_ws;
  el += static_cast<size_t>(total_L) * H;
  auto* x2 = x + el;
  el += static_cast<size_t>(total_L) * H;
  auto* ln = x2 + static_cast<size_t>(total_L) * H;
  el += static_cast<size_t>(total_L) * H;
  auto* qkv = ln + static_cast<size_t>(total_L) * H;
  el += static_cast<size_t>(total_L) * 3 * H;
  auto* ctx = qkv + static_cast<size_t>(total_L) * 3 * H;
  el += static_cast<size_t>(total_L) * H;
  auto* attn_out = ctx + static_cast<size_t>(total_L) * H;
  el += static_cast<size_t>(total_L) * H;
  auto* mlp1 = attn_out + static_cast<size_t>(total_L) * H;
  el += static_cast<size_t>(total_L) * I;
  auto* mlp2 = mlp1 + static_cast<size_t>(total_L) * I;
  el += static_cast<size_t>(total_L) * H;
  auto* pos = mlp2 + static_cast<size_t>(total_L) * H;
  el += static_cast<size_t>(total_L) * H;
  auto* gemm_ws = reinterpret_cast<uint16_t*>(
      reinterpret_cast<uint8_t*>(tower.d_ws) + el * sizeof(uint16_t));

  auto gemm = [&](const uint16_t* a, const uint16_t* b, uint16_t* c, int M,
                  int N, int K) {
    return model::Bf16Gemm(a, b, c, M, N, K, 1.0f, 0.0f, gemm_ws,
                           gemm_ws_bytes, stream)
               .status == CUBLAS_STATUS_SUCCESS;
  };
  auto add_bias = [&](const uint16_t* a, const uint16_t* b, uint16_t* c,
                      int rows, int dim) {
    AddBiasBf16Kernel<<<(rows * dim + kBlock - 1) / kBlock, kBlock, 0,
                        stream>>>(a, b, c, rows, dim);
  };
  auto set_err = [err](const std::string& s) {
    if (err) *err = s;
  };

  // 1. patch_embed: x = pixel_values @ W_patch^T + b_patch.
  if (!gemm(pixel_values, w.d_patch_w, x, total_L, H, in_dim)) {
    set_err("patch gemm");
    return false;
  }
  add_bias(x, w.d_patch_b, x, total_L, H);

  // 2. Per-image: pos_embed + 27 blocks + merger.
  int offset = 0;      // patch offset into x/qkv/... buffers
  int merged_offset = 0;  // merged-token offset into merged/out buffers
  for (const auto& sh : shapes) {
    const int L = sh.L();
    const int h = sh.h, wd = sh.w, t = sh.t;
    const int merged_n = (h / m) * (wd / m) * t;
    // pos_embed (bilinear interpolate) + pos_ids (block-major).
    if (!BuildPosTables(h, wd, grid, tower.d_pos_rows, tower.d_pos_weights)) {
      set_err("build pos tables");
      return false;
    }
    if (!BuildPosIds(h, wd, tower.d_pos_ids)) {
      set_err("build pos ids");
      return false;
    }
    PosEmbedKernel<<<(L * H + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        w.d_pos_embed, tower.d_pos_rows, tower.d_pos_weights,
        pos + offset * H, L, H);
    AddBf16Kernel<<<(L * H + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
        x + offset * H, pos + offset * H, x + offset * H, L * H);
    // 27 blocks.
    for (int i = 0; i < cfg.depth; ++i) {
      uint16_t* xi = x + offset * H;
      uint16_t* xi2 = x2 + offset * H;
      // attn branch.
      LayerNormKernel<<<L, kBlock, 0, stream>>>(xi, w.d_ln1_w[i],
                                                w.d_ln1_b[i], ln + offset * H,
                                                L, H, cfg.eps);
      if (!gemm(ln + offset * H, w.d_qkv_w[i], qkv + offset * 3 * H, L, 3 * H,
                H)) {
        set_err("qkv gemm block " + std::to_string(i));
        return false;
      }
      add_bias(qkv + offset * 3 * H, w.d_qkv_b[i], qkv + offset * 3 * H, L,
               3 * H);
      // 2D RoPE on q (offset 0) and k (offset H); v is left untouched.
      const int rope_total = 2 * L * nh * 18;
      const int rope_grid = (rope_total + kBlock - 1) / kBlock;
      ApplyRope2DKernel<<<rope_grid, kBlock, 0, stream>>>(
          qkv + offset * 3 * H, L, nh, 3 * H, 0, tower.d_pos_ids, tower.d_cos,
          tower.d_sin, tower.d_cos_w, tower.d_sin_w);
      ApplyRope2DKernel<<<rope_grid, kBlock, 0, stream>>>(
          qkv + offset * 3 * H, L, nh, 3 * H, H, tower.d_pos_ids, tower.d_cos,
          tower.d_sin, tower.d_cos_w, tower.d_sin_w);
      {
        const int smem = L * sizeof(float);
        dim3 grid_attn(L, nh);
        AttentionKernel<<<grid_attn, kBlock, smem, stream>>>(
            qkv + offset * 3 * H, ctx + offset * H, L, nh, hd, H, scale);
      }
      if (!gemm(ctx + offset * H, w.d_attn_proj_w[i], attn_out + offset * H,
                L, H, H)) {
        set_err("attn proj gemm block " + std::to_string(i));
        return false;
      }
      add_bias(attn_out + offset * H, w.d_attn_proj_b[i], attn_out + offset * H,
               L, H);
      AddBf16Kernel<<<(L * H + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
          xi, attn_out + offset * H, xi2, L * H);
      // mlp branch.
      LayerNormKernel<<<L, kBlock, 0, stream>>>(xi2, w.d_ln2_w[i],
                                                w.d_ln2_b[i], ln + offset * H,
                                                L, H, cfg.eps);
      if (!gemm(ln + offset * H, w.d_fc1_w[i], mlp1 + offset * I, L, I, H)) {
        set_err("fc1 gemm block " + std::to_string(i));
        return false;
      }
      add_bias(mlp1 + offset * I, w.d_fc1_b[i], mlp1 + offset * I, L, I);
      GeluTanhKernel<<<(L * I + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
          mlp1 + offset * I, L * I);
      if (!gemm(mlp1 + offset * I, w.d_fc2_w[i], mlp2 + offset * H, L, H, I)) {
        set_err("fc2 gemm block " + std::to_string(i));
        return false;
      }
      add_bias(mlp2 + offset * H, w.d_fc2_b[i], mlp2 + offset * H, L, H);
      AddBf16Kernel<<<(L * H + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
          xi2, mlp2 + offset * H, xi, L * H);
    }
    // merger: LN(1152) -> view as [merged_n, 4*H] (block-major: 2x2 blocks
    // are already consecutive) -> fc1 -> GELU -> fc2.
    LayerNormKernel<<<L, kBlock, 0, stream>>>(x + offset * H, w.d_merger_ln_w,
                                              w.d_merger_ln_b, ln + offset * H,
                                              L, H, cfg.eps);
    // In block-major order, the 4 tokens of each 2x2 block are consecutive,
    // so [L, H] viewed as [L/4, 4*H] is the correct merge (no data movement).
    if (!gemm(ln + offset * H, w.d_merger_fc1_w, mlp1 + offset * I, merged_n,
              4 * H, 4 * H)) {
      set_err("merger fc1 gemm");
      return false;
    }
    add_bias(mlp1 + offset * I, w.d_merger_fc1_b, mlp1 + offset * I, merged_n,
             4 * H);
    GeluTanhKernel<<<(merged_n * 4 * H + kBlock - 1) / kBlock, kBlock, 0,
                     stream>>>(mlp1 + offset * I, merged_n * 4 * H);
    if (!gemm(mlp1 + offset * I, w.d_merger_fc2_w,
              out + merged_offset * cfg.out_hidden_size, merged_n,
              cfg.out_hidden_size, 4 * H)) {
      set_err("merger fc2 gemm");
      return false;
    }
    add_bias(out + merged_offset * cfg.out_hidden_size, w.d_merger_fc2_b,
             out + merged_offset * cfg.out_hidden_size, merged_n,
             cfg.out_hidden_size);
    offset += L;
    merged_offset += merged_n;
  }
  return true;
}

}  // namespace vision
}  // namespace q4t
