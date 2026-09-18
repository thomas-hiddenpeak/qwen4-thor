// Model head/tail implementation. See include/q4t/model/model_head.h.
//
//   EmbedLookup : token_ids [T] -> emb [T, hs]            (row gather)
//   ExpandTrunk : emb [T, hs] -> trunk [T, hc*hs]         (hc identical copies)
//   HeadForward : trunk [T, hc*hs] -> mixer.mix -> [T, hs] -> lm_head ->
//                 logits [T, vocab]
#include "q4t/model/model_head.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/model/linear.h"

namespace q4t {
namespace model {

namespace {

constexpr int kBlock = 256;
constexpr size_t kGemmScratch = 32 * 1024 * 1024;  // 32 MiB cuBLASLt scratch

inline size_t AlignUp(size_t n, size_t a) { return (n + a - 1) & ~(a - 1); }

// token_ids [T] -> emb [T, hs]: one block per token, copy hs BF16 values.
__global__ void EmbedLookupKernel(const uint16_t* __restrict__ embed,
                                  const int32_t* __restrict__ ids,
                                  uint16_t* __restrict__ out, int hs) {
  const int t = blockIdx.x;
  const int tok = ids[t];
  const uint16_t* src = embed + static_cast<size_t>(tok) * hs;
  uint16_t* dst = out + static_cast<size_t>(t) * hs;
  for (int i = threadIdx.x; i < hs; i += blockDim.x) dst[i] = src[i];
}

// emb [T, hs] -> trunk [T, hc*hs]: each of the hc branches is a copy of emb.
__global__ void ExpandTrunkKernel(const uint16_t* __restrict__ emb,
                                  uint16_t* __restrict__ trunk, int T, int hc,
                                  int hs) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * hs) return;
  const int t = idx / hs;
  const int c = idx % hs;
  const uint16_t v = emb[idx];
  uint16_t* dst = trunk + static_cast<size_t>(t) * hc * hs + c;
  for (int b = 0; b < hc; ++b) dst[b * hs] = v;
}

Status CheckGemm(const Bf16GemmResult& r) {
  if (r.status != CUBLAS_STATUS_SUCCESS || !r.has_algo) {
    return Status::Fail(std::string("Bf16Gemm failed (status=") +
                        std::to_string((int)r.status) + ")");
  }
  return Status();
}

}  // namespace

void ModelHeadWeights::Free() {
  if (embed_tokens) cudaFree(embed_tokens);
  if (lm_head) cudaFree(lm_head);
  lm_head_fp8.Free();
  mixer.Free();
  embed_tokens = lm_head = nullptr;
}

size_t ModelHeadWorkspaceBytes(int T, int hs) {
  const int hc_dim = 4 * hs;
  size_t off = 0;
  auto carve = [&](size_t bytes) {
    off = AlignUp(off, 256);
    off += bytes;
  };
  carve(static_cast<size_t>(T) * hs * 2);  // mixed [T, hs]
  carve(static_cast<size_t>(T) * hc_dim * 2);  // normed [T, hc*hs]
  carve(kGemmScratch);  // mixer GEMM
  carve(kGemmScratch);  // lm_head GEMM
  return off;
}

