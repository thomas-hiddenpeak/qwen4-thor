// MTP (Multi-Token Predictor) draft model — implementation. See mtp.h.
//
// One draft step (scheme A), mirroring
// reference/vllm/vllm/models/qwen4_exp/nvidia/mtp.py:
//   1. emb        = embed_tokens(input_ids)                  [T, H]
//   2. prev_block = fc_embedding(pre_fc_norm_embedding(emb))  [T, H]
//   3. hs_multi   = pre_fc_norm_hidden(hidden_states)         [T, hc*H] (grouped)
//      hs_multi   = flatten(fc_hidden(hs_multi.view(T, hc, H))) [T, hc*H]
//   4. MTP decoder layer (delayed combine, returns a triple):
//        a. trunk = unit_combine(hs_multi, prev_block)        [T, hc*H]
//           (the layer's first combine has prev_injection=None -> unit weight;
//            vLLM ops/hc.py _hc_combine_kernel)
//        b. mixed_attn, normed_attn = attn_hc.mix(trunk)      [T, H]
//        c. attn_out = FullAttentionForward(mixed_attn)       [T, H]
//        d. trunk_a  = attn_hc.combine(attn_out, trunk, normed_attn) [T, hc*H]
//        e. mixed_mlp, normed_mlp = mlp_hc.mix(trunk_a)       [T, H]
//        f. mlp_out  = MoeBf16Forward(mixed_mlp)              [T, H]
//        -> returns (trunk_a, mlp_out, mlp_injection)
//   5. Final mixer (GatedResidual, use_combine=false):
//        multi_hidden  = combine(mlp_out, trunk_a, mlp_injection) [T, hc*H]
//                        (learned injection = the mlp_hc's own block_inject)
//        sample_hidden = mixer.mix(multi_hidden)              [T, H]
//   6. logits = lm_head(sample_hidden)                        [T, vocab]
//
// The only new kernel vs the main model is the unit-weight combine (step 4a);
// everything else reuses model:: primitives.
#include "q4t/mtp/mtp.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "q4t/model/full_attention.h"
#include "q4t/model/hyperconnection.h"
#include "q4t/model/linear.h"
#include "q4t/model/model_head.h"
#include "q4t/model/moe.h"

namespace q4t {
namespace mtp {

namespace {

constexpr int kBlock = 256;
constexpr size_t kGemmWs = 32u * 1024u * 1024u;  // cuBLASLt scratch per GEMM

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

// Plain GemmaRMSNorm (one group per row): out = x * rsqrt(mean(x^2)+eps) *
// (1 + weight). Used by pre_fc_norm_embedding over [T, hs].
__global__ void GemmaRmsNormKernel(const uint16_t* __restrict__ x,
                                   const uint16_t* __restrict__ weight,
                                   uint16_t* __restrict__ out, int T, int dim,
                                   float eps) {
  const int t = blockIdx.x;
  if (t >= T) return;
  const uint16_t* row = x + static_cast<size_t>(t) * dim;
  uint16_t* orow = out + static_cast<size_t>(t) * dim;
  __shared__ float s_part[kBlock];
  float acc = 0.0f;
  for (int i = threadIdx.x; i < dim; i += blockDim.x) {
    const float v = Bf16ToFloat(row[i]);
    acc += v * v;
  }
  s_part[threadIdx.x] = acc;
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
    orow[i] = FloatToBf16(v * rs * (1.0f + w));
  }
}

// Grouped GemmaRMSNorm (hc groups of hs per row): each group RMSNorm'd
// independently, then scaled by (1 + weight). Used by pre_fc_norm_hidden over
// [T, hc*hs]. Same math as the main model's GroupedRmsNormKernel.
__global__ void GroupedRmsNormKernel(const uint16_t* __restrict__ x,
                                     const uint16_t* __restrict__ weight,
                                     uint16_t* __restrict__ out, int T, int hc,
                                     int hs, float eps) {
  const int t = blockIdx.x;
  if (t >= T) return;
  const uint16_t* row = x + static_cast<size_t>(t) * hc * hs;
  uint16_t* orow = out + static_cast<size_t>(t) * hc * hs;
  __shared__ float s_part[kBlock];
  for (int b = 0; b < hc; ++b) {
    const uint16_t* g = row + b * hs;
    float acc = 0.0f;
    for (int i = threadIdx.x; i < hs; i += blockDim.x) {
      const float v = Bf16ToFloat(g[i]);
      acc += v * v;
    }
    s_part[threadIdx.x] = acc;
    __syncthreads();
    for (int off = blockDim.x / 2; off > 0; off >>= 1) {
      if (threadIdx.x < off) s_part[threadIdx.x] += s_part[threadIdx.x + off];
      __syncthreads();
    }
    const float rs = rsqrtf(s_part[0] / hs + eps);
    __syncthreads();
    for (int i = threadIdx.x; i < hs; i += blockDim.x) {
      const float v = Bf16ToFloat(g[i]);
      const float w = Bf16ToFloat(weight[b * hs + i]);
      orow[b * hs + i] = FloatToBf16(v * rs * (1.0f + w));
    }
    __syncthreads();
  }
}

// Unit-weight HC combine: out = residual + block (block broadcast to every
// branch, no gate). The MTP decoder layer's first combine has
// prev_injection=None -> unit weight (vLLM ops/hc.py _hc_combine_kernel).
// `residual` and `out` may alias (in-place add); each thread only touches its
// own element, so no __restrict__ on those two.
__global__ void UnitCombineKernel(const uint16_t* residual,
                                  const uint16_t* __restrict__ block,
                                  uint16_t* out, int T, int hc, int hs) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * hc * hs) return;
  const int t = idx / (hc * hs);
  const int c = idx % hs;
  const float r = Bf16ToFloat(residual[idx]);
  const float bo = Bf16ToFloat(block[static_cast<size_t>(t) * hs + c]);
  out[idx] = FloatToBf16(r + bo);
}

