// Test for the full_attention (QSA sparse attention) block against the real
// checkpoint. Loads the layer-3 self_attn weights, runs the forward on a
// random [T, hs] input (positions 0..T-1) with zero-initialized KV/indexer
// caches, and compares the output against a full CPU reference.
//
// For T=8 (compress_ratio 4) the number of visible compressed blocks is
// ceil(8/4)=2 <= block_topk=512, so QSA degenerates to dense causal
// attention and topk[t] = [0..t]. The CPU reference mirrors: the q/k/v
// projections, the per-head centered RMSNorm (q, k), partial RoPE (first 64
// dims), dense causal GQA attention, the sigmoid gate, and the output
// projection. The QSA indexer logits are checked separately against a CPU
// reference (compressed keys + relu dot product). Skipped when CUDA or the
// model is absent.
#include "q4t/io/weight_loader.h"
#include "q4t/model/full_attention.h"
#include "q4t/test.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <random>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;
using q4t::model::FullAttentionForward;
using q4t::model::FullAttentionWeights;
using q4t::model::LoadFullAttention;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const int kHs = 2560;
const int kNq = 24;
const int kNkv = 2;
const int kHd = 256;
const int kRotD = 64;
const float kTheta = 1e7f;
const float kEps = 1e-6f;
const int kIdxN = 4;
const int kIdxKv = 1;
const int kIdxHd = 128;
const int kIdxBudget = 2048;
const int kIdxCompress = 4;
const int kT = 8;
const int kMaxLen = 256;
const std::string kPrefix = "model.language_model.layers.3.self_attn";

bool FileExists(const char* path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return false;
  close(fd);
  return true;
}
bool CudaAvailable() {
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}
uint16_t FloatToBf16(float f) {
  const __nv_bfloat16 b = __float2bfloat16_rn(f);
  return *reinterpret_cast<const uint16_t*>(&b);
}
float Bf16Round(float f) { return Bf16ToFloat(FloatToBf16(f)); }
float Sigmoid(float v) { return 1.0f / (1.0f + std::exp(-v)); }

float L2RelErr(const std::vector<float>& a, const std::vector<float>& b) {
  double num = 0.0, den = 0.0;
  for (size_t i = 0; i < a.size(); ++i) {
    const double d = double(a[i]) - double(b[i]);
    num += d * d;
    den += double(b[i]) * double(b[i]);
  }
  return float(std::sqrt(num) / (std::sqrt(den) + 1e-6));
}
std::vector<uint16_t> ToBf16(const std::vector<float>& v) {
  std::vector<uint16_t> out(v.size());
  for (size_t i = 0; i < v.size(); ++i) out[i] = FloatToBf16(v[i]);
  return out;
}
std::vector<float> FromBf16(const std::vector<uint16_t>& v) {
  std::vector<float> out(v.size());
  for (size_t i = 0; i < v.size(); ++i) out[i] = Bf16ToFloat(v[i]);
  return out;
}

// y[t, n] = sum_k x[t, k] * W[n, k]  (W row-major [N, K]), BF16-rounded.
std::vector<float> CpuLinearBf16(const std::vector<float>& x,
                                 const std::vector<float>& W, int T, int N,
                                 int K) {
  std::vector<float> y(static_cast<size_t>(T) * N);
  for (int t = 0; t < T; ++t) {
    for (int n = 0; n < N; ++n) {
      float acc = 0.0f;
      const float* wrow = &W[static_cast<size_t>(n) * K];
      const float* xrow = &x[static_cast<size_t>(t) * K];
      for (int k = 0; k < K; ++k) acc += xrow[k] * wrow[k];
      y[static_cast<size_t>(t) * N + n] = Bf16Round(acc);
    }
  }
  return y;
}

// Partial RoPE (first rot_d dims) on a vector, in place (FP32).
void CpuPartialRope(std::vector<float>& v, int rot_d, int pos, float theta) {
  const int half = rot_d / 2;
  for (int i = 0; i < half; ++i) {
    const float inv_freq = std::pow(theta, -2.0f * i / rot_d);
    const float ang = static_cast<float>(pos) * inv_freq;
    const float c = std::cos(ang), s = std::sin(ang);
    const float a = v[i], b = v[i + half];
    v[i] = a * c + b * s;
    v[i + half] = -a * s + b * c;
  }
}