Status LoadModelHead(const io::WeightLoader& loader, int vocab, int hs, int hc,
                     int lowrank, float eps, ModelHeadWeights* out,
                     cudaStream_t stream) {
  if (vocab <= 0 || hs <= 0 || hc <= 0) {
    return Status::Fail("invalid head dims");
  }
  out->vocab = vocab;
  out->hs = hs;
  out->hc = hc;
  out->hc_dim = hc * hs;

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
  if (!(s = alloc(&out->embed_tokens,
                  static_cast<size_t>(vocab) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->lm_head,
                  static_cast<size_t>(vocab) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = load("model.language_model.embed_tokens.weight", out->embed_tokens,
                 static_cast<size_t>(vocab) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = load("lm_head.weight", out->lm_head,
                 static_cast<size_t>(vocab) * hs * sizeof(uint16_t))))
    return s;
  // FP8 (e4m3) decode shadow of lm_head (gated by Q4T_FP8_PROJ). Only used on
  // the M=1 decode GEMV; prefill logits stay BF16.
  if (!BuildFp8Shadow(out->lm_head, vocab, hs, &out->lm_head_fp8,
                      Fp8Part::kLmHead, stream))
    return Status::Fail("lm_head FP8 shadow");
  // Mixer: GatedResidual, use_combine=false (no block_inject).
  s = LoadHyperConnection(loader, "model.language_model.hyper_connection_mixer",
                          hc, hs, lowrank, eps, false, &out->mixer, stream);
  if (!s.ok()) return s;
  if (stream != nullptr &&
      cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("stream sync failed");
  }
  return Status();
}

Status EmbedLookup(const ModelHeadWeights& w, const int32_t* token_ids,
                   uint16_t* emb, int T, cudaStream_t stream) {
  if (T <= 0) return Status();
  EmbedLookupKernel<<<T, kBlock, 0, stream>>>(w.embed_tokens, token_ids, emb,
                                              w.hs);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("embed launch");
  return Status();
}

// 2-stage argmax over [B, vocab]. Stage 1 (grid = (numChunks, B)) splits each
// row into numChunks contiguous slices; each 256-thread block scans its slice
// tracking the local (max, lowest-index) and writes one partial. Stage 2
// (grid = B) reduces the numChunks partials to the global (max, lowest-index).
// The old single-block-per-row kernel used ONE block (one SM) to scan all
// 248320 vocab entries -- latency-bound at ~523us for B=1. The 2-stage version
// spreads the read across ~970 blocks (all 20 SMs) and is bandwidth-bound
// (~10-20us). Lowest-index tie-break is preserved: chunk c covers
// [c*per, (c+1)*per), so the lowest index among equal-valued entries is always
// in the earliest chunk, and both stages break ties toward the lower index.
constexpr int kArgmaxThreads = 256;

__global__ void ArgmaxBf16ChunkKernel(const uint16_t* __restrict__ logits,
                                      int vocab, int numChunks,
                                      float* __restrict__ part_v,
                                      int* __restrict__ part_i) {
  const int b = blockIdx.y;
  const int chunk = blockIdx.x;
  const uint16_t* row = logits + static_cast<size_t>(b) * vocab;
  const int per = (vocab + numChunks - 1) / numChunks;
  const int start = chunk * per;
  const int end = start + per < vocab ? start + per : vocab;
  float best_v = -1e30f;
  int best_i = start;
  for (int v = start + threadIdx.x; v < end; v += blockDim.x) {
    const float f = __uint_as_float(static_cast<uint32_t>(row[v]) << 16);
    if (f > best_v) {  // strict > keeps the lowest index within this thread
      best_v = f;
      best_i = v;
    }
  }
  __shared__ float s_v[kArgmaxThreads];
  __shared__ int s_i[kArgmaxThreads];
  s_v[threadIdx.x] = best_v;
  s_i[threadIdx.x] = best_i;
  __syncthreads();
  for (int stride = blockDim.x >> 1; stride > 0; stride >>= 1) {
    if (threadIdx.x < stride) {
      const float ov = s_v[threadIdx.x + stride];
      const int oi = s_i[threadIdx.x + stride];
      if (ov > s_v[threadIdx.x] ||
          (ov == s_v[threadIdx.x] && oi < s_i[threadIdx.x])) {
        s_v[threadIdx.x] = ov;
        s_i[threadIdx.x] = oi;
      }
    }
    __syncthreads();
  }
  if (threadIdx.x == 0) {
    const size_t o = static_cast<size_t>(b) * numChunks + chunk;
    part_v[o] = s_v[0];
    part_i[o] = s_i[0];
  }
}

__global__ void ArgmaxBf16ReduceKernel(const float* __restrict__ part_v,
                                       const int* __restrict__ part_i,
                                       int numChunks, int32_t* __restrict__ out) {
  const int b = blockIdx.x;
  const float* pv = part_v + static_cast<size_t>(b) * numChunks;
  const int* pi = part_i + static_cast<size_t>(b) * numChunks;
  float best_v = -1e30f;
  int best_i = 0x7fffffff;
  for (int c = threadIdx.x; c < numChunks; c += blockDim.x) {
    if (pv[c] > best_v || (pv[c] == best_v && pi[c] < best_i)) {
      best_v = pv[c];
      best_i = pi[c];
    }
  }
  __shared__ float s_v[kArgmaxThreads];
  __shared__ int s_i[kArgmaxThreads];
  s_v[threadIdx.x] = best_v;
  s_i[threadIdx.x] = best_i;
  __syncthreads();
  for (int stride = blockDim.x >> 1; stride > 0; stride >>= 1) {
    if (threadIdx.x < stride) {
      const float ov = s_v[threadIdx.x + stride];
      const int oi = s_i[threadIdx.x + stride];
      if (ov > s_v[threadIdx.x] ||
          (ov == s_v[threadIdx.x] && oi < s_i[threadIdx.x])) {
        s_v[threadIdx.x] = ov;
        s_i[threadIdx.x] = oi;
      }
    }
    __syncthreads();
  }
  if (threadIdx.x == 0) out[b] = s_i[0];
}

Status ArgmaxBf16Rows(const uint16_t* logits, int B, int vocab,
                      int32_t* out_tokens, cudaStream_t stream) {
  if (B <= 0 || vocab <= 0) return Status();
  const int numChunks = (vocab + kArgmaxThreads - 1) / kArgmaxThreads;
  // Persistent partial buffer [B, numChunks] (float value + int index), grown
  // on demand. The argmax calls are serialized on the model stream (under
  // model_mu_ in the scheduler and the MTP path), so no extra lock is needed.
  static float* s_part_v = nullptr;
  static int* s_part_i = nullptr;
  static size_t s_part_cap = 0;  // capacity in (B, numChunks) pairs
  const size_t need = static_cast<size_t>(B) * numChunks;
  if (need > s_part_cap) {
    if (s_part_v) {
      cudaFree(s_part_v);
      cudaFree(s_part_i);
      s_part_v = nullptr;
      s_part_i = nullptr;
      s_part_cap = 0;
    }
    const size_t cap = need > static_cast<size_t>(256) * numChunks
                           ? need
                           : static_cast<size_t>(256) * numChunks;
    if (cudaMalloc(reinterpret_cast<void**>(&s_part_v), cap * sizeof(float)) !=
            cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&s_part_i), cap * sizeof(int)) !=
            cudaSuccess) {
      return Status::Fail("argmax partial buffer alloc");
    }
    s_part_cap = cap;
  }
  ArgmaxBf16ChunkKernel<<<dim3(numChunks, B), kArgmaxThreads, 0, stream>>>(
      logits, vocab, numChunks, s_part_v, s_part_i);
  if (cudaGetLastError() != cudaSuccess)
    return Status::Fail("argmax chunk launch");
  ArgmaxBf16ReduceKernel<<<B, kArgmaxThreads, 0, stream>>>(
      s_part_v, s_part_i, numChunks, out_tokens);
  if (cudaGetLastError() != cudaSuccess)
    return Status::Fail("argmax reduce launch");
  return Status();
}

