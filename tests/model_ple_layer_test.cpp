// Test for the PLE layer forward (model/ple_layer.h) against the real
// checkpoint. Loads the layer-1 PLE weights (checkpoint `ple_layer_ids = [2]`
// is 1-indexed -> 0-indexed layer 1) and verifies the full forward
// (key/value proj + 3x GroupedGemmaRMSNorm + gate + depthwise causal conv)
// against a CPU reference. Skipped when CUDA or the model is absent.
#include "q4t/io/weight_loader.h"
#include "q4t/model/ple_layer.h"
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
using q4t::model::LoadPleLayer;
using q4t::model::PleLayerForward;
using q4t::model::PleLayerWeights;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const char* kPlePrefix = "model.language_model.layers.1.ple";
const int kHs = 2560;
const int kHc = 4;
const int kPe = 2560;  // ple_embed_dim
const int kConvK = 4;
const int kConvDil = 3;  // ngram_size
const float kEps = 1e-6f;
const int kT = 8;
const int kHcDim = kHc * kHs;

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
float L2RelErr(const std::vector<float>& a, const std::vector<float>& b) {
  double num = 0.0, den = 0.0;
  for (size_t i = 0; i < a.size(); ++i) {
    const double d = double(a[i]) - double(b[i]);
    num += d * d;
    den += double(b[i]) * double(b[i]);
  }
  return float(std::sqrt(num) / (std::sqrt(den) + 1e-6));
}
std::vector<float> FromBf16(const std::vector<uint16_t>& v) {
  std::vector<float> out(v.size());
  for (size_t i = 0; i < v.size(); ++i) out[i] = Bf16ToFloat(v[i]);
  return out;
}
// GroupedGemmaRMSNorm: split `x` (length hc*hs) into hc groups of hs, RMSNorm
// each group, scale by (1 + w). `w` is length hc*hs.
std::vector<float> GroupedRmsNorm(const std::vector<float>& x,
                                  const std::vector<float>& w, int hc, int hs,
                                  float eps) {
  std::vector<float> out(x.size());
  for (int b = 0; b < hc; ++b) {
    double acc = 0.0;
    for (int c = 0; c < hs; ++c) {
      const double v = x[b * hs + c];
      acc += v * v;
    }
    const float rs = 1.0f / float(std::sqrt(acc / hs + eps));
    for (int c = 0; c < hs; ++c) {
      out[b * hs + c] = x[b * hs + c] * rs * (1.0f + w[b * hs + c]);
    }
  }
  return out;
}

}  // namespace

