// Test for the linear_attention (Gated DeltaNet SSM) block against the real
// checkpoint. Loads the layer-2 linear_attn weights, runs the forward on a
// random [T, hs] input with zero-initialized SSM/conv state, and compares the
// output (and final SSM state) against a full CPU reference that mirrors the
// projections, causal conv1d (SiLU), the Gated DeltaNet recurrence, the
// per-head RMSNorm*sigmoid gate (output_gate_type), and the output
// projection. Skipped (reported as pass) when CUDA or the real model is
// absent.
#include "q4t/io/weight_loader.h"
#include "q4t/model/linear_attention.h"
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
using q4t::model::LinearAttentionForward;
using q4t::model::LinearAttentionWeights;
using q4t::model::LoadLinearAttention;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const int kHs = 2560;
const int kNkh = 16;
const int kNv = 48;
const int kKd = 128;
const int kVd = 128;
const int kConvK = 4;
const float kEps = 1e-6f;
const int kT = 4;
const std::string kPrefix = "model.language_model.layers.2.linear_attn";

const int kQk = kNkh * kKd;  // 2048
const int kVDim = kNv * kVd;  // 6144
const int kInQkv = 2 * kQk + kVDim;  // 10240

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
float Silu(float v) { return v / (1.0f + std::exp(-v)); }
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

}  // namespace

