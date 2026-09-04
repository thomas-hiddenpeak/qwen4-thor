// Tests for the NVFP4 quantization layer: e2m1/e4m3 format primitives, the
// scale-factor swizzle layout, the dequant kernel, the activation-quant
// kernel, and the native cuBLASLt W4A4 GEMM.
//
// The format and swizzle tests are pure host code. The kernel and GEMM tests
// require a CUDA device and are skipped (reported as pass) when none is
// available.
#include "q4t/quant/act_quant.h"
#include "q4t/quant/dequant.h"
#include "q4t/quant/fp4_gemm.h"
#include "q4t/quant/format.h"
#include "q4t/quant/swizzle.h"
#include "q4t/test.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

namespace {

using q4t::quant::E2m1ToFloat;
using q4t::quant::E4m3ToFloat;
using q4t::quant::FloatToE2m1Code;
using q4t::quant::FloatToE4m3;
using q4t::quant::QuantizeActivationToFp4Async;
using q4t::quant::DequantFp4ToBf16Async;
using q4t::quant::Fp4Gemm;
using q4t::quant::SfBufferSize;
using q4t::quant::SfOffset;
using q4t::quant::SfNumGtiles;
using q4t::quant::SwizzleSf;

bool CudaAvailable() {
  int count = 0;
  if (cudaGetDeviceCount(&count) != cudaSuccess) return false;
  return count > 0;
}

float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}

// Round a float to BF16 (round-to-nearest-even) and back to float, so a CPU
// reference matches the kernel's __float2bfloat16_rn output exactly.
float RoundToBf16(float v) {
  const __nv_bfloat16 b = __float2bfloat16_rn(v);
  return __bfloat162float(b);
}

// Host NVFP4 quantization of a row-major [rows, K] float matrix, matching the
// GPU kernels' convention: block_scale = gmax/6; e4m3 = round(block_scale /
// global_scale); e2m1 codes rounded against (e4m3 * global_scale). This is the
// convention that makes the GEMM (which multiplies by global_scale via alpha)
// reconstruct the input.
struct HostQuant {
  std::vector<uint8_t> packed;  // [rows, K/2]
  std::vector<uint8_t> sf;      // row-major [rows, K/16]
};

HostQuant HostQuantize(const std::vector<float>& v, int rows, int K,
                       float global_scale) {
  HostQuant h;
  const int groups = K / 16;
  h.packed.assign(static_cast<size_t>(rows) * (K / 2), 0);
  h.sf.assign(static_cast<size_t>(rows) * groups, 0);
  for (int r = 0; r < rows; ++r) {
    for (int g = 0; g < groups; ++g) {
      float gmax = 0.0f;
      for (int j = 0; j < 16; ++j)
        gmax = std::fmax(gmax,
                         std::fabs(v[static_cast<size_t>(r) * K + g * 16 + j]));
      const float block_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
      const uint8_t sf = FloatToE4m3(block_scale / global_scale);
      const float eff = E4m3ToFloat(sf) * global_scale;
      const float inv = eff > 0.0f ? 1.0f / eff : 0.0f;
      h.sf[static_cast<size_t>(r) * groups + g] = sf;
      for (int j = 0; j < 8; ++j) {
        const int c0 =
            FloatToE2m1Code(v[static_cast<size_t>(r) * K + g * 16 + 2 * j] * inv);
        const int c1 = FloatToE2m1Code(
            v[static_cast<size_t>(r) * K + g * 16 + 2 * j + 1] * inv);
        h.packed[static_cast<size_t>(r) * (K / 2) + g * 8 + j] =
            static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
      }
    }
  }
  return h;
}

// Dequantize a host-quantized NVFP4 matrix back to float [rows, K]:
// recon = e2m1 * e4m3 * global_scale.
std::vector<float> HostDequant(const HostQuant& h, int rows, int K,
                               float global_scale) {
  const int groups = K / 16;
  std::vector<float> out(static_cast<size_t>(rows) * K, 0.0f);
  for (int r = 0; r < rows; ++r) {
    for (int g = 0; g < groups; ++g) {
      const float gs =
          E4m3ToFloat(h.sf[static_cast<size_t>(r) * groups + g]) * global_scale;
      const uint8_t* p = &h.packed[static_cast<size_t>(r) * (K / 2) + g * 8];
      for (int j = 0; j < 8; ++j) {
        out[static_cast<size_t>(r) * K + g * 16 + 2 * j] =
            E2m1ToFloat(p[j] & 0xF) * gs;
        out[static_cast<size_t>(r) * K + g * 16 + 2 * j + 1] =
            E2m1ToFloat((p[j] >> 4) & 0xF) * gs;
      }
    }
  }
  return out;
}

}  // namespace