Status CheckGemm(const model::Bf16GemmResult& r) {
  if (r.status != CUBLAS_STATUS_SUCCESS || !r.has_algo) {
    return Status::Fail(std::string("Bf16Gemm failed (status=") +
                        std::to_string((int)r.status) + ")");
  }
  return Status();
}

}  // namespace

void MtpModel::Free() {
  if (fc_embedding) cudaFree(fc_embedding);
  if (fc_hidden) cudaFree(fc_hidden);
  if (pre_fc_norm_embedding) cudaFree(pre_fc_norm_embedding);
  if (pre_fc_norm_hidden) cudaFree(pre_fc_norm_hidden);
  attn_hc.Free();
  mlp_hc.Free();
  full_attn.Free();
  moe.Free();
  moe_extra.Free();
  mixer.Free();
  if (kv_cache) cudaFree(kv_cache);
  if (page_table) cudaFree(page_table);
  if (idx_raw) cudaFree(idx_raw);
  if (idx_comp) cudaFree(idx_comp);
  if (d_rope_pos) cudaFree(d_rope_pos);
  if (d_ws) cudaFree(d_ws);
  if (d_sample) cudaFree(d_sample);
  if (d_trunk) cudaFree(d_trunk);
  if (d_logits) cudaFree(d_logits);
  if (d_ids_scratch) cudaFree(d_ids_scratch);
  if (d_pos_scratch) cudaFree(d_pos_scratch);
  if (d_spec_logits) cudaFree(d_spec_logits);
  if (d_spec_trunk) cudaFree(d_spec_trunk);
  if (d_spec_sample) cudaFree(d_spec_sample);
  if (d_g) cudaFree(d_g);
  fc_embedding = nullptr;
  fc_hidden = nullptr;
  pre_fc_norm_embedding = nullptr;
  pre_fc_norm_hidden = nullptr;
  kv_cache = nullptr;
  page_table = nullptr;
  idx_raw = nullptr;
  idx_comp = nullptr;
  d_rope_pos = nullptr;
  d_ws = nullptr;
  d_sample = nullptr;
  d_trunk = nullptr;
  d_logits = nullptr;
  d_ids_scratch = nullptr;
  d_pos_scratch = nullptr;
  d_spec_logits = nullptr;
  d_spec_trunk = nullptr;
  d_spec_sample = nullptr;
  d_g = nullptr;
  k_max = 0;
}