Status ExpandTrunk(const ModelHeadWeights& w, const uint16_t* emb,
                   uint16_t* trunk, int T, cudaStream_t stream) {
  if (T <= 0) return Status();
  const int total = T * w.hs;
  ExpandTrunkKernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
      emb, trunk, T, w.hc, w.hs);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("expand launch");
  return Status();
}

Status HeadForward(const ModelHeadWeights& w, const uint16_t* trunk,
                   uint16_t* logits, int T, void* workspace,
                   size_t workspace_bytes, cudaStream_t stream) {
  const int hs = w.hs, hc_dim = w.hc_dim, vocab = w.vocab;
  if (T <= 0) return Status();

  char* base = static_cast<char*>(workspace);
  size_t off = 0;
  auto carve = [&](size_t bytes) -> char* {
    off = AlignUp(off, 256);
    char* p = base + off;
    off += bytes;
    return p;
  };
  char* d_mixed = carve(static_cast<size_t>(T) * hs * 2);
  char* d_normed = carve(static_cast<size_t>(T) * hc_dim * 2);
  char* d_gemm1 = carve(kGemmScratch);
  char* d_gemm2 = carve(kGemmScratch);
  if (off > workspace_bytes) {
    return Status::Fail("HeadForward: workspace too small");
  }

  // 1. mixer.mix(trunk) -> mixed [T, hs] (normed is scratch).
  Status s =
      HyperConnectionMix(w.mixer, trunk, reinterpret_cast<uint16_t*>(d_mixed),
                         reinterpret_cast<uint16_t*>(d_normed), T, d_gemm1,
                         kGemmScratch, stream);
  if (!s.ok()) return s;
  // 2. logits = mixed @ lm_head^T  [T, hs] x [vocab, hs]^T -> [T, vocab].
  s = CheckGemm(ProjGemm(reinterpret_cast<uint16_t*>(d_mixed), w.lm_head,
                         &w.lm_head_fp8, logits, T, vocab, hs, 1.0f, 0.0f,
                         d_gemm2, kGemmScratch, stream));
  return s;
}

}  // namespace model
}  // namespace q4t