// ---------------------------------------------------------------------------
// Format primitives
// ---------------------------------------------------------------------------

Q4T_TEST(quant_e2m1_decode_table) {
  const float expect[16] = {0.0f,  0.5f, 1.0f,  1.5f, 2.0f, 3.0f, 4.0f, 6.0f,
                            -0.0f, -0.5f, -1.0f, -1.5f, -2.0f, -3.0f, -4.0f,
                            -6.0f};
  for (int c = 0; c < 16; ++c) {
    if (E2m1ToFloat(static_cast<uint8_t>(c)) != expect[c]) return false;
  }
  return true;
}

Q4T_TEST(quant_e2m1_encode_roundtrip) {
  // Each representable value encodes to its own code.
  const float vals[8] = {0.5f, 1.0f, 1.5f, 2.0f, 3.0f, 4.0f, 6.0f, 0.0f};
  const int codes[8] = {1, 2, 3, 4, 5, 6, 7, 0};
  for (int i = 0; i < 8; ++i) {
    if (FloatToE2m1Code(vals[i]) != codes[i]) return false;
  }
  // Ties go to the even code (round-to-nearest-even).
  if (FloatToE2m1Code(0.25f) != 0) return false;  // -> 0.0
  if (FloatToE2m1Code(0.75f) != 2) return false;  // -> 1.0
  if (FloatToE2m1Code(1.25f) != 2) return false;  // -> 1.0
  if (FloatToE2m1Code(1.75f) != 4) return false;  // -> 2.0
  if (FloatToE2m1Code(2.5f) != 4) return false;   // -> 2.0
  if (FloatToE2m1Code(3.5f) != 6) return false;   // -> 4.0
  if (FloatToE2m1Code(5.0f) != 6) return false;   // -> 4.0
  // Saturation.
  if (FloatToE2m1Code(100.0f) != 7) return false;
  if (FloatToE2m1Code(-100.0f) != 15) return false;
  // Sign.
  if (FloatToE2m1Code(-1.0f) != 10) return false;
  return true;
}

