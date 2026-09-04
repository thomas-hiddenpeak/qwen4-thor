// Tests for the NVFP4 routed-expert MoE weight loader, against the real
// checkpoint. Verifies:
//   1. Packed weights match the shard bytes (gate/up merged, down).
//   2. Swizzled scale factors un-swizzle back to the source weight_scale bytes.
//   3. gate_proj and up_proj share identical weight_scale_2 / input_scale.
//   4. A full W4A4 GEMM on a loaded expert matches a CPU dequant reference.
// Skipped (reported as pass) when the real model directory is absent.
#include "q4t/io/weight_loader.h"
#include "q4t/quant/fp4_gemm.h"
#include "q4t/quant/format.h"
#include "q4t/quant/moe_weights.h"
#include "q4t/quant/swizzle.h"
#include "q4t/test.h"

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
using q4t::quant::E2m1ToFloat;
using q4t::quant::E4m3ToFloat;
using q4t::quant::Fp4Gemm;
using q4t::quant::LoadMoEWeights;
using q4t::quant::MoEWeightLayout;
using q4t::quant::SfOffset;
using q4t::quant::SfNumGtiles;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

// Real qwen4_exp routed-expert dims.
const int kE = 512;
const int kHs = 2560;
const int kMoeIs = 640;
const int kLayer = 2;  // a real MoE layer

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

struct Ctx {
  WeightIndex* idx = nullptr;
  WeightLoader* loader = nullptr;
};

bool OpenCtx(Ctx* c) {
  if (!FileExists(kIndex)) return false;
  if (!WeightIndex::Open(kIndex, &c->idx).ok()) return false;
  if (!WeightLoader::Create(kModelDir, *c->idx, 16, &c->loader).ok())
    return false;
  return true;
}

void FreeLayout(MoEWeightLayout* w) {
  if (!w) return;
  if (w->gu_packed) cudaFree(w->gu_packed);
  if (w->gu_sf) cudaFree(w->gu_sf);
  if (w->dn_packed) cudaFree(w->dn_packed);
  if (w->dn_sf) cudaFree(w->dn_sf);
  if (w->gu_w_scale2) cudaFree(w->gu_w_scale2);
  if (w->gu_input_scale) cudaFree(w->gu_input_scale);
  if (w->dn_w_scale2) cudaFree(w->dn_w_scale2);
  if (w->dn_input_scale) cudaFree(w->dn_input_scale);
  w->gu_packed = w->gu_sf = w->dn_packed = w->dn_sf = nullptr;
  w->gu_w_scale2 = w->gu_input_scale = w->dn_w_scale2 = w->dn_input_scale =
      nullptr;
}

std::string Name(int e, const char* proj, const char* suf) {
  return "model.language_model.layers." + std::to_string(kLayer) +
         ".mlp.experts." + std::to_string(e) + "." + proj + "." + suf;
}

}  // namespace

Q4T_TEST(moe_load_packed_matches_shard) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  Ctx c;
  if (!OpenCtx(&c)) {
    std::printf("  (skipped: real model not present)\n");
    return true;
  }
  MoEWeightLayout w;
  Status s = LoadMoEWeights(*c.loader, kLayer, kE, kHs, kMoeIs, &w, 0);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  // Verify packed weights for a few experts match the shard bytes.
  const int experts[] = {0, 100, 511};
  const size_t gu_w_bytes = static_cast<size_t>(kMoeIs) * (kHs / 2);
  const size_t dn_w_bytes = static_cast<size_t>(kHs) * (kMoeIs / 2);
  std::vector<uint8_t> gate(gu_w_bytes), up(gu_w_bytes), dn(dn_w_bytes);
  for (int e : experts) {
    if (!c.loader->ReadTensor(Name(e, "gate_proj", "weight"), gate.data())
             .ok())
      return false;
    if (!c.loader->ReadTensor(Name(e, "up_proj", "weight"), up.data()).ok())
      return false;
    if (!c.loader->ReadTensor(Name(e, "down_proj", "weight"), dn.data()).ok())
      return false;
    std::vector<uint8_t> gu_host(gu_w_bytes * 2), dn_host(dn_w_bytes);
    if (cudaMemcpy(gu_host.data(), w.gu_packed_expert(e), gu_host.size(),
                   cudaMemcpyDeviceToHost) != cudaSuccess)
      return false;
    if (cudaMemcpy(dn_host.data(), w.dn_packed_expert(e), dn_host.size(),
                   cudaMemcpyDeviceToHost) != cudaSuccess)
      return false;
    // gate rows then up rows.
    if (std::memcmp(gu_host.data(), gate.data(), gu_w_bytes) != 0) {
      std::printf("  gate packed mismatch expert %d\n", e);
      return false;
    }
    if (std::memcmp(gu_host.data() + gu_w_bytes, up.data(), gu_w_bytes) != 0) {
      std::printf("  up packed mismatch expert %d\n", e);
      return false;
    }
    if (std::memcmp(dn_host.data(), dn.data(), dn_w_bytes) != 0) {
      std::printf("  down packed mismatch expert %d\n", e);
      return false;
    }
  }
  FreeLayout(&w);
  delete c.loader;
  delete c.idx;
  return true;
}