Q4T_TEST(ple_layer_forward) {
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

  PleLayerWeights w;
  s = LoadPleLayer(*loader, kPlePrefix, kHc, kHs, kPe, kConvK, kConvDil, kEps,
                   &w, nullptr);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  auto host_copy = [&](const uint16_t* dev, size_t n) -> std::vector<float> {
    std::vector<uint16_t> h(n);
    if (cudaMemcpy(h.data(), dev, n * sizeof(uint16_t),
                   cudaMemcpyDeviceToHost) != cudaSuccess)
      return std::vector<float>();
    return FromBf16(h);
  };
  std::vector<float> key_proj = host_copy(w.key_proj, size_t(kHcDim) * kPe);
  std::vector<float> value_proj = host_copy(w.value_proj, size_t(kHs) * kPe);
  std::vector<float> norm_key = host_copy(w.norm_key, kHcDim);
  std::vector<float> norm_query = host_copy(w.norm_query, kHcDim);
  std::vector<float> norm_conv = host_copy(w.norm_conv, kHcDim);
  std::vector<float> conv1d = host_copy(w.conv1d, size_t(kHcDim) * kConvK);
  if (key_proj.empty() || value_proj.empty() || norm_key.empty() ||
      norm_query.empty() || norm_conv.empty() || conv1d.empty()) {
    std::printf("  host weight read failed\n");
    w.Free();
    return false;
  }

  // Random inputs: embeddings [T, pe], hyper_input [T, hc*hs].
  std::mt19937 rng(12345);
  std::normal_distribution<float> dist(0.0f, 0.5f);
  std::vector<float> emb(kT * kPe), hyper(kT * kHcDim);
  for (auto& v : emb) v = dist(rng);
  for (auto& v : hyper) v = dist(rng);
  std::vector<uint16_t> emb_bf(emb.size()), hyper_bf(hyper.size());
  for (size_t i = 0; i < emb.size(); ++i) emb_bf[i] = FloatToBf16(emb[i]);
  for (size_t i = 0; i < hyper.size(); ++i) hyper_bf[i] = FloatToBf16(hyper[i]);

  // Device buffers.
  uint16_t* d_emb = nullptr, *d_hyper = nullptr, *d_out = nullptr;
  void* d_ws = nullptr;
  const size_t kWs = 160 * 1024 * 1024;  // 160 MiB
  if (cudaMalloc(&d_emb, emb_bf.size() * sizeof(uint16_t)) != cudaSuccess ||
      cudaMalloc(&d_hyper, hyper_bf.size() * sizeof(uint16_t)) != cudaSuccess ||
      cudaMalloc(&d_out, size_t(kT) * kHcDim * sizeof(uint16_t)) != cudaSuccess ||
      cudaMalloc(&d_ws, kWs) != cudaSuccess) {
    std::printf("  cudaMalloc failed\n");
    w.Free();
    return false;
  }
  cudaMemcpy(d_emb, emb_bf.data(), emb_bf.size() * sizeof(uint16_t),
             cudaMemcpyHostToDevice);
  cudaMemcpy(d_hyper, hyper_bf.data(), hyper_bf.size() * sizeof(uint16_t),
             cudaMemcpyHostToDevice);

  s = PleLayerForward(w, d_emb, d_hyper, d_out, kT, d_ws, kWs, nullptr);
  if (!s.ok()) {
    std::printf("  forward failed: %s\n", s.message().c_str());
    w.Free();
    return false;
  }

  std::vector<uint16_t> out_bf(size_t(kT) * kHcDim);
  cudaMemcpy(out_bf.data(), d_out, out_bf.size() * sizeof(uint16_t),
             cudaMemcpyDeviceToHost);
  std::vector<float> out = FromBf16(out_bf);

  // ---- CPU reference ----
  // 1. key = emb @ key_proj^T  [T, hc_dim]; value = emb @ value_proj^T [T, hs].
  std::vector<float> key(kT * kHcDim), value(kT * kHs);
  for (int t = 0; t < kT; ++t) {
    for (int n = 0; n < kHcDim; ++n) {
      double acc = 0.0;
      for (int k = 0; k < kPe; ++k)
        acc += emb[t * kPe + k] * key_proj[n * kPe + k];
      key[t * kHcDim + n] = float(acc);
    }
    for (int n = 0; n < kHs; ++n) {
      double acc = 0.0;
      for (int k = 0; k < kPe; ++k)
        acc += emb[t * kPe + k] * value_proj[n * kPe + k];
      value[t * kHs + n] = float(acc);
    }
  }
  // 3-4. norms.
  std::vector<float> key_n(kT * kHcDim), query_n(kT * kHcDim);
  for (int t = 0; t < kT; ++t) {
    std::vector<float> kr(key.begin() + t * kHcDim,
                          key.begin() + (t + 1) * kHcDim);
    std::vector<float> qr(hyper.begin() + t * kHcDim,
                          hyper.begin() + (t + 1) * kHcDim);
    std::vector<float> kn = GroupedRmsNorm(kr, norm_key, kHc, kHs, kEps);
    std::vector<float> qn = GroupedRmsNorm(qr, norm_query, kHc, kHs, kEps);
    for (int i = 0; i < kHcDim; ++i) {
      key_n[t * kHcDim + i] = kn[i];
      query_n[t * kHcDim + i] = qn[i];
    }
  }
  // 5. gate.
  std::vector<float> gate(kT * kHc);
  const float inv_sqrt_hs = 1.0f / std::sqrt(float(kHs));
  for (int t = 0; t < kT; ++t) {
    for (int b = 0; b < kHc; ++b) {
      double acc = 0.0;
      for (int c = 0; c < kHs; ++c)
        acc += key_n[t * kHcDim + b * kHs + c] *
               query_n[t * kHcDim + b * kHs + c];
      float raw = float(acc) * inv_sqrt_hs;
      float g = std::sqrt(std::fabs(raw) + 1e-6f) * (raw < 0.0f ? -1.0f : 1.0f);
      gate[t * kHc + b] = 1.0f / (1.0f + std::exp(-g));
    }
  }
  // 6. gated_value.
  std::vector<float> gated(kT * kHcDim);
  for (int t = 0; t < kT; ++t)
    for (int b = 0; b < kHc; ++b)
      for (int c = 0; c < kHs; ++c)
        gated[t * kHcDim + b * kHs + c] = gate[t * kHc + b] * value[t * kHs + c];
  // 7. gated_n.
  std::vector<float> gated_n(kT * kHcDim);
  for (int t = 0; t < kT; ++t) {
    std::vector<float> gr(gated.begin() + t * kHcDim,
                          gated.begin() + (t + 1) * kHcDim);
    std::vector<float> gn = GroupedRmsNorm(gr, norm_conv, kHc, kHs, kEps);
    for (int i = 0; i < kHcDim; ++i) gated_n[t * kHcDim + i] = gn[i];
  }
  // 8. conv_out = silu(depthwise causal conv(gated_n)).
  std::vector<float> conv_out(kT * kHcDim);
  for (int t = 0; t < kT; ++t) {
    for (int ch = 0; ch < kHcDim; ++ch) {
      double acc = 0.0;
      for (int j = 0; j < kConvK; ++j) {
        const int src = t - (kConvK - 1 - j) * kConvDil;
        if (src < 0) continue;
        acc += conv1d[ch * kConvK + j] * gated_n[src * kHcDim + ch];
      }
      const float v = float(acc);
      conv_out[t * kHcDim + ch] = v / (1.0f + std::exp(-v));
    }
  }
  // 9. out = gated + conv_out.
  std::vector<float> ref(kT * kHcDim);
  for (size_t i = 0; i < ref.size(); ++i) ref[i] = gated[i] + conv_out[i];

  const float err = L2RelErr(out, ref);
  std::printf("  ple_layer out l2_rel_err = %.3e\n", err);
  Q4T_CHECK(err < 3e-2f);

  cudaFree(d_emb);
  cudaFree(d_hyper);
  cudaFree(d_out);
  cudaFree(d_ws);
  w.Free();
  return true;
}