Q4T_TEST(quant_e4m3_roundtrip) {
  // Exact values encode/decode losslessly.
  const float vals[] = {0.0f,   0.5f,   1.0f,    1.5f,   2.0f,   3.0f,
                        4.0f,   6.0f,   448.0f,  0.015625f, 0.03125f,
                        128.0f, 0.75f,  2.25f,   0.0625f};
  for (float v : vals) {
    const uint8_t code = FloatToE4m3(v);
    if (E4m3ToFloat(code) != v) {
      std::printf("  e4m3 roundtrip failed for %f (code 0x%02x -> %f)\n", v,
                  code, E4m3ToFloat(code));
      return false;
    }
  }
  // Subnormal: 5 * 2^-9.
  if (FloatToE4m3(5.0f * 0.001953125f) != 5) return false;
  // Saturation to max normal 448.
  if (E4m3ToFloat(FloatToE4m3(1000.0f)) != 448.0f) return false;
  // Monotonicity over a sweep.
  float prev = 0.0f;
  for (int i = 0; i < 1000; ++i) {
    float v = 0.001f + i * 0.5f;
    float d = E4m3ToFloat(FloatToE4m3(v));
    if (d < prev - 1e-6f) return false;
    prev = d;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Swizzle layout
// ---------------------------------------------------------------------------

Q4T_TEST(quant_swizzle_offsets) {
  // Verified against CuTe tile_to_shape(SfAtom, (M,K), Step<_2,_1>).
  const int num_g_tiles_128 = SfNumGtiles(128);  // (128/16)/4 = 2
  if (num_g_tiles_128 != 2) return false;
  // M=128, K=128.
  if (SfOffset(0, 0, 2) != 0) return false;
  if (SfOffset(0, 1, 2) != 1) return false;
  if (SfOffset(0, 3, 2) != 3) return false;
  if (SfOffset(0, 4, 2) != 512) return false;
  if (SfOffset(0, 7, 2) != 515) return false;
  if (SfOffset(1, 0, 2) != 16) return false;
  if (SfOffset(31, 0, 2) != 496) return false;
  if (SfOffset(32, 0, 2) != 4) return false;
  if (SfOffset(127, 7, 2) != 1023) return false;
  // M=256, K=128: row 128 starts a new 128-row block.
  if (SfOffset(128, 0, 2) != 1024) return false;
  // K=2560: num_g_tiles = (2560/16)/4 = 40.
  if (SfNumGtiles(2560) != 40) return false;
  if (SfOffset(128, 0, 40) != 20480) return false;
  return true;
}

Q4T_TEST(quant_swizzle_buffer_size) {
  // Padded to whole 128-row atoms.
  if (SfBufferSize(1, 2560) != 20480) return false;   // 1 block * 40 * 512
  if (SfBufferSize(8, 640) != 5120) return false;     // 1 block * 10 * 512
  if (SfBufferSize(128, 640) != 5120) return false;
  if (SfBufferSize(129, 640) != 10240) return false;  // 2 blocks
  if (SfBufferSize(640, 2560) != 102400) return false;
  return true;
}

Q4T_TEST(quant_swizzle_roundtrip) {
  // Swizzle then un-swizzle (via the offset map) recovers the original.
  const int rows = 300, K = 640;
  const int groups = K / 16;
  std::vector<uint8_t> rm(static_cast<size_t>(rows) * groups);
  for (auto& b : rm) b = static_cast<uint8_t>(rand() & 0x7F);
  std::vector<uint8_t> sw = SwizzleSf(rm.data(), rows, K);
  if (sw.size() != SfBufferSize(rows, K)) return false;
  const int num_g_tiles = SfNumGtiles(K);
  std::vector<uint8_t> back(SfBufferSize(rows, K), 0);
  for (int r = 0; r < rows; ++r)
    for (int g = 0; g < groups; ++g)
      back[SfOffset(r, g, num_g_tiles)] =
          sw[SfOffset(r, g, num_g_tiles)];
  for (int r = 0; r < rows; ++r)
    for (int g = 0; g < groups; ++g)
      if (back[SfOffset(r, g, num_g_tiles)] !=
          rm[static_cast<size_t>(r) * groups + g])
        return false;
  return true;
}

// ---------------------------------------------------------------------------
// Dequant kernel (GPU)
// ---------------------------------------------------------------------------

Q4T_TEST(quant_dequant_kernel) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  const int N = 2, K = 32;  // 2 groups per row
  const int groups = K / 16;
  std::vector<uint8_t> packed(static_cast<size_t>(N) * (K / 2));
  std::vector<uint8_t> scale(static_cast<size_t>(N) * groups);
  for (auto& b : packed) b = static_cast<uint8_t>(rand() & 0xFF);
  for (auto& s : scale) s = static_cast<uint8_t>(rand() & 0x7F);
  const float inv_global = 0.37f;

  std::vector<uint16_t> out(static_cast<size_t>(N) * K, 0);
  uint8_t* d_packed;
  uint8_t* d_scale;
  uint16_t* d_out;
  cudaMalloc(&d_packed, packed.size());
  cudaMalloc(&d_scale, scale.size());
  cudaMalloc(&d_out, out.size() * 2);
  cudaMemcpy(d_packed, packed.data(), packed.size(), cudaMemcpyHostToDevice);
  cudaMemcpy(d_scale, scale.data(), scale.size(), cudaMemcpyHostToDevice);
  if (DequantFp4ToBf16Async(d_packed, d_scale, d_out, inv_global, N, K, 0) !=
      cudaSuccess)
    return false;
  if (cudaDeviceSynchronize() != cudaSuccess) return false;
  cudaMemcpy(out.data(), d_out, out.size() * 2, cudaMemcpyDeviceToHost);
  cudaFree(d_packed);
  cudaFree(d_scale);
  cudaFree(d_out);

  // CPU reference.
  for (int r = 0; r < N; ++r) {
    for (int g = 0; g < groups; ++g) {
      const float gs =
          E4m3ToFloat(scale[static_cast<size_t>(r) * groups + g]) * inv_global;
      const uint8_t* p = &packed[static_cast<size_t>(r) * (K / 2) + g * 8];
      for (int j = 0; j < 8; ++j) {
        if (Bf16ToFloat(out[static_cast<size_t>(r) * K + g * 16 + 2 * j]) !=
            RoundToBf16(E2m1ToFloat(p[j] & 0xF) * gs))
          return false;
        if (Bf16ToFloat(out[static_cast<size_t>(r) * K + g * 16 + 2 * j + 1]) !=
            RoundToBf16(E2m1ToFloat((p[j] >> 4) & 0xF) * gs))
          return false;
      }
    }
  }
  return true;
}

// ---------------------------------------------------------------------------
// Activation quant kernel (GPU)
// ---------------------------------------------------------------------------

Q4T_TEST(quant_act_quant_kernel) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  const int M = 5, K = 64;  // K multiple of 32
  const int groups = K / 16;
  std::mt19937 rng(7);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  std::vector<float> a(static_cast<size_t>(M) * K);
  for (auto& x : a) x = dist(rng);
  std::vector<uint16_t> a_bf16(a.size());
  std::vector<float> a_bf16f(a.size());
  for (size_t i = 0; i < a.size(); ++i) {
    const __nv_bfloat16 b = __float2bfloat16(a[i]);
    a_bf16[i] = *reinterpret_cast<const uint16_t*>(&b);
    a_bf16f[i] = __bfloat162float(b);  // what the GPU kernel actually sees
  }

  // Host reference must quantize the BF16-rounded values, not the originals.
  // Use a realistic activation global scale (checkpoint input_scale ~ 1.7e-3)
  // so the e4m3 = round(block_scale / global_scale) path is exercised.
  const float global_scale = 0.00167f;
  HostQuant hq = HostQuantize(a_bf16f, M, K, global_scale);
  std::vector<uint8_t> gpu_packed(hq.packed.size(), 0);
  std::vector<uint8_t> gpu_sf(SfBufferSize(M, K), 0);
  uint16_t* d_a;
  uint8_t* d_packed;
  uint8_t* d_sf;
  cudaMalloc(&d_a, a_bf16.size() * 2);
  cudaMalloc(&d_packed, gpu_packed.size());
  cudaMalloc(&d_sf, gpu_sf.size());
  cudaMemcpy(d_a, a_bf16.data(), a_bf16.size() * 2, cudaMemcpyHostToDevice);
  if (QuantizeActivationToFp4Async(d_a, d_packed, d_sf, M, K, global_scale,
                                   0) != cudaSuccess)
    return false;
  if (cudaDeviceSynchronize() != cudaSuccess) return false;
  cudaMemcpy(gpu_packed.data(), d_packed, gpu_packed.size(),
             cudaMemcpyDeviceToHost);
  cudaMemcpy(gpu_sf.data(), d_sf, gpu_sf.size(), cudaMemcpyDeviceToHost);
  cudaFree(d_a);
  cudaFree(d_packed);
  cudaFree(d_sf);

  // Packed payload must match the host reference exactly.
  if (gpu_packed != hq.packed) {
    std::printf("  packed mismatch\n");
    return false;
  }
  // Swizzled SF must equal the host row-major SF placed through the offsets.
  const int num_g_tiles = SfNumGtiles(K);
  for (int r = 0; r < M; ++r)
    for (int g = 0; g < groups; ++g)
      if (gpu_sf[SfOffset(r, g, num_g_tiles)] !=
          hq.sf[static_cast<size_t>(r) * groups + g]) {
        std::printf("  sf mismatch at (%d,%d)\n", r, g);
        return false;
      }
  return true;
}