Q4T_TEST(moe_load_sf_unswizzle_matches_shard) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  Ctx c;
  if (!OpenCtx(&c)) {
    std::printf("  (skipped: real model not present)\n");
    return true;
  }
  MoEWeightLayout w;
  Status s = LoadMoEWeights(*c.loader, kLayer, kE, kHs, kMoeIs, &w, 0);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  const int experts[] = {0, 255, 511};
  const int gu_groups = kHs / 16;
  const int dn_groups = kMoeIs / 16;
  const int num_gu_tiles = SfNumGtiles(kHs);
  const int num_dn_tiles = SfNumGtiles(kMoeIs);
  std::vector<uint8_t> gate_s(static_cast<size_t>(kMoeIs) * gu_groups);
  std::vector<uint8_t> up_s(static_cast<size_t>(kMoeIs) * gu_groups);
  std::vector<uint8_t> dn_s(static_cast<size_t>(kHs) * dn_groups);
  for (int e : experts) {
    if (!c.loader
             ->ReadTensor(Name(e, "gate_proj", "weight_scale"), gate_s.data())
             .ok())
      return false;
    if (!c.loader
             ->ReadTensor(Name(e, "up_proj", "weight_scale"), up_s.data())
             .ok())
      return false;
    if (!c.loader
             ->ReadTensor(Name(e, "down_proj", "weight_scale"), dn_s.data())
             .ok())
      return false;

    std::vector<uint8_t> gu_sf_host(w.gu_sf_block()), dn_sf_host(w.dn_sf_block());
    if (cudaMemcpy(gu_sf_host.data(), w.gu_sf_expert(e), gu_sf_host.size(),
                   cudaMemcpyDeviceToHost) != cudaSuccess)
      return false;
    if (cudaMemcpy(dn_sf_host.data(), w.dn_sf_expert(e), dn_sf_host.size(),
                   cudaMemcpyDeviceToHost) != cudaSuccess)
      return false;

    // Un-swizzle the gate/up block (2*kMoeIs rows) and compare to the merged
    // source (gate rows then up rows).
    for (int r = 0; r < 2 * kMoeIs; ++r) {
      for (int g = 0; g < gu_groups; ++g) {
        const uint8_t got = gu_sf_host[SfOffset(r, g, num_gu_tiles)];
        const uint8_t* src =
            (r < kMoeIs) ? &gate_s[static_cast<size_t>(r) * gu_groups + g]
                         : &up_s[static_cast<size_t>(r - kMoeIs) * gu_groups +
                                 g];
        if (got != *src) {
          std::printf("  gu sf mismatch expert %d r=%d g=%d\n", e, r, g);
          return false;
        }
      }
    }
    // Un-swizzle the down block (kHs rows).
    for (int r = 0; r < kHs; ++r) {
      for (int g = 0; g < dn_groups; ++g) {
        const uint8_t got = dn_sf_host[SfOffset(r, g, num_dn_tiles)];
        const uint8_t src = dn_s[static_cast<size_t>(r) * dn_groups + g];
        if (got != src) {
          std::printf("  dn sf mismatch expert %d r=%d g=%d\n", e, r, g);
          return false;
        }
      }
    }
  }
  FreeLayout(&w);
  delete c.loader;
  delete c.idx;
  return true;
}