Status LoadMtp(const MtpConfig& cfg, const uint16_t* main_embed,
               const uint16_t* main_lm_head, MtpModel* out,
               cudaStream_t stream) {
  out->cfg = cfg;
  out->embed_tokens = main_embed;
  out->lm_head = main_lm_head;
  const int hs = cfg.hs, hc = cfg.hc, hc_dim = hc * hs;

  // RAII: the mmap'd MTP shards must be released on every exit path (same
  // unified-memory constraint as the main model).
  std::unique_ptr<io::WeightIndex> index;
  {
    io::WeightIndex* raw = nullptr;
    Status s = io::WeightIndex::Open(
        cfg.mtp_dir + "/model.safetensors.index.json", &raw);
    if (!s.ok()) return s;
    index.reset(raw);
  }
  std::unique_ptr<io::WeightLoader> loader;
  {
    io::WeightLoader* raw = nullptr;
    Status s = io::WeightLoader::Create(cfg.mtp_dir, *index, 8, &raw);
    if (!s.ok()) return s;
    loader.reset(raw);
  }

  auto alloc = [](uint16_t** p, size_t bytes) -> Status {
    if (cudaMalloc(reinterpret_cast<void**>(p), bytes) != cudaSuccess)
      return Status::Fail("cudaMalloc failed");
    return Status();
  };
  auto load = [&loader, stream](const std::string& name, uint16_t* dst,
                                size_t bytes) -> Status {
    std::vector<uint16_t> host(bytes / sizeof(uint16_t));
    Status st = loader->ReadTensor(name, host.data());
    if (!st.ok()) return st;
    if (cudaMemcpyAsync(dst, host.data(), bytes, cudaMemcpyHostToDevice,
                        stream) != cudaSuccess)
      return Status::Fail("H2D failed");
    return Status();
  };

  Status s;
  // 1. fc projections + pre-norms (mtp.* prefix).
  if (!(s = alloc(&out->fc_embedding,
                  static_cast<size_t>(hs) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->fc_hidden,
                  static_cast<size_t>(hs) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->pre_fc_norm_embedding,
                  static_cast<size_t>(hs) * sizeof(uint16_t))))
    return s;
  if (!(s = alloc(&out->pre_fc_norm_hidden,
                  static_cast<size_t>(hc_dim) * sizeof(uint16_t))))
    return s;
  if (!(s = load("mtp.fc_embedding.weight", out->fc_embedding,
                 static_cast<size_t>(hs) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = load("mtp.fc_hidden.weight", out->fc_hidden,
                 static_cast<size_t>(hs) * hs * sizeof(uint16_t))))
    return s;
  if (!(s = load("mtp.pre_fc_norm_embedding.weight", out->pre_fc_norm_embedding,
                 static_cast<size_t>(hs) * sizeof(uint16_t))))
    return s;
  if (!(s = load("mtp.pre_fc_norm_hidden.weight", out->pre_fc_norm_hidden,
                 static_cast<size_t>(hc_dim) * sizeof(uint16_t))))
    return s;

  // 2. The MTP full-attention decoder layer (mtp.layers.0.*).
  const std::string lp = "mtp.layers.0";
  if (!(s = model::LoadHyperConnection(
            *loader, lp + ".attn_hyper_connection", hc, hs, cfg.lowrank,
            cfg.eps, true, &out->attn_hc, stream)))
    return s;
  if (!(s = model::LoadHyperConnection(
            *loader, lp + ".mlp_hyper_connection", hc, hs, cfg.lowrank,
            cfg.eps, true, &out->mlp_hc, stream)))
    return s;
  if (!(s = model::LoadFullAttention(
            *loader, lp + ".self_attn", hs, cfg.nq, cfg.nkv, cfg.hd, cfg.rot_d,
            cfg.rope_theta, cfg.eps, cfg.idx_n_heads, cfg.idx_kv_heads,
            cfg.idx_head_dim, cfg.idx_budget, cfg.idx_compress, &out->full_attn,
            stream)))
    return s;
  out->full_attn.max_len = cfg.max_len;  // sizes the persistent 3D MRoPE table.
  if (!(s = LoadMoeBf16(*loader, lp + ".mlp", cfg.E, hs, cfg.moe_is, &out->moe,
                        stream)))
    return s;
  if (!(s = model::LoadMoEExtra(*loader, lp + ".mlp", cfg.E, hs, cfg.shared_is,
                                &out->moe_extra, stream)))
    return s;

  // 3. Final mixer (use_combine=false; no block_inject in the checkpoint).
  if (!(s = model::LoadHyperConnection(
            *loader, "mtp.hyper_connection_mixer", hc, hs, cfg.lowrank,
            cfg.eps, false, &out->mixer, stream)))
    return s;

  // 4. Persistent full-attention KV / indexer buffers + identity page table.
  const int n_pages = (cfg.max_len + model::kKvPageSize - 1) /
                      model::kKvPageSize;
  out->kv_bytes =
      static_cast<size_t>(n_pages) * model::kKvPageSize * cfg.nkv * 2 *
      cfg.hd * sizeof(uint16_t);
  if (cudaMalloc(reinterpret_cast<void**>(&out->kv_cache), out->kv_bytes) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc kv_cache");
  if (cudaMalloc(reinterpret_cast<void**>(&out->page_table),
                 static_cast<size_t>(cfg.max_len) * sizeof(int)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc page_table");
  if (cudaMalloc(reinterpret_cast<void**>(&out->idx_raw),
                 static_cast<size_t>(cfg.max_len) * cfg.idx_head_dim *
                     sizeof(uint16_t)) != cudaSuccess)
    return Status::Fail("cudaMalloc idx_raw");
  if (cudaMalloc(reinterpret_cast<void**>(&out->idx_comp),
                 static_cast<size_t>(cfg.max_len) * cfg.idx_head_dim *
                     sizeof(uint16_t)) != cudaSuccess)
    return Status::Fail("cudaMalloc idx_comp");
  // 3D MRoPE identity table [3, max_len]: all three rows = position (pure
  // text, delta=0). The RoPE kernels index by positions[t] (absolute pos).
  if (cudaMalloc(reinterpret_cast<void**>(&out->d_rope_pos),
                 3u * static_cast<size_t>(cfg.max_len) * sizeof(int)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc d_rope_pos");
  {
    std::vector<int> rp(3 * static_cast<size_t>(cfg.max_len), 0);
    for (int p = 0; p < cfg.max_len; ++p)
      for (int r = 0; r < 3; ++r)
        rp[r * cfg.max_len + p] = p;
    if (cudaMemcpy(out->d_rope_pos, rp.data(),
                   rp.size() * sizeof(int), cudaMemcpyHostToDevice) !=
        cudaSuccess)
      return Status::Fail("H2D d_rope_pos");
  }
  {
    std::vector<int> pt(cfg.max_len);
    for (int p = 0; p < cfg.max_len; ++p)
      pt[p] = p / model::kKvPageSize;  // identity mapping
    if (cudaMemcpy(out->page_table, pt.data(),
                   static_cast<size_t>(cfg.max_len) * sizeof(int),
                   cudaMemcpyHostToDevice) != cudaSuccess)
      return Status::Fail("H2D page_table");
  }

  // 5. Forward workspace (sized for max_prefill tokens).
  out->ws_bytes = MtpWorkspaceBytes(cfg, cfg.max_prefill, out->full_attn);
  if (cudaMalloc(&out->d_ws, out->ws_bytes) != cudaSuccess)
    return Status::Fail("cudaMalloc d_ws");

  // 6. Speculative-step scratch (one draft step of a single token): the
  //    sample_hidden [hs], the multi_hidden trunk [hc*hs], and the logits
  //    [vocab].
  if (cudaMalloc(reinterpret_cast<void**>(&out->d_sample),
                 static_cast<size_t>(hs) * sizeof(uint16_t)) != cudaSuccess)
    return Status::Fail("cudaMalloc d_sample");
  if (cudaMalloc(reinterpret_cast<void**>(&out->d_trunk),
                 static_cast<size_t>(hc_dim) * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc d_trunk");
  if (cudaMalloc(reinterpret_cast<void**>(&out->d_logits),
                 static_cast<size_t>(cfg.vocab) * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc d_logits");

  if (stream != nullptr && cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("stream sync failed");
  }
  return Status();
}

Status MtpResetState(const MtpModel& m, cudaStream_t stream) {
  if (m.kv_cache) {
    if (cudaMemsetAsync(m.kv_cache, 0, m.kv_bytes, stream) != cudaSuccess)
      return Status::Fail("memset kv_cache");
  }
  const size_t idx_bytes = static_cast<size_t>(m.cfg.max_len) *
                           m.cfg.idx_head_dim * sizeof(uint16_t);
  if (m.idx_raw &&
      cudaMemsetAsync(m.idx_raw, 0, idx_bytes, stream) != cudaSuccess)
    return Status::Fail("memset idx_raw");
  if (m.idx_comp &&
      cudaMemsetAsync(m.idx_comp, 0, idx_bytes, stream) != cudaSuccess)
    return Status::Fail("memset idx_comp");
  return Status();
}

Status MtpReserveScratch(MtpModel& m, int k_max) {
  if (k_max <= 0) return Status::Fail("MtpReserveScratch: k_max must be > 0");
  if (k_max <= m.k_max) return Status();  // already sized for this k
  const int hs = m.cfg.hs, hc_dim = m.hc_dim(), vocab = m.cfg.vocab;
  if (m.d_ids_scratch) cudaFree(m.d_ids_scratch);
  if (m.d_pos_scratch) cudaFree(m.d_pos_scratch);
  if (m.d_spec_logits) cudaFree(m.d_spec_logits);
  if (m.d_spec_trunk) cudaFree(m.d_spec_trunk);
  if (m.d_spec_multi) cudaFree(m.d_spec_multi);
  if (m.d_spec_sample) cudaFree(m.d_spec_sample);
  if (m.d_g) cudaFree(m.d_g);
  m.d_ids_scratch = nullptr;
  m.d_pos_scratch = nullptr;
  m.d_spec_logits = nullptr;
  m.d_spec_trunk = nullptr;
  m.d_spec_multi = nullptr;
  m.d_spec_sample = nullptr;
  m.d_g = nullptr;
  // k_max rows: verify uses k+1 tokens, extend uses a+1 <= k+1 tokens.
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_ids_scratch),
                 static_cast<size_t>(k_max) * sizeof(int32_t)) != cudaSuccess)
    return Status::Fail("MtpReserveScratch: d_ids_scratch");
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_pos_scratch),
                 static_cast<size_t>(k_max) * sizeof(int)) != cudaSuccess)
    return Status::Fail("MtpReserveScratch: d_pos_scratch");
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_spec_logits),
                 static_cast<size_t>(k_max) * vocab * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("MtpReserveScratch: d_spec_logits");
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_spec_trunk),
                 static_cast<size_t>(k_max) * hc_dim * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("MtpReserveScratch: d_spec_trunk");
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_spec_multi),
                 static_cast<size_t>(k_max) * hc_dim * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("MtpReserveScratch: d_spec_multi");
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_spec_sample),
                 static_cast<size_t>(k_max) * hs * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("MtpReserveScratch: d_spec_sample");
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_g),
                 static_cast<size_t>(hc_dim) * sizeof(uint16_t)) != cudaSuccess)
    return Status::Fail("MtpReserveScratch: d_g");
  m.k_max = k_max;
  return Status();
}

