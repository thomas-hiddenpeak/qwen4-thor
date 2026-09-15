// Step 1b: drive the AOT-exported FA4 hd256 forward kernel from pure C++
// (no Python at runtime). Mirrors the Step 1a vec_add driver pattern.
//
// Verifies the full chain:
//   FA4 FlashAttentionForwardSm100 (hd256, GQA 24:2, causal)
//     -> cute.compile -> export_to_c -> fa4_fwd_hd256.{h,o}
//     -> this C++ driver (real cudaStream_t) -> correct O vs fp32 reference.
//
// Config (matches our model): head_dim 256, 24 q-heads, 2 kv-heads
// (GQA 12:1), causal, bf16, rectangular batch=1, s_q=s_k=128.
#include "fa4_fwd_hd256.h"
#include <cuda_runtime.h>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

// Self-contained bf16 <-> fp32 (avoids cuda_bf16.h version coupling).
static uint16_t f2bf(float f) {
  uint32_t u;
  std::memcpy(&u, &f, 4);
  uint32_t lsb = (u >> 16) & 1u;
  u += 0x7fffu + lsb;  // round-to-nearest-even
  return (uint16_t)(u >> 16);
}
static float bf2f(uint16_t b) {
  uint32_t u = (uint32_t)b << 16;
  float f;
  std::memcpy(&f, &u, 4);
  return f;
}

// Layout (b, s, h, d) contiguous.
constexpr int kBatch = 1;
constexpr int kSq = 128;
constexpr int kSk = 128;
constexpr int kHq = 24;
constexpr int kHkv = 2;
constexpr int kHd = 256;
constexpr int kGqa = kHq / kHkv;  // 12
constexpr float kScale = 1.0f / std::sqrt((float)kHd);

// Deterministic pseudo-random in [-1, 1) from an integer seed (no RNG state).
static float det_rand(uint32_t x) {
  x ^= x << 13;
  x ^= x >> 17;
  x ^= x << 5;
  return (float)(x & 0xFFFFFF) / (float)0xFFFFFF - 1.0f;
}

static inline int q_off(int b, int s, int h, int d) {
  return ((b * kSq + s) * kHq + h) * kHd + d;
}
static inline int kv_off(int b, int s, int h, int d) {
  return ((b * kSk + s) * kHkv + h) * kHd + d;
}