// Centered per-head RMSNorm (in place, FP32): x * rsqrt(mean(x^2)+eps) * (1+w).
void CpuCenteredRmsNorm(std::vector<float>& v, int hd,
                        const std::vector<float>& w, float eps) {
  float sum = 0.0f;
  for (int i = 0; i < hd; ++i) sum += v[i] * v[i];
  const float rs = 1.0f / std::sqrt(sum / hd + eps);
  for (int i = 0; i < hd; ++i) v[i] = Bf16Round(v[i] * rs * (1.0f + w[i]));
}

}  // namespace

Q4T_TEST(full_attention_forward) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex)) {
    std::printf("  (skipped: model index not found)\n");
    return true;
  }

  WeightIndex* index = nullptr;
  Status s = WeightIndex::Open(kIndex, &index);
  if (!s.ok()) {
    std::printf("  index open failed: %s\n", s.message().c_str());
    return false;
  }
  WeightLoader* loader = nullptr;
  s = WeightLoader::Create(kModelDir, *index, 8, &loader);
  if (!s.ok()) {
    std::printf("  loader create failed: %s\n", s.message().c_str());
    return false;
  }

  FullAttentionWeights w;
  s = LoadFullAttention(*loader, kPrefix, kHs, kNq, kNkv, kHd, kRotD, kTheta,
                        kEps, kIdxN, kIdxKv, kIdxHd, kIdxBudget, kIdxCompress,
                        &w, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }
  w.max_len = kMaxLen;  // sizes the persistent 3D MRoPE table.

  auto read_host = [&](const std::string& name, size_t n) {
    std::vector<uint16_t> raw(n);
    Status ls = loader->ReadTensor(name, raw.data());
    if (!ls.ok()) return std::vector<float>();
    return FromBf16(raw);
  };
  const int qg_dim = kNq * 2 * kHd;
  const int kv_dim = kNkv * kHd;
  std::vector<float> W_q = read_host(kPrefix + ".q_proj.weight",
                                     static_cast<size_t>(qg_dim) * kHs);
  std::vector<float> W_k = read_host(kPrefix + ".k_proj.weight",
                                     static_cast<size_t>(kv_dim) * kHs);
  std::vector<float> W_v = read_host(kPrefix + ".v_proj.weight",
                                     static_cast<size_t>(kv_dim) * kHs);
  std::vector<float> W_o = read_host(kPrefix + ".o_proj.weight",
                                     static_cast<size_t>(kHs) * kNq * kHd);
  std::vector<float> q_norm = read_host(kPrefix + ".q_norm.weight", kHd);
  std::vector<float> k_norm = read_host(kPrefix + ".k_norm.weight", kHd);
  std::vector<float> W_idx = read_host(
      kPrefix + ".indexer.index_qk_proj.weight",
      static_cast<size_t>((kIdxN + kIdxKv) * kIdxHd) * kHs);
  std::vector<float> idx_q_norm =
      read_host(kPrefix + ".indexer.q_layernorm.weight", kIdxHd);
  std::vector<float> idx_k_norm =
      read_host(kPrefix + ".indexer.k_layernorm.weight", kIdxHd);
  if (W_q.empty() || W_k.empty() || W_v.empty() || W_o.empty() ||
      q_norm.empty() || k_norm.empty() || W_idx.empty() || idx_q_norm.empty() ||
      idx_k_norm.empty()) {
    std::printf("  host weight read failed\n");
    w.Free();
    return false;
  }

  // Random input x [T, hs], positions 0..T-1.
  std::mt19937 rng(424242);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  std::vector<float> x_in(static_cast<size_t>(kT) * kHs);
  for (auto& v : x_in) v = dist(rng);
  std::vector<uint16_t> x_bf = ToBf16(x_in);
  std::vector<int> positions(kT);
  for (int i = 0; i < kT; ++i) positions[i] = i;

  // Device buffers.
  uint16_t* d_x = nullptr;
  uint16_t* d_out = nullptr;
  uint16_t* d_kv = nullptr;
  int* d_page_table = nullptr;
  uint16_t* d_idx_raw = nullptr;
  uint16_t* d_idx_comp = nullptr;
  int* d_rope_pos = nullptr;
  void* d_ws = nullptr;
  const size_t ws_bytes = 128u * 1024u * 1024u;
  const int kPageSize = q4t::model::kKvPageSize;
  const int n_pages = (kMaxLen + kPageSize - 1) / kPageSize;
  const size_t kv_bytes =
      static_cast<size_t>(n_pages) * kPageSize * kNkv * 2 * kHd * 2;
  const size_t idx_bytes = static_cast<size_t>(kMaxLen) * kIdxHd * 2;
  if (cudaMalloc(&d_x, x_bf.size() * sizeof(uint16_t)) != cudaSuccess ||
      cudaMalloc(&d_out, static_cast<size_t>(kT) * kHs * sizeof(uint16_t)) !=
          cudaSuccess ||
      cudaMalloc(&d_kv, kv_bytes) != cudaSuccess ||
      cudaMalloc(&d_page_table, static_cast<size_t>(kMaxLen) * 4) !=
          cudaSuccess ||
      cudaMalloc(&d_idx_raw, idx_bytes) != cudaSuccess ||
      cudaMalloc(&d_idx_comp, idx_bytes) != cudaSuccess ||
      cudaMalloc(&d_rope_pos, 3u * static_cast<size_t>(kMaxLen) * 4) !=
          cudaSuccess ||
      cudaMalloc(&d_ws, ws_bytes) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    w.Free();
    return false;
  }
  cudaMemset(d_kv, 0, kv_bytes);
  cudaMemset(d_idx_raw, 0, idx_bytes);
  cudaMemset(d_idx_comp, 0, idx_bytes);
  // Identity page table: page_table[p] = p / kPageSize (legacy contiguous).
  std::vector<int> page_table(kMaxLen);
  for (int p = 0; p < kMaxLen; ++p) page_table[p] = p / kPageSize;
  cudaMemcpy(d_page_table, page_table.data(),
             static_cast<size_t>(kMaxLen) * 4, cudaMemcpyHostToDevice);
  // Identity 3D MRoPE table [3, kMaxLen]: all rows = position (pure text).
  {
    std::vector<int> rope_pos(3 * static_cast<size_t>(kMaxLen), 0);
    for (int p = 0; p < kMaxLen; ++p)
      for (int r = 0; r < 3; ++r) rope_pos[r * kMaxLen + p] = p;
    cudaMemcpy(d_rope_pos, rope_pos.data(), rope_pos.size() * sizeof(int),
               cudaMemcpyHostToDevice);
  }
  cudaMemcpy(d_x, x_bf.data(), x_bf.size() * sizeof(uint16_t),
             cudaMemcpyHostToDevice);

  s = FullAttentionForward(w, d_x, d_out, positions.data(), d_rope_pos, d_kv,
                           d_page_table, d_idx_raw, d_idx_comp, kT, d_ws,
                           ws_bytes, nullptr);
  if (!s.ok()) {
    std::printf("  forward failed: %s\n", s.message().c_str());
    w.Free();
    return false;
  }

  // ---------------- CPU reference (main attention) ----------------
  // 1. Projections (BF16-rounded).
  std::vector<float> qg = CpuLinearBf16(x_in, W_q, kT, qg_dim, kHs);
  std::vector<float> k = CpuLinearBf16(x_in, W_k, kT, kv_dim, kHs);
  std::vector<float> v = CpuLinearBf16(x_in, W_v, kT, kv_dim, kHs);
  std::vector<float> k_raw = k;  // pre-norm reference for diagnostics
  std::vector<float> v_raw = v;

  // 2. Deinterleave qg -> q, gate; centered RMSNorm(q).
  std::vector<float> q(static_cast<size_t>(kT) * kNq * kHd);
  std::vector<float> gate(static_cast<size_t>(kT) * kNq * kHd);
  for (int t = 0; t < kT; ++t) {
    for (int h = 0; h < kNq; ++h) {
      std::vector<float> qh(kHd), gh(kHd);
      for (int d = 0; d < kHd; ++d) {
        qh[d] = qg[static_cast<size_t>(t) * qg_dim + h * 2 * kHd + d];
        gh[d] = qg[static_cast<size_t>(t) * qg_dim + h * 2 * kHd + kHd + d];
      }
      CpuCenteredRmsNorm(qh, kHd, q_norm, kEps);
      CpuPartialRope(qh, kRotD, t, kTheta);
      for (int d = 0; d < kHd; ++d) {
        q[static_cast<size_t>(t) * kNq * kHd + h * kHd + d] = qh[d];
        gate[static_cast<size_t>(t) * kNq * kHd + h * kHd + d] = gh[d];
      }
    }
  }
  // 3. Centered RMSNorm(k) + partial RoPE.
  for (int t = 0; t < kT; ++t) {
    for (int h = 0; h < kNkv; ++h) {
      std::vector<float> kh(kHd);
      for (int d = 0; d < kHd; ++d)
        kh[d] = k[static_cast<size_t>(t) * kv_dim + h * kHd + d];
      CpuCenteredRmsNorm(kh, kHd, k_norm, kEps);
      CpuPartialRope(kh, kRotD, t, kTheta);
      for (int d = 0; d < kHd; ++d)
        k[static_cast<size_t>(t) * kv_dim + h * kHd + d] = kh[d];
    }
  }

  // 4. Dense causal GQA attention (QSA degenerates to dense for T=8).
  const float scale = 1.0f / std::sqrt(static_cast<float>(kHd));
  std::vector<float> attn(static_cast<size_t>(kT) * kNq * kHd);
  for (int t = 0; t < kT; ++t) {
    for (int h = 0; h < kNq; ++h) {
      const int kvh = h / (kNq / kNkv);
      // scores over positions 0..t
      std::vector<float> scores(t + 1);
      for (int p = 0; p <= t; ++p) {
        float dot = 0.0f;
        for (int d = 0; d < kHd; ++d)
          dot += q[static_cast<size_t>(t) * kNq * kHd + h * kHd + d] *
                 k[static_cast<size_t>(p) * kv_dim + kvh * kHd + d];
        scores[p] = dot * scale;
      }
      float m = -1e30f;
      for (float sc : scores) m = std::max(m, sc);
      float l = 0.0f;
      for (float& sc : scores) {
        sc = std::exp(sc - m);
        l += sc;
      }
      for (float& sc : scores) sc /= l;
      std::vector<float> oh(kHd, 0.0f);
      for (int p = 0; p <= t; ++p) {
        for (int d = 0; d < kHd; ++d)
          oh[d] += scores[p] * v[static_cast<size_t>(p) * kv_dim + kvh * kHd + d];
      }
      for (int d = 0; d < kHd; ++d)
        attn[static_cast<size_t>(t) * kNq * kHd + h * kHd + d] =
            Bf16Round(oh[d] * Sigmoid(gate[static_cast<size_t>(t) * kNq * kHd + h * kHd + d]));
    }
  }
  // 5. Output projection.
  std::vector<float> out_ref = CpuLinearBf16(attn, W_o, kT, kHs, kNq * kHd);

  // Compare output.
  std::vector<uint16_t> out_dev(static_cast<size_t>(kT) * kHs);
  cudaMemcpy(out_dev.data(), d_out, out_dev.size() * sizeof(uint16_t),
             cudaMemcpyDeviceToHost);
  const float out_err = L2RelErr(FromBf16(out_dev), out_ref);
  std::printf("  out l2_rel_err = %.3e\n", out_err);

  // Cleanup.
  cudaFree(d_x);
  cudaFree(d_out);
  cudaFree(d_kv);
  cudaFree(d_page_table);
  cudaFree(d_idx_raw);
  cudaFree(d_idx_comp);
  cudaFree(d_rope_pos);
  cudaFree(d_ws);
  w.Free();

  Q4T_CHECK(out_err < 3e-2f);
  return true;
}