size_t MoeBf16ScratchBytes(int T, int topk, int hs, int shared_is, int E) {
  // The non-routed scratch MoeBf16Forward carves after the routed region
  // (router logits + expert_ids + router_w + routed f32 + shared gu/swiglu/
  // down). Mirrors the carve in MoeBf16Forward.
  return ((static_cast<size_t>(T) * E * sizeof(uint16_t) +
           static_cast<size_t>(T) * topk * (sizeof(int32_t) + sizeof(float)) +
           static_cast<size_t>(T) * hs * sizeof(float) +
           static_cast<size_t>(T) * (2 * shared_is) * sizeof(uint16_t) +
           static_cast<size_t>(T) * shared_is * sizeof(uint16_t) +
           static_cast<size_t>(T) * hs * sizeof(uint16_t) + 7) &
          ~size_t(7));
}

size_t MtpWorkspaceBytes(const MtpConfig& cfg, int T,
                         const model::FullAttentionWeights& full) {
  // Must match MtpForward's carve order: full-attention scratch, the BF16 MoE
  // workspace, two 32 MiB GEMM regions, then the activation scratch (7 hs
  // buffers + 5 hc_dim buffers, see the carve in MtpForward).
  const int hc_dim = cfg.hc * cfg.hs;
  size_t b = 0;
  b += model::FullAttentionWorkspaceBytes(full, T);
  b += MoeBf16WorkspaceBytes(T, cfg.topk, cfg.hs, cfg.moe_is, cfg.E) +
       MoeBf16ScratchBytes(T, cfg.topk, cfg.hs, cfg.shared_is, cfg.E);
  b += 2 * kGemmWs;
  b += 7 * static_cast<size_t>(T) * cfg.hs * sizeof(uint16_t);
  b += 5 * static_cast<size_t>(T) * hc_dim * sizeof(uint16_t);
  return (b + 255) & ~size_t(255);
}

// ---------------------------------------------------------------------------
// GPU argmax over `rows` BF16 logits rows [rows, vocab] -> out[row] = argmax.
// Tie-break = lowest index (matches the host scan's strict-> semantics).
// Replaces the per-row "500 KB sync D2H + host CPU scan" (the MTP hot path
// did this 7x/step, leaving the GPU idle while the CPU scanned 248320 floats).
namespace {
constexpr int kArgmaxBlock = 256;
}  // namespace

__global__ void ArgmaxBf16RowsKernel(const uint16_t* __restrict__ logits,
                                     int vocab, int* __restrict__ out) {
  const int row = blockIdx.y;
  const uint16_t* lg = logits + static_cast<size_t>(row) * vocab;
  float bestv = -1e30f;
  int besti = 0;
  for (int v = threadIdx.x; v < vocab; v += kArgmaxBlock) {
    const float x = Bf16ToFloat(lg[v]);
    if (x > bestv) {
      bestv = x;
      besti = v;
    }
  }
  __shared__ float s_v[kArgmaxBlock];
  __shared__ int s_i[kArgmaxBlock];
  s_v[threadIdx.x] = bestv;
  s_i[threadIdx.x] = besti;
  __syncthreads();
  for (int s = kArgmaxBlock / 2; s > 0; s >>= 1) {
    if (threadIdx.x < s) {
      const float ov = s_v[threadIdx.x + s];
      const int oi = s_i[threadIdx.x + s];
      if (ov > s_v[threadIdx.x] || (ov == s_v[threadIdx.x] && oi < s_i[threadIdx.x])) {
        s_v[threadIdx.x] = ov;
        s_i[threadIdx.x] = oi;
      }
    }
    __syncthreads();
  }
  if (threadIdx.x == 0) out[row] = s_i[0];
}

Status ArgmaxBf16Rows(const uint16_t* logits, int rows, int vocab, int32_t* out,
                      cudaStream_t stream) {
  if (rows <= 0) return Status();
  const dim3 block(kArgmaxBlock);
  const dim3 grid(1, rows);
  ArgmaxBf16RowsKernel<<<grid, block, 0, stream>>>(logits, vocab, out);
  return cudaGetLastError() == cudaSuccess
             ? Status()
             : Status::Fail("ArgmaxBf16Rows: kernel launch");
}