// ---------------------------------------------------------------------------
// Native cuBLASLt W4A4 GEMM (GPU)
// ---------------------------------------------------------------------------

Q4T_TEST(quant_fp4_gemm_w4a4) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  const float inv_w_global = 0.29f;  // = weight_scale_2
  const float inv_a_global = 0.41f;  // = input_scale
  const size_t kWorkspace = 32 * 1024 * 1024;

  struct Shape {
    int N, K;
  };
  const Shape shapes[] = {{640, 2560}, {2560, 640}};
  const int Ms[] = {1, 8, 64, 256};

  std::mt19937 rng(1234);
  std::normal_distribution<float> dist(0.0f, 1.0f);
  void* d_ws = nullptr;
  cudaMalloc(&d_ws, kWorkspace);

  int total = 0, ok = 0;
  for (const auto& sh : shapes) {
    const int N = sh.N, K = sh.K;
    // Random weight [N, K] and quantize on host.
    std::vector<float> w(static_cast<size_t>(N) * K);
    for (auto& x : w) x = dist(rng);
    HostQuant wq = HostQuantize(w, N, K, inv_w_global);
    std::vector<uint8_t> w_sf_sw = SwizzleSf(wq.sf.data(), N, K);

    uint8_t* d_w = nullptr;
    uint8_t* d_wsf = nullptr;
    cudaMalloc(&d_w, wq.packed.size());
    cudaMalloc(&d_wsf, w_sf_sw.size());
    cudaMemcpy(d_w, wq.packed.data(), wq.packed.size(), cudaMemcpyHostToDevice);
    cudaMemcpy(d_wsf, w_sf_sw.data(), w_sf_sw.size(), cudaMemcpyHostToDevice);

    for (int M : Ms) {
      ++total;
      std::vector<float> a(static_cast<size_t>(M) * K);
      for (auto& x : a) x = dist(rng);
      HostQuant aq = HostQuantize(a, M, K, inv_a_global);
      std::vector<uint8_t> a_sf_sw = SwizzleSf(aq.sf.data(), M, K);

      uint8_t* d_a = nullptr;
      uint8_t* d_asf = nullptr;
      float* d_out = nullptr;
      cudaMalloc(&d_a, aq.packed.size());
      cudaMalloc(&d_asf, a_sf_sw.size());
      cudaMalloc(&d_out, static_cast<size_t>(M) * N * sizeof(float));
      cudaMemcpy(d_a, aq.packed.data(), aq.packed.size(),
                 cudaMemcpyHostToDevice);
      cudaMemcpy(d_asf, a_sf_sw.data(), a_sf_sw.size(), cudaMemcpyHostToDevice);

      auto res = Fp4Gemm(d_w, d_wsf, d_a, d_asf, d_out, M, N, K, inv_w_global,
                         inv_a_global, d_ws, kWorkspace, 0);
      if (res.status != CUBLAS_STATUS_SUCCESS || !res.has_algo) {
        std::printf("  [N=%d K=%d M=%d] no algo (status=%d)\n", N, K, M,
                    (int)res.status);
        cudaFree(d_a);
        cudaFree(d_asf);
        cudaFree(d_out);
        continue;
      }
      if (cudaDeviceSynchronize() != cudaSuccess) {
        cudaFree(d_a);
        cudaFree(d_asf);
        cudaFree(d_out);
        continue;
      }
      std::vector<float> got(static_cast<size_t>(M) * N);
      cudaMemcpy(got.data(), d_out, got.size() * sizeof(float),
                 cudaMemcpyDeviceToHost);
      cudaFree(d_a);
      cudaFree(d_asf);
      cudaFree(d_out);

      // CPU reference: dequant both, FP32 GEMM.
      std::vector<float> w_dq = HostDequant(wq, N, K, inv_w_global);
      std::vector<float> a_dq = HostDequant(aq, M, K, inv_a_global);
      double max_rel = 0.0;
      for (int m = 0; m < M; ++m) {
        for (int n = 0; n < N; ++n) {
          float acc = 0.0f;
          for (int k = 0; k < K; ++k)
            acc += a_dq[static_cast<size_t>(m) * K + k] *
                   w_dq[static_cast<size_t>(n) * K + k];
          double denom = std::fmax(1.0, std::fabs((double)acc));
          max_rel = std::fmax(max_rel,
                              std::fabs((double)got[static_cast<size_t>(m) * N + n] -
                                        (double)acc) /
                                  denom);
        }
      }
      const bool pass = max_rel < 0.02;
      if (pass) ++ok;
      std::printf("  [N=%d K=%d M=%3d] max_rel=%.5f %s\n", N, K, M, max_rel,
                  pass ? "PASS" : "FAIL");
      if (!pass) return false;
    }
    cudaFree(d_w);
    cudaFree(d_wsf);
  }
  cudaFree(d_ws);
  std::printf("  %d/%d W4A4 GEMM cases passed\n", ok, total);
  return ok == total;
}