Q4T_TEST(moe_load_gate_up_share_scale) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  Ctx c;
  if (!OpenCtx(&c)) {
    std::printf("  (skipped: real model not present)\n");
    return true;
  }
  MoEWeightLayout w;
  Status s = LoadMoEWeights(*c.loader, kLayer, kE, kHs, kMoeIs, &w, 0);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }
  // gate and up share weight_scale_2 and input_scale (verified on checkpoint).
  for (int e = 0; e < kE; ++e) {
    float g_ws2, g_isc, u_ws2, u_isc;
    if (!c.loader
             ->ReadTensor(Name(e, "gate_proj", "weight_scale_2"), &g_ws2)
             .ok())
      return false;
    if (!c.loader
             ->ReadTensor(Name(e, "up_proj", "weight_scale_2"), &u_ws2).ok())
      return false;
    if (!c.loader
             ->ReadTensor(Name(e, "gate_proj", "input_scale"), &g_isc).ok())
      return false;
    if (!c.loader
             ->ReadTensor(Name(e, "up_proj", "input_scale"), &u_isc).ok())
      return false;
    if (g_ws2 != u_ws2 || g_isc != u_isc) {
      std::printf("  gate/up scale differ at expert %d\n", e);
      return false;
    }
    // Host copies must match the device-loaded values.
    if (w.gu_w_scale2_h[e] != g_ws2 || w.gu_input_scale_h[e] != g_isc) {
      std::printf("  host copy mismatch expert %d\n", e);
      return false;
    }
  }
  FreeLayout(&w);
  delete c.loader;
  delete c.idx;
  return true;
}