// ---------------------------------------------------------------------------
// Draft-extend (see mtp.h): run the draft model over T tokens to build its
// full-attention KV, returning the last row's first-draft argmax + trunk.
// ---------------------------------------------------------------------------
Status MtpDraftExtend(const MtpModel& m, const int32_t* shifted_ids,
                      const uint16_t* main_trunk, const int* positions, int T,
                      int32_t* out_d0, uint16_t* out_g, cudaStream_t stream) {
  if (T <= 0) return Status::Fail("MtpDraftExtend: T must be > 0");
  const int hc_dim = m.hc_dim();
  const int hs = m.cfg.hs;
  const int vocab = m.cfg.vocab;

  // Use the persistent per-step scratch when T fits (the common internal-extend
  // case, T = a+1 <= k+1); otherwise fall back to per-call allocation (the
  // initial prompt extend, T = P, which is large and one-shot).
  const bool use_scratch = m.k_max > 0 && T <= m.k_max;
  int32_t* d_ids = use_scratch ? m.d_ids_scratch : nullptr;
  int* d_pos = use_scratch ? m.d_pos_scratch : nullptr;
  uint16_t* d_sample = use_scratch ? m.d_spec_sample : nullptr;  // [T, hs]
  uint16_t* d_multi = use_scratch ? m.d_spec_multi : nullptr;    // [T, hc_dim]
  uint16_t* d_logits = use_scratch ? m.d_spec_logits : nullptr;  // [T, vocab]
  auto cleanup = [&](Status s) -> Status {
    if (!use_scratch) {
      if (d_ids) cudaFree(d_ids);
      if (d_pos) cudaFree(d_pos);
      if (d_sample) cudaFree(d_sample);
      if (d_multi) cudaFree(d_multi);
      if (d_logits) cudaFree(d_logits);
    }
    return s;
  };
  if (!use_scratch) {
    if (cudaMalloc(reinterpret_cast<void**>(&d_ids),
                   static_cast<size_t>(T) * sizeof(int32_t)) != cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_pos),
                   static_cast<size_t>(T) * sizeof(int)) != cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_sample),
                   static_cast<size_t>(T) * hs * sizeof(uint16_t)) !=
            cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_multi),
                   static_cast<size_t>(T) * hc_dim * sizeof(uint16_t)) !=
            cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_logits),
                   static_cast<size_t>(T) * vocab * sizeof(uint16_t)) !=
            cudaSuccess)
      return cleanup(Status::Fail("MtpDraftExtend: cudaMalloc"));
  }

  cudaMemcpy(d_ids, shifted_ids, static_cast<size_t>(T) * sizeof(int32_t),
             cudaMemcpyHostToDevice);
  cudaMemcpy(d_pos, positions, static_cast<size_t>(T) * sizeof(int),
             cudaMemcpyHostToDevice);

  Status s = MtpForward(m, d_ids, d_pos, main_trunk, d_sample, d_multi,
                        d_logits, T, stream);
  if (!s.ok()) return cleanup(s);

  // out_g = last row's multi_hidden (draft trunk g_{last}).
  if (cudaMemcpy(out_g, d_multi + static_cast<size_t>(T - 1) * hc_dim,
                 static_cast<size_t>(hc_dim) * sizeof(uint16_t),
                 cudaMemcpyDeviceToDevice) != cudaSuccess)
    return cleanup(Status::Fail("MtpDraftExtend: D2D out_g"));

  // out_d0 = argmax of the last row's logits (first draft token), on the GPU
  // (1 kernel + 4-byte D2H). d_ids is reused as the 1-int output (the input
  // ids are no longer needed after MtpForward).
  {
    Status s = ArgmaxBf16Rows(d_logits + static_cast<size_t>(T - 1) * vocab,
                              1, vocab, d_ids, stream);
    if (!s.ok()) return cleanup(s);
    if (cudaMemcpy(out_d0, d_ids, sizeof(int32_t), cudaMemcpyDeviceToHost) !=
        cudaSuccess)
      return cleanup(Status::Fail("MtpDraftExtend: D2H argmax"));
  }
  return cleanup(Status());
}