int main() {
  const size_t nq = (size_t)kBatch * kSq * kHq * kHd;
  const size_t nkv = (size_t)kBatch * kSk * kHkv * kHd;
  const size_t nlse = (size_t)kBatch * kHq * kSq;

  // Host inputs (bf16).
  std::vector<uint16_t> hq(nq), hk(nkv), hv(nkv);
  std::vector<float> hqf(nq), hkf(nkv), hvf(nkv);
  for (size_t i = 0; i < nq; ++i) {
    hqf[i] = det_rand((uint32_t)(i * 2654435761u + 1));
    hq[i] = f2bf(hqf[i]);
  }
  for (size_t i = 0; i < nkv; ++i) {
    hkf[i] = det_rand((uint32_t)(i * 2246822519u + 1000003));
    hvf[i] = det_rand((uint32_t)(i * 3266489917u + 2000003));
    hk[i] = f2bf(hkf[i]);
    hv[i] = f2bf(hvf[i]);
  }

  // fp32 causal reference (bottom-right aligned; s_q==s_k so j<=i).
  std::vector<float> ref(nq, 0.0f);
  for (int b = 0; b < kBatch; ++b) {
    for (int hq_i = 0; hq_i < kHq; ++hq_i) {
      const int kvh = hq_i / kGqa;
      for (int i = 0; i < kSq; ++i) {
        // scores over j in [0, i]
        std::vector<float> scores(i + 1, 0.0f);
        float mx = -1e30f;
        for (int j = 0; j <= i; ++j) {
          float acc = 0.0f;
          for (int d = 0; d < kHd; ++d)
            acc += hqf[q_off(b, i, hq_i, d)] * hkf[kv_off(b, j, kvh, d)];
          acc *= kScale;
          scores[j] = acc;
          if (acc > mx) mx = acc;
        }
        float sum = 0.0f;
        for (int j = 0; j <= i; ++j) {
          scores[j] = std::exp(scores[j] - mx);
          sum += scores[j];
        }
        for (int d = 0; d < kHd; ++d) {
          float acc = 0.0f;
          for (int j = 0; j <= i; ++j)
            acc += scores[j] * hvf[kv_off(b, j, kvh, d)];
          ref[q_off(b, i, hq_i, d)] = acc / sum;
        }
      }
    }
  }

  // Device buffers.
  uint16_t *dQ = nullptr, *dK = nullptr, *dV = nullptr, *dO = nullptr;
  float *dLSE = nullptr;
  cudaMalloc(&dQ, nq * 2);
  cudaMalloc(&dK, nkv * 2);
  cudaMalloc(&dV, nkv * 2);
  cudaMalloc(&dO, nq * 2);
  cudaMalloc(&dLSE, nlse * 4);
  cudaMemcpy(dQ, hq.data(), nq * 2, cudaMemcpyHostToDevice);
  cudaMemcpy(dK, hk.data(), nkv * 2, cudaMemcpyHostToDevice);
  cudaMemcpy(dV, hv.data(), nkv * 2, cudaMemcpyHostToDevice);
  cudaMemset(dO, 0, nq * 2);
  cudaMemset(dLSE, 0, nlse * 4);

  // Tensor descriptors (dynamic dims/strides per the exported header).
  fa4_fwd_hd256_Tensor_mQ_t mQ{dQ, {kBatch, kSq, kHq, kHd},
                               {(int64_t)kSq * kHq * kHd, (int64_t)kHq * kHd,
                                kHd}};
  fa4_fwd_hd256_Tensor_mK_t mK{dK, {kBatch, kSk, kHkv, kHd},
                               {(int64_t)kSk * kHkv * kHd,
                                (int64_t)kHkv * kHd, kHd}};
  fa4_fwd_hd256_Tensor_mV_t mV{dV, {kBatch, kSk, kHkv, kHd},
                               {(int64_t)kSk * kHkv * kHd,
                                (int64_t)kHkv * kHd, kHd}};
  fa4_fwd_hd256_Tensor_mO_t mO{dO, {kBatch, kSq, kHq, kHd},
                               {(int64_t)kSq * kHq * kHd, (int64_t)kHq * kHd,
                                kHd}};
  fa4_fwd_hd256_Tensor_mLSE_t mLSE{dLSE, {kBatch, kHq, kSq},
                                   {(int64_t)kHq * kSq, kSq}};

  fa4_fwd_hd256_Kernel_Module_t module;
  fa4_fwd_hd256_Kernel_Module_Load(&module);
  int32_t ret = cute_dsl_fa4_fwd_hd256_wrapper(&module, &mQ, &mK, &mV, &mO,
                                               &mLSE, kScale, kSq,
                                               /*stream=*/0);
  if (ret != 0) {
    printf("wrapper ret=%d\n", ret);
    return 1;
  }
  cudaDeviceSynchronize();

  std::vector<uint16_t> ho(nq);
  cudaMemcpy(ho.data(), dO, nq * 2, cudaMemcpyDeviceToHost);

  // Compare (bf16 output vs fp32 ref).
  double l2num = 0.0, l2den = 0.0;
  int argmax_mismatch = 0;
  for (int b = 0; b < kBatch; ++b) {
    for (int i = 0; i < kSq; ++i) {
      for (int h = 0; h < kHq; ++h) {
        int obest = -1, rbest = -1;
        float ob = -1e30f, rb = -1e30f;
        for (int d = 0; d < kHd; ++d) {
          float ov = bf2f(ho[q_off(b, i, h, d)]);
          float rv = ref[q_off(b, i, h, d)];
          l2num += (double)(ov - rv) * (ov - rv);
          l2den += (double)rv * rv;
          if (ov > ob) { ob = ov; obest = d; }
          if (rv > rb) { rb = rv; rbest = d; }
        }
        if (obest != rbest) ++argmax_mismatch;
      }
    }
  }
  double l2_rel = std::sqrt(l2num / (l2den > 0 ? l2den : 1.0));
  int total_rows = kBatch * kSq * kHq;
  printf("FA4 hd256 AOT (no Python): l2_rel=%.6f argmax_mismatch=%d/%d\n",
         l2_rel, argmax_mismatch, total_rows);
  bool ok = l2_rel < 0.05;
  printf("  -> %s\n", ok ? "PASS" : "FAIL");

  fa4_fwd_hd256_Kernel_Module_Unload(&module);
  cudaFree(dQ); cudaFree(dK); cudaFree(dV); cudaFree(dO); cudaFree(dLSE);
  return ok ? 0 : 2;
}