Q4T_TEST(linear_attention_forward) {
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

  LinearAttentionWeights w;
  s = LoadLinearAttention(*loader, kPrefix, kHs, kNkh, kNv, kKd, kVd, kConvK,
                          kEps, &w, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  // Host copies of the weights for the CPU reference.
  auto read_host = [&](const std::string& name, size_t n) {
    std::vector<uint16_t> raw(n);
    Status ls = loader->ReadTensor(name, raw.data());
    if (!ls.ok()) return std::vector<float>();
    return FromBf16(raw);
  };
  std::vector<float> W_qkv = read_host(kPrefix + ".in_proj_qkv.weight",
                                       static_cast<size_t>(kInQkv) * kHs);
  std::vector<float> W_z = read_host(kPrefix + ".in_proj_z.weight",
                                     static_cast<size_t>(kVDim) * kHs);
  std::vector<float> W_a = read_host(kPrefix + ".in_proj_a.weight",
                                     static_cast<size_t>(kNv) * kHs);
  std::vector<float> W_b = read_host(kPrefix + ".in_proj_b.weight",
                                     static_cast<size_t>(kNv) * kHs);
  std::vector<float> W_conv = read_host(kPrefix + ".conv1d.weight",
                                        static_cast<size_t>(kInQkv) * kConvK);
  std::vector<float> W_out = read_host(kPrefix + ".out_proj.weight",
                                       static_cast<size_t>(kHs) * kVDim);
  std::vector<float> norm_w = read_host(kPrefix + ".norm.weight", kVd);
  std::vector<float> A_log = read_host(kPrefix + ".A_log", kNv);
  std::vector<float> dt_bias = read_host(kPrefix + ".dt_bias", kNv);
  if (W_qkv.empty() || W_z.empty() || W_a.empty() || W_b.empty() ||
      W_conv.empty() || W_out.empty() || norm_w.empty() || A_log.empty() ||
      dt_bias.empty()) {
    std::printf("  host weight read failed\n");
    w.Free();
    return false;
  }

  // Random input x [T, hs] and zero-initialized states.
  std::mt19937 rng(98765);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  std::vector<float> x_in(static_cast<size_t>(kT) * kHs);
  for (auto& v : x_in) v = dist(rng);
  std::vector<uint16_t> x_bf = ToBf16(x_in);
  std::vector<float> ssm0(static_cast<size_t>(kNv) * kKd * kVd, 0.0f);
  std::vector<uint16_t> conv0(static_cast<size_t>(kInQkv) * (kConvK - 1), 0);

  // Device buffers.
  uint16_t* d_x = nullptr;
  uint16_t* d_out = nullptr;
  float* d_ssm = nullptr;
  uint16_t* d_conv = nullptr;
  void* d_ws = nullptr;
  const size_t ws_bytes = 64u * 1024u * 1024u;
  if (cudaMalloc(reinterpret_cast<void**>(&d_x),
                x_bf.size() * sizeof(uint16_t)) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_out),
                 static_cast<size_t>(kT) * kHs * sizeof(uint16_t)) !=
          cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_ssm),
                 ssm0.size() * sizeof(float)) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&d_conv),
                 conv0.size() * sizeof(uint16_t)) != cudaSuccess ||
      cudaMalloc(&d_ws, ws_bytes) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    w.Free();
    return false;
  }
  cudaMemcpy(d_x, x_bf.data(), x_bf.size() * sizeof(uint16_t),
             cudaMemcpyHostToDevice);
  cudaMemcpy(d_ssm, ssm0.data(), ssm0.size() * sizeof(float),
             cudaMemcpyHostToDevice);
  cudaMemcpy(d_conv, conv0.data(), conv0.size() * sizeof(uint16_t),
             cudaMemcpyHostToDevice);

  s = LinearAttentionForward(w, d_x, d_out, d_ssm, d_conv, kT, d_ws, ws_bytes,
                             nullptr);
  if (!s.ok()) {
    std::printf("  forward failed: %s\n", s.message().c_str());
    w.Free();
    return false;
  }

  // ---------------- CPU reference ----------------
  // 1. Projections (BF16-rounded).
  std::vector<float> qkv_raw = CpuLinearBf16(x_in, W_qkv, kT, kInQkv, kHs);
  std::vector<float> z = CpuLinearBf16(x_in, W_z, kT, kVDim, kHs);
  std::vector<float> a = CpuLinearBf16(x_in, W_a, kT, kNv, kHs);
  std::vector<float> beta = CpuLinearBf16(x_in, W_b, kT, kNv, kHs);

  // 2. Causal conv1d (SiLU) with zero initial state.
  const int hist = kConvK - 1;
  std::vector<float> conv_state(static_cast<size_t>(kInQkv) * hist, 0.0f);
  std::vector<float> qkv(static_cast<size_t>(kT) * kInQkv);
  for (int t = 0; t < kT; ++t) {
    for (int ch = 0; ch < kInQkv; ++ch) {
      float acc = 0.0f;
      for (int k = 0; k < kConvK; ++k) {
        const int src_t = t - (hist - k);
        float val;
        if (src_t < 0)
          val = conv_state[static_cast<size_t>(ch) * hist + (src_t + hist)];
        else
          val = qkv_raw[static_cast<size_t>(src_t) * kInQkv + ch];
        acc += val * W_conv[static_cast<size_t>(ch) * kConvK + k];
      }
      qkv[static_cast<size_t>(t) * kInQkv + ch] = Bf16Round(Silu(acc));
    }
  }
  // Update conv_state to the last `hist` raw inputs.
  for (int ch = 0; ch < kInQkv; ++ch)
    for (int k = 0; k < hist; ++k) {
      const int src_t = kT - hist + k;
      if (src_t >= 0)
        conv_state[static_cast<size_t>(ch) * hist + k] =
            qkv_raw[static_cast<size_t>(src_t) * kInQkv + ch];
    }

  // 3. Gated DeltaNet recurrence. S kept in FP32 (mirrors device smem).
  std::vector<float> S(static_cast<size_t>(kNv) * kKd * kVd, 0.0f);
  std::vector<float> y_ssm(static_cast<size_t>(kT) * kVDim, 0.0f);
  const int nv_per_kh = kNv / kNkh;
  const float q_scale = 1.0f / std::sqrt(static_cast<float>(kKd));
  for (int t = 0; t < kT; ++t) {
    for (int h_v = 0; h_v < kNv; ++h_v) {
      const int h_k = h_v / nv_per_kh;
      const float* q = &qkv[static_cast<size_t>(t) * kInQkv + h_k * kKd];
      const float* k =
          &qkv[static_cast<size_t>(t) * kInQkv + kNkh * kKd + h_k * kKd];
      const float* v = &qkv[static_cast<size_t>(t) * kInQkv + 2 * kNkh * kKd +
                            h_v * kVd];
      // k_hat, q_hat (FP32).
      float k_sq = 0.0f, q_sq = 0.0f;
      for (int i = 0; i < kKd; ++i) {
        k_sq += k[i] * k[i];
        q_sq += q[i] * q[i];
      }
      // L2-style normalization (NOT RMSNorm): 1/sqrt(sum(x^2) + eps), no /kd.
      const float k_norm = 1.0f / std::sqrt(k_sq + 1e-6f);
      const float q_norm = 1.0f / std::sqrt(q_sq + 1e-6f) * q_scale;
      std::vector<float> k_hat(kKd), q_hat(kKd);
      for (int i = 0; i < kKd; ++i) {
        k_hat[i] = k[i] * k_norm;
        q_hat[i] = q[i] * q_norm;
      }
      const float ab = a[static_cast<size_t>(t) * kNv + h_v] + dt_bias[h_v];
      const float dt_v = (ab > 20.0f) ? ab : std::log1p(std::exp(ab));
      const float alpha = std::exp(-dt_v * std::exp(A_log[h_v]));
      const float beta_v =
          Sigmoid(beta[static_cast<size_t>(t) * kNv + h_v]);
      float* S_hv = &S[static_cast<size_t>(h_v) * kKd * kVd];
      for (int j = 0; j < kVd; ++j) {
        float kS = 0.0f;
        for (int i = 0; i < kKd; ++i) kS += k_hat[i] * S_hv[i * kVd + j];
        const float delta = v[j] - alpha * kS;
        float y_j = 0.0f;
        for (int i = 0; i < kKd; ++i) {
          const float new_s = alpha * S_hv[i * kVd + j] + beta_v * k_hat[i] * delta;
          S_hv[i * kVd + j] = new_s;
          y_j += new_s * q_hat[i];
        }
        y_ssm[static_cast<size_t>(t) * kVDim + h_v * kVd + j] = Bf16Round(y_j);
      }
    }
  }

  // 4. Fused per-head RMSNorm * sigmoid(z) gate (output_gate_type).
  for (int t = 0; t < kT; ++t) {
    for (int h_v = 0; h_v < kNv; ++h_v) {
      float* yrow = &y_ssm[static_cast<size_t>(t) * kVDim + h_v * kVd];
      const float* zrow = &z[static_cast<size_t>(t) * kVDim + h_v * kVd];
      float sum_sq = 0.0f;
      for (int i = 0; i < kVd; ++i) sum_sq += yrow[i] * yrow[i];
      const float inv_rms = 1.0f / std::sqrt(sum_sq / kVd + kEps);
      for (int i = 0; i < kVd; ++i) {
        const float normalized = yrow[i] * inv_rms * norm_w[i];
        yrow[i] = Bf16Round(normalized * Sigmoid(zrow[i]));
      }
    }
  }

  // 5. Output projection (BF16-rounded).
  std::vector<float> out_ref = CpuLinearBf16(y_ssm, W_out, kT, kHs, kVDim);

  // Compare output.
  std::vector<uint16_t> out_dev(static_cast<size_t>(kT) * kHs);
  cudaMemcpy(out_dev.data(), d_out, out_dev.size() * sizeof(uint16_t),
             cudaMemcpyDeviceToHost);
  const float out_err = L2RelErr(FromBf16(out_dev), out_ref);
  std::printf("  out l2_rel_err = %.3e\n", out_err);

  // Compare final SSM state (FP32, no quantization — matches reference).
  std::vector<float> ssm_dev(ssm0.size());
  cudaMemcpy(ssm_dev.data(), d_ssm, ssm_dev.size() * sizeof(float),
             cudaMemcpyDeviceToHost);
  const float ssm_err = L2RelErr(ssm_dev, S);
  std::printf("  ssm_state l2_rel_err = %.3e\n", ssm_err);

  // Cleanup.
  cudaFree(d_x);
  cudaFree(d_out);
  cudaFree(d_ssm);
  cudaFree(d_conv);
  cudaFree(d_ws);
  w.Free();

  // BF16 intermediates (qkv/z/a/beta/y_ssm) limit precision; the recurrence
  // keeps S in FP32. L2 relative error should be at the ~BF16 epsilon scale.
  Q4T_CHECK(out_err < 2e-2f);
  Q4T_CHECK(ssm_err < 2e-2f);
  return true;
}