// ---------------------------------------------------------------------------
// Speculative decoding step (see mtp.h).
//
// Alignment (scheme A, EAGLE-shift, mirrors the vLLM MTP proposer):
//   - The draft KV is already built to position P-1 (MtpDraftExtend over the
//     prompt for the first step; the previous step's internal extend after).
//   - b = t_P (main's bonus, from prefill/prev step); d0 = draft's t_{P+1};
//     g_in = draft trunk g_{P-1}.
//   - draft loop writes speculative KV[P..P+k-2]; lazy verification feeds ONLY
//     the accepted tokens (b + accepted drafts), so the main recurrent state
//     advances exactly over the accepted prefix — no snapshot/rollback needed.
//   - internal extend rebuilds draft KV[P..P+a] from the captured main trunks
//     (matching vLLM's next-call first pass over the accepted tokens).
// ---------------------------------------------------------------------------
Status MtpSpeculativeStep(const model::Model& main, const MtpModel& mtp,
                          model::ModelSequence* seq, int32_t b, int32_t d0,
                          const uint16_t* g_in, int k, int32_t* accepted_tokens,
                          int* accepted_count, int32_t* next_b,
                          int32_t* next_d0, uint16_t* next_g,
                          cudaStream_t stream) {
  if (!seq || !g_in || !accepted_tokens || !accepted_count || !next_b ||
      !next_d0 || !next_g)
    return Status::Fail("MtpSpeculativeStep: null arg");
  if (k <= 0) return Status::Fail("MtpSpeculativeStep: k must be > 0");
  if (seq->stage != model::ModelSequence::Stage::kDecode)
    return Status::Fail("MtpSpeculativeStep: sequence not in decode stage");
  const int P = seq->position;
  const int vocab = mtp.cfg.vocab;
  const int hc_dim = mtp.hc_dim();

  // Verification + draft-loop buffers. Use the persistent per-step scratch
  // when k+1 fits (the common case); otherwise fall back to per-call
  // allocation. d_vlogits = [k+1, vocab] verify logits, d_vtrunk = [k+1,
  // hc_dim] main trunks (h_P..h_{P+k}), d_g = [hc_dim] rolling draft trunk.
  const bool use_scratch = mtp.k_max > 0 && k + 1 <= mtp.k_max;
  uint16_t* d_vlogits = use_scratch ? mtp.d_spec_logits : nullptr;
  uint16_t* d_vtrunk = use_scratch ? mtp.d_spec_trunk : nullptr;
  uint16_t* d_g = use_scratch ? mtp.d_g : nullptr;
  auto cleanup = [&](Status s) -> Status {
    if (!use_scratch) {
      if (d_vlogits) cudaFree(d_vlogits);
      if (d_vtrunk) cudaFree(d_vtrunk);
      if (d_g) cudaFree(d_g);
    }
    return s;
  };
  if (!use_scratch) {
    if (cudaMalloc(reinterpret_cast<void**>(&d_vlogits),
                   static_cast<size_t>(k + 1) * vocab * sizeof(uint16_t)) !=
            cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_vtrunk),
                   static_cast<size_t>(k + 1) * hc_dim * sizeof(uint16_t)) !=
            cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_g),
                   static_cast<size_t>(hc_dim) * sizeof(uint16_t)) !=
            cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStep: cudaMalloc"));
  }

  const bool timing = getenv("Q4T_MTP_TIMING") != nullptr;
  const auto t_draft0 = std::chrono::steady_clock::now();
  // 1. Draft loop: drafts[0] = d0 (given); generate drafts[1..k-1].
  std::vector<int32_t> drafts(k);
  drafts[0] = d0;
  if (cudaMemcpy(d_g, g_in, static_cast<size_t>(hc_dim) * sizeof(uint16_t),
                 cudaMemcpyDeviceToDevice) != cudaSuccess)
    return cleanup(Status::Fail("MtpSpeculativeStep: D2D g_in"));
  for (int j = 1; j < k; ++j) {
    const int pos = P + j - 1;  // EAGLE shift: token t_{P+j} fed at P+j-1.
    int32_t h_ids[1] = {drafts[j - 1]};
    int h_pos[1] = {pos};
    // Reuse the persistent scratch's first element (no per-iteration malloc).
    int32_t* d_ids = use_scratch ? mtp.d_ids_scratch : nullptr;
    int* d_pos = use_scratch ? mtp.d_pos_scratch : nullptr;
    if (!use_scratch) {
      if (cudaMalloc(reinterpret_cast<void**>(&d_ids), sizeof(int32_t)) !=
              cudaSuccess ||
          cudaMalloc(reinterpret_cast<void**>(&d_pos), sizeof(int)) !=
              cudaSuccess) {
        if (d_ids) cudaFree(d_ids);
        if (d_pos) cudaFree(d_pos);
        return cleanup(Status::Fail("MtpSpeculativeStep: cudaMalloc draft"));
      }
    }
    cudaMemcpy(d_ids, h_ids, sizeof(int32_t), cudaMemcpyHostToDevice);
    cudaMemcpy(d_pos, h_pos, sizeof(int), cudaMemcpyHostToDevice);
    Status s = MtpForward(mtp, d_ids, d_pos, d_g, mtp.d_sample, mtp.d_trunk,
                          mtp.d_logits, 1, stream);
    if (!use_scratch) cudaFree(d_pos);
    if (!s.ok()) return cleanup(s);
    // GPU argmax (1 kernel + 4-byte D2H) instead of 500 KB D2H + host scan.
    // Reuses d_ids as the 1-int output (input ids are no longer needed).
    s = ArgmaxBf16Rows(mtp.d_logits, 1, vocab, d_ids, stream);
    if (!s.ok()) return cleanup(s);
    if (cudaMemcpy(&drafts[j], d_ids, sizeof(int32_t),
                   cudaMemcpyDeviceToHost) != cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStep: D2H draft argmax"));
    if (!use_scratch) cudaFree(d_ids);
    if (cudaMemcpy(d_g, mtp.d_trunk,
                   static_cast<size_t>(hc_dim) * sizeof(uint16_t),
                   cudaMemcpyDeviceToDevice) != cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStep: D2D roll g"));
  }

  const auto t_draft1 = std::chrono::steady_clock::now();
  const bool dbg = getenv("Q4T_MTP_DEBUG") != nullptr;

  // 2. Batched verification: feed [b, d_0..d_{k-1}] (k+1 tokens) at absolute
  //    positions P..P+k in ONE main forward, saving per-token SSM/conv
  //    checkpoints. The batch advances the recurrent state k+1 steps, but only
  //    1+a accepted tokens should stick; a partial accept restores the
  //    accepted-prefix boundary from checkpoint[a] via D2D (no re-advance).
  //    The paged full-attention KV is keyed by absolute position and is
  //    overwritten by the next step, so it needs no rollback.
  std::vector<int32_t> verify_ids(k + 1);
  verify_ids[0] = b;
  for (int i = 0; i < k; ++i) verify_ids[i + 1] = drafts[i];
  {
    Status s = model::ModelDecodeBatch(
        main, verify_ids.data(), k + 1, P, seq->history.data(),
        static_cast<int>(seq->history.size()), d_vlogits, stream, d_vtrunk,
        /*save_checkpoints=*/true);
    if (!s.ok()) return cleanup(s);
  }
  // m_i = argmax(logits[i]) on the GPU (1 kernel over k+1 rows + (k+1)*4-byte
  // D2H) instead of (k+1) full-vocab D2H + host scans. Device output reuses
  // the ids scratch (draft loop is done; k_max >= k+1).
  int32_t* d_argmax = use_scratch ? mtp.d_ids_scratch : nullptr;
  if (!use_scratch) {
    if (cudaMalloc(reinterpret_cast<void**>(&d_argmax),
                   static_cast<size_t>(k + 1) * sizeof(int32_t)) != cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStep: cudaMalloc argmax"));
  }
  {
    Status s = ArgmaxBf16Rows(d_vlogits, k + 1, vocab, d_argmax, stream);
    if (!s.ok()) return cleanup(s);
  }
  const auto t_verify1 = std::chrono::steady_clock::now();
  std::vector<int32_t> m_argmax(k + 1);
  if (cudaMemcpy(m_argmax.data(), d_argmax,
                 static_cast<size_t>(k + 1) * sizeof(int32_t),
                 cudaMemcpyDeviceToHost) != cudaSuccess)
    return cleanup(Status::Fail("MtpSpeculativeStep: D2H verify argmax"));
  if (!use_scratch) cudaFree(d_argmax);
  int a = 0;
  for (int i = 0; i < k; ++i) {
    const int mi = m_argmax[i];
    if (dbg && i < 3)
      std::fprintf(stderr, "[mtp-debug] P=%d i=%d draft=%d main=%d match=%d\n",
                   P, i, drafts[i], mi, drafts[i] == mi ? 1 : 0);
    if (drafts[i] != mi) break;
    a = i + 1;
  }
  const int32_t correction = m_argmax[a];

  // 3. Reconcile the main recurrent state to P+1+a. The batch advanced it to
  //    P+k+1; on full accept (a==k) that is already correct, else restore the
  //    per-token checkpoint[a] (= state after accepting [b, d_0..d_{a-1}]) via
  //    D2D — no re-advance. The full-attention KV is keyed by absolute position
  //    and is rewritten identically by the next step; trunks are already
  //    captured in d_vtrunk.
  if (a < k) {
    Status s = model::ModelRestoreCheckpoint(main, a, stream);
    if (!s.ok()) return cleanup(s);
  }
  // Advance the seq state machine over the accepted tokens [b, d_0..d_{a-1}].
  seq->position = P + 1 + a;
  seq->history.push_back(b);
  for (int i = 0; i < a; ++i) seq->history.push_back(drafts[i]);

  // 4. Emit accepted = [b, drafts[0..a-1]]; correction = m_a = next bonus.
  accepted_tokens[0] = b;
  for (int i = 0; i < a; ++i) accepted_tokens[1 + i] = drafts[i];
  *accepted_count = 1 + a;
  *next_b = correction;

  // 5. Internal extend: rebuild draft KV[P..P+a] from the captured main trunks
  //    d_vtrunk[0..a] (= h_P..h_{P+a}), input = accepted drafts + correction,
  //    yielding the next step's first draft (next_d0) + trunk (next_g).
  {
    std::vector<int32_t> ext_ids(a + 1);
    std::vector<int> ext_pos(a + 1);
    for (int i = 0; i < a; ++i) {
      ext_ids[i] = drafts[i];
      ext_pos[i] = P + i;
    }
    ext_ids[a] = correction;
    ext_pos[a] = P + a;
    Status s = MtpDraftExtend(mtp, ext_ids.data(), d_vtrunk, ext_pos.data(),
                              a + 1, next_d0, next_g, stream);
    if (!s.ok()) return cleanup(s);
  }
  if (timing) {
    const auto t_end = std::chrono::steady_clock::now();
    auto ms = [](auto lo, auto hi) {
      return std::chrono::duration<double, std::milli>(hi - lo).count();
    };
    std::fprintf(stderr,
                 "[mtp-timing] draft=%.1f verify=%.1f extend+accept=%.1f ms "
                 "(a=%d k=%d)\n",
                 ms(t_draft0, t_draft1), ms(t_draft1, t_verify1),
                 ms(t_verify1, t_end), a, k);
  }
  return cleanup(Status());
}


Status MtpForward(const MtpModel& m, const int32_t* input_ids,
                  const int* positions, const uint16_t* hidden_states,
                  uint16_t* sample_hidden, uint16_t* multi_hidden,
                  uint16_t* logits, int T, cudaStream_t stream) {
  const int hs = m.cfg.hs, hc = m.cfg.hc, hc_dim = hc * hs;
  if (T <= 0) return Status();
  const auto& cfg = m.cfg;
  if (m.ws_bytes < MtpWorkspaceBytes(cfg, T, m.full_attn)) {
    return Status::Fail("MtpForward: workspace too small");
  }
  // Carve the workspace into per-submodule regions (mirrors
  // DecoderLayerForward): full-attention scratch, the BF16 MoE workspace, two
  // 32 MiB GEMM scratch regions (one for the MoE GEMMs, one for the HC/fc/lm
  // GEMMs), then the activation scratch.
  const size_t attn_ws = model::FullAttentionWorkspaceBytes(m.full_attn, T);
  const size_t moe_carve = MoeBf16WorkspaceBytes(T, cfg.topk, hs, cfg.moe_is,
                                                 cfg.E) +
                           MoeBf16ScratchBytes(T, cfg.topk, hs, cfg.shared_is,
                                               cfg.E);
  uint8_t* ws = static_cast<uint8_t*>(m.d_ws);
  size_t off = 0;
  auto carve = [&](size_t bytes) -> void* {
    void* p = ws + off;
    off += (bytes + 255) & ~size_t(255);
    return p;
  };
  void* d_attn_ws = carve(attn_ws);
  void* d_moe_ws = carve(moe_carve);
  void* d_moe_gemm = carve(kGemmWs);
  void* d_hc_gemm = carve(kGemmWs);

  uint16_t* d_emb = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hs * sizeof(uint16_t)));
  uint16_t* d_normed_emb = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hs * sizeof(uint16_t)));
  uint16_t* d_prev_block = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hs * sizeof(uint16_t)));
  uint16_t* d_normed_hs = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hc_dim * sizeof(uint16_t)));
  uint16_t* d_trunk = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hc_dim * sizeof(uint16_t)));
  uint16_t* d_mixed_attn = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hs * sizeof(uint16_t)));
  uint16_t* d_normed_attn = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hc_dim * sizeof(uint16_t)));
  uint16_t* d_attn_out = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hs * sizeof(uint16_t)));
  uint16_t* d_trunk_a = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hc_dim * sizeof(uint16_t)));
  uint16_t* d_mixed_mlp = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hs * sizeof(uint16_t)));
  uint16_t* d_normed_mlp = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hc_dim * sizeof(uint16_t)));
  uint16_t* d_mlp_out = static_cast<uint16_t*>(carve(static_cast<size_t>(T) * hs * sizeof(uint16_t)));

  Status s;
  // 1. emb = embed_tokens(input_ids) [T, hs].
  {
    model::ModelHeadWeights head_view;
    head_view.embed_tokens = const_cast<uint16_t*>(m.embed_tokens);
    head_view.vocab = cfg.vocab;
    head_view.hs = hs;
    s = model::EmbedLookup(head_view, input_ids, d_emb, T, stream);
    if (!s.ok()) return s;
  }
  // 2. prev_block = fc_embedding(pre_fc_norm_embedding(emb)) [T, hs].
  GemmaRmsNormKernel<<<T, kBlock, 0, stream>>>(d_emb, m.pre_fc_norm_embedding,
                                               d_normed_emb, T, hs, cfg.eps);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("rmsnorm emb");
  s = CheckGemm(model::Bf16Gemm(d_normed_emb, m.fc_embedding, d_prev_block, T,
                                hs, hs, 1.0f, 0.0f, d_hc_gemm, kGemmWs, stream));
  if (!s.ok()) return s;

  // 3. hs_multi = pre_fc_norm_hidden(hidden_states) [T, hc*hs] (grouped norm),
  //    then fc_hidden per branch (shared H->H over the flattened [T, hc*hs]
  //    is WRONG — fc_hidden is [H, H] applied per branch). vLLM does
  //    fc_hidden(hidden.view(T, hc, H)) which broadcasts the [H, H] weight
  //    over the hc dimension: out[t, b, :] = hidden[t, b, :] @ W^T. We do it
  //    as one GEMM over [T*hc, H] (the rows are independent per branch).
  GroupedRmsNormKernel<<<T, kBlock, 0, stream>>>(hidden_states,
                                                 m.pre_fc_norm_hidden,
                                                 d_normed_hs, T, hc, hs,
                                                 cfg.eps);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("grouped rmsnorm");
  s = CheckGemm(model::Bf16Gemm(d_normed_hs, m.fc_hidden, d_trunk, T * hc, hs,
                                hs, 1.0f, 0.0f, d_hc_gemm, kGemmWs, stream));
  if (!s.ok()) return s;

  // 4. MTP decoder layer (delayed combine).
  // 4a. unit combine: trunk = hs_multi + prev_block (broadcast to branches).
  UnitCombineKernel<<<(T * hc_dim + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
      d_trunk, d_prev_block, d_trunk, T, hc, hs);
  if (cudaGetLastError() != cudaSuccess) return Status::Fail("unit combine");
  // 4b. attn_hc.mix.
  s = model::HyperConnectionMix(m.attn_hc, d_trunk, d_mixed_attn,
                                d_normed_attn, T, d_hc_gemm, kGemmWs, stream);
  if (!s.ok()) return s;
  // 4c. full attention.
  s = model::FullAttentionForward(m.full_attn, d_mixed_attn, d_attn_out,
                                  positions, m.d_rope_pos, m.kv_cache,
                                  m.page_table, m.idx_raw, m.idx_comp, T,
                                  d_attn_ws, attn_ws, stream);
  if (!s.ok()) return s;
  // 4d. attn_hc.combine: trunk_a = attn_out + trunk (learned injection).
  s = model::HyperConnectionCombine(m.attn_hc, d_attn_out, d_trunk,
                                    d_normed_attn, d_trunk_a, T, d_hc_gemm,
                                    kGemmWs, stream);
  if (!s.ok()) return s;
  // 4e. mlp_hc.mix.
  s = model::HyperConnectionMix(m.mlp_hc, d_trunk_a, d_mixed_mlp, d_normed_mlp,
                                T, d_hc_gemm, kGemmWs, stream);
  if (!s.ok()) return s;
  // 4f. BF16 MoE.
  s = MoeBf16Forward(d_mixed_mlp, m.moe, m.moe_extra, d_mlp_out, T, cfg.topk,
                     d_moe_ws, moe_carve, d_moe_gemm, kGemmWs, stream);
  if (!s.ok()) return s;

  // 5. Final mixer: combine (learned, using mlp_hc's injection) + mix.
  //    multi_hidden = mlp_hc.combine(mlp_out, trunk_a, normed_mlp) [T, hc*hs].
  s = model::HyperConnectionCombine(m.mlp_hc, d_mlp_out, d_trunk_a,
                                    d_normed_mlp, multi_hidden, T, d_hc_gemm,
                                    kGemmWs, stream);
  if (!s.ok()) return s;
  //    sample_hidden = mixer.mix(multi_hidden) [T, hs].
  s = model::HyperConnectionMix(m.mixer, multi_hidden, sample_hidden,
                                d_normed_mlp, T, d_hc_gemm, kGemmWs, stream);
  if (!s.ok()) return s;

  // 6. logits = lm_head(sample_hidden) [T, vocab].
  {
    model::ModelHeadWeights head_view;
    head_view.lm_head = const_cast<uint16_t*>(m.lm_head);
    head_view.vocab = cfg.vocab;
    head_view.hs = hs;
    s = CheckGemm(model::Bf16Gemm(sample_hidden, head_view.lm_head, logits, T,
                                  cfg.vocab, hs, 1.0f, 0.0f, d_hc_gemm, kGemmWs,
                                  stream));
    if (!s.ok()) return s;
  }
  return Status();
}

}  // namespace mtp
}  // namespace q4t
