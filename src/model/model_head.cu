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
  s = CheckGemm(Bf16Gemm(reinterpret_cast<uint16_t*>(d_mixed), w.lm_head,
                         logits, T, vocab, hs, 1.0f, 0.0f, d_gemm2,
                         kGemmScratch, stream));
  return s;
}

}  // namespace model
}  // namespace q4t