Q4T_TEST(moe_load_gemm_matches_reference) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  Ctx c;
  if (!OpenCtx(&c)) {
    std::printf("  (skipped: real model not present)\n");
    return true;
  }
  MoEWeightLayout w;
  Status s = LoadMoEWeights(*c.loader, kLayer, kE, kHs, kMoeIs, &w, 0);
  if (!s.ok()) {
    std::printf("  load failed: %s\n", s.message().c_str());
    return false;
  }

  const int e = 0;
  const int M = 8;
  const size_t kWorkspace = 32 * 1024 * 1024;
  void* d_ws = nullptr;
  cudaMalloc(&d_ws, kWorkspace);

  // Random activation [M, hs], quantized to NVFP4 on host with the SAME
  // convention as the act_quant kernel: e4m3 = round(block_scale / input_scale),
  // e2m1 rounded against (e4m3 * input_scale). The dequant reference below
  // reconstructs a_recon = e2m1 * e4m3 * input_scale ~= a.
  const float inv_a = w.gu_input_scale_h[0];
  std::mt19937 rng(99);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  const int groups = kHs / 16;
  std::vector<uint8_t> a_packed(static_cast<size_t>(M) * (kHs / 2));
  std::vector<uint8_t> a_sf_rm(static_cast<size_t>(M) * groups);
  for (int m = 0; m < M; ++m) {
    for (int g = 0; g < groups; ++g) {
      float a[16];
      float gmax = 0.0f;
      for (int j = 0; j < 16; ++j) {
        a[j] = dist(rng);
        gmax = std::fmax(gmax, std::fabs(a[j]));
      }
      const float block_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
      const uint8_t sf = q4t::quant::FloatToE4m3(block_scale / inv_a);
      const float eff = E4m3ToFloat(sf) * inv_a;
      const float inv = eff > 0.0f ? 1.0f / eff : 0.0f;
      a_sf_rm[static_cast<size_t>(m) * groups + g] = sf;
      for (int j = 0; j < 8; ++j) {
        const int c0 = q4t::quant::FloatToE2m1Code(a[2 * j] * inv);
        const int c1 = q4t::quant::FloatToE2m1Code(a[2 * j + 1] * inv);
        a_packed[static_cast<size_t>(m) * (kHs / 2) + g * 8 + j] =
            static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
      }
    }
  }
  std::vector<uint8_t> a_sf_sw =
      q4t::quant::SwizzleSf(a_sf_rm.data(), M, kHs);
  uint8_t* d_a = nullptr;
  uint8_t* d_asf = nullptr;
  cudaMalloc(&d_a, a_packed.size());
  cudaMalloc(&d_asf, a_sf_sw.size());
  cudaMemcpy(d_a, a_packed.data(), a_packed.size(), cudaMemcpyHostToDevice);
  cudaMemcpy(d_asf, a_sf_sw.data(), a_sf_sw.size(), cudaMemcpyHostToDevice);

  // CPU reference: dequantize the loaded gate/up weights (expert e) and the
  // activation, then FP32 GEMM. (inv_a was defined above from the same expert.)
  const float inv_w = w.gu_w_scale2_h[e];
  const size_t gu_w_bytes = static_cast<size_t>(2 * kMoeIs) * (kHs / 2);
  std::vector<uint8_t> gu_host(gu_w_bytes);
  cudaMemcpy(gu_host.data(), w.gu_packed_expert(e), gu_w_bytes,
             cudaMemcpyDeviceToHost);
  std::vector<uint8_t> gu_sf_host(w.gu_sf_block());
  cudaMemcpy(gu_sf_host.data(), w.gu_sf_expert(e), gu_sf_host.size(),
             cudaMemcpyDeviceToHost);
  const int num_gu_tiles = SfNumGtiles(kHs);
  auto dequant_row = [&](int row, int K, const uint8_t* packed,
                         const uint8_t* sf_block, int num_tiles,
                         float inv_global) {
    std::vector<float> out(K);
    const int gg = K / 16;
    for (int g = 0; g < gg; ++g) {
      const float gs = E4m3ToFloat(sf_block[SfOffset(row, g, num_tiles)]) *
                       inv_global;
      const uint8_t* p = packed + (static_cast<size_t>(row) * (K / 2) + g * 8);
      for (int j = 0; j < 8; ++j) {
        out[g * 16 + 2 * j] = E2m1ToFloat(p[j] & 0xF) * gs;
        out[g * 16 + 2 * j + 1] = E2m1ToFloat((p[j] >> 4) & 0xF) * gs;
      }
    }
    return out;
  };
  // Dequant activation rows (row-major, using a_sf_rm).
  std::vector<std::vector<float>> a_dq(M);
  for (int m = 0; m < M; ++m) {
    a_dq[m].resize(kHs);
    for (int g = 0; g < groups; ++g) {
      const float gs = E4m3ToFloat(a_sf_rm[static_cast<size_t>(m) * groups + g]) *
                       inv_a;
      const uint8_t* p =
          &a_packed[static_cast<size_t>(m) * (kHs / 2) + g * 8];
      for (int j = 0; j < 8; ++j) {
        a_dq[m][g * 16 + 2 * j] = E2m1ToFloat(p[j] & 0xF) * gs;
        a_dq[m][g * 16 + 2 * j + 1] = E2m1ToFloat((p[j] >> 4) & 0xF) * gs;
      }
    }
  }

  // Run the W4A4 GEMM for the merged gate/up (N = 2*kMoeIs, K = kHs).
  float* d_out = nullptr;
  cudaMalloc(&d_out, static_cast<size_t>(M) * 2 * kMoeIs * sizeof(float));
  auto res = Fp4Gemm(w.gu_packed_expert(e), w.gu_sf_expert(e), d_a, d_asf,
                     d_out, M, 2 * kMoeIs, kHs, inv_w, inv_a, d_ws, kWorkspace,
                     0);
  if (res.status != CUBLAS_STATUS_SUCCESS || !res.has_algo) {
    std::printf("  GEMM no algo (status=%d)\n", (int)res.status);
    return false;
  }
  if (cudaDeviceSynchronize() != cudaSuccess) return false;
  std::vector<float> got(static_cast<size_t>(M) * 2 * kMoeIs);
  cudaMemcpy(got.data(), d_out, got.size() * sizeof(float),
             cudaMemcpyDeviceToHost);

  double max_rel = 0.0;
  for (int m = 0; m < M; ++m) {
    for (int n = 0; n < 2 * kMoeIs; ++n) {
      const std::vector<float> wrow =
          dequant_row(n, kHs, gu_host.data(), gu_sf_host.data(), num_gu_tiles,
                      inv_w);
      float acc = 0.0f;
      for (int k = 0; k < kHs; ++k) acc += a_dq[m][k] * wrow[k];
      double denom = std::fmax(1.0, std::fabs((double)acc));
      max_rel = std::fmax(
          max_rel,
          std::fabs((double)got[static_cast<size_t>(m) * 2 * kMoeIs + n] -
                    (double)acc) /
              denom);
    }
  }
  std::printf("  gate/up GEMM expert %d M=%d max_rel=%.5f\n", e, M, max_rel);
  const bool pass = max_rel < 0.02;

  cudaFree(d_ws);
  cudaFree(d_a);
  cudaFree(d_asf);
  cudaFree(d_out);
  FreeLayout(&w);
  delete c.loader;
  delete c.idx;
  return pass;
}
