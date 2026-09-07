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
// Host-side BF16 -> float (for the speculative step's argmax on host logits).
static float Bf16ToFloatHost(uint16_t b) {
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
  if (d_ws) cudaFree(d_ws);
  if (d_sample) cudaFree(d_sample);
  if (d_trunk) cudaFree(d_trunk);
  if (d_logits) cudaFree(d_logits);
  fc_embedding = nullptr;
  fc_hidden = nullptr;
  pre_fc_norm_embedding = nullptr;
  pre_fc_norm_hidden = nullptr;
  kv_cache = nullptr;
  page_table = nullptr;
  idx_raw = nullptr;
  idx_comp = nullptr;
  d_ws = nullptr;
  d_sample = nullptr;
  d_trunk = nullptr;
  d_logits = nullptr;
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
// Speculative decoding step (see mtp.h).
//
// Alignment (scheme A, mirrors the vLLM MTP proposer):
//   - trunk_in = the main model's pre-final-mixer multi stream at position
//     P-1 (the last prompt token, or the prior speculative step's next_trunk).
//   - draft step i (i=0..k-1): input token = the token at position P+i
//     (i=0: the last prompt token; i>0: the prior draft token), hidden =
//     trunk_{P+i-1}. It predicts the token at position P+i+1 (draft[i]) and
//     emits trunk_{P+i}.
//   - verification: the main model at position P+i predicts the token at
//     P+i+1; draft[i] is accepted iff it equals that argmax.
//
// Rollback: the main model's paged KV/indexer caches are written at absolute
// positions and are overwritten identically by the re-run, so they need no
// rollback. Only the recurrent state (linear SSM/conv, PLE conv) is updated
// in place; it is snapshotted before verification and restored before the
// re-run of the accepted prefix.
// ---------------------------------------------------------------------------

Status MtpSpeculativeStep(const model::Model& main, const MtpModel& mtp,
                          model::ModelSequence* seq, const uint16_t* trunk_in,
                          int k, int32_t* accepted_tokens, int* accepted_count,
                          uint16_t* next_trunk, cudaStream_t stream) {
  if (!seq || !trunk_in || !accepted_tokens || !accepted_count || !next_trunk)
    return Status::Fail("MtpSpeculativeStep: null arg");
  if (k <= 0) return Status::Fail("MtpSpeculativeStep: k must be > 0");
  if (seq->stage != model::ModelSequence::Stage::kDecode) {
    return Status::Fail("MtpSpeculativeStep: sequence not in decode stage");
  }
  const int P = seq->position;
  const int vocab = mtp.cfg.vocab;

  // Host buffers.
  std::vector<int32_t> draft(k);
  std::vector<int32_t> accepted(k + 1);

  // Device buffers: the main model's per-step logits [vocab] (the main model's
  // own d_ws is too small for a full-vocab logits row) and the per-step trunk
  // [hc*hs]. The draft step reuses the MtpModel's d_trunk/d_logits.
  uint16_t* d_main_logits = nullptr;
  uint16_t* d_trunk_step = nullptr;
  const size_t hc_dim = static_cast<size_t>(mtp.hc_dim());
  if (cudaMalloc(reinterpret_cast<void**>(&d_main_logits),
                 static_cast<size_t>(vocab) * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc d_main_logits");
  if (cudaMalloc(reinterpret_cast<void**>(&d_trunk_step),
                 hc_dim * sizeof(uint16_t)) != cudaSuccess) {
    cudaFree(d_main_logits);
    return Status::Fail("cudaMalloc d_trunk_step");
  }

  auto cleanup = [&](Status s) -> Status {
    cudaFree(d_main_logits);
    cudaFree(d_trunk_step);
    return s;
  };

  // 1. Snapshot the main model's recurrent state (pre-verification).
  model::ModelStateSnapshot snap;
  {
    Status s = model::ModelSnapshotState(main, &snap, stream);
    if (!s.ok()) return cleanup(s);
  }

  // 2. Draft phase: k MTP steps. The MTP full-attention KV/indexer caches are
  //    ACCUMULATED across speculative steps (the draft model builds its own KV
  //    over all draft tokens, mirroring the main model's decode KV). Call
  //    MtpResetState once before the FIRST speculative step of a sequence
  //    (mirroring the main model's ModelBeginSequence), not here.
  for (int i = 0; i < k; ++i) {
    const int pos = P + i;
    // input token: i=0 -> last prompt token (history[P-1]); i>0 -> draft[i-1].
    const int32_t in_tok =
        (i == 0) ? seq->history[static_cast<size_t>(P) - 1] : draft[i - 1];
    int32_t h_ids[1] = {in_tok};
    int h_pos[1] = {pos};
    int32_t* d_ids = nullptr;
    int* d_pos = nullptr;
    if (cudaMalloc(reinterpret_cast<void**>(&d_ids), sizeof(int32_t)) !=
            cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_pos), sizeof(int)) !=
            cudaSuccess) {
      if (d_ids) cudaFree(d_ids);
      if (d_pos) cudaFree(d_pos);
      return cleanup(Status::Fail("cudaMalloc draft ids/pos"));
    }
    cudaMemcpy(d_ids, h_ids, sizeof(int32_t), cudaMemcpyHostToDevice);
    cudaMemcpy(d_pos, h_pos, sizeof(int), cudaMemcpyHostToDevice);
    // hidden_states = trunk_{P+i-1}: i=0 -> trunk_in (caller), i>0 -> the
    // prior draft step's multi_hidden (mtp.d_trunk).
    const uint16_t* hidden_src = (i == 0) ? trunk_in : mtp.d_trunk;
    Status s = MtpForward(mtp, d_ids, d_pos, hidden_src, mtp.d_sample,
                          mtp.d_trunk, mtp.d_logits, 1, stream);
    cudaFree(d_ids);
    cudaFree(d_pos);
    if (!s.ok()) return cleanup(s);
    // argmax of the draft logits (host).
    std::vector<uint16_t> lg(static_cast<size_t>(vocab));
    if (cudaMemcpy(lg.data(), mtp.d_logits, lg.size() * sizeof(uint16_t),
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      return cleanup(Status::Fail("D2H draft logits"));
    }
    int best = 0;
    float bestv = -1e30f;
    for (int v = 0; v < vocab; ++v) {
      const float x = Bf16ToFloatHost(lg[static_cast<size_t>(v)]);
      if (x > bestv) {
        bestv = x;
        best = v;
      }
    }
    draft[i] = best;
  }

  // 3. Verification: k main-model decode steps (positions P..P+k-1). Each
  //    predicts the token at position P+i+1; compare with draft[i].
  //    ModelDecodeStepSeq auto-advances seq->position/history, so snapshot
  //    them now and restore after verification (the re-run re-advances them
  //    over the accepted prefix only).
  const int saved_position = seq->position;
  const std::vector<int32_t> saved_history = seq->history;
  // main_pred[i] = the main model's argmax at position P+i (predicts the token
  // at P+i+1). Used both for the accept decision and, when a==0, as the bonus
  // token (the main model's own next token at position P).
  std::vector<int> main_pred(k);
  int a = 0;
  for (int i = 0; i < k; ++i) {
    Status s = model::ModelDecodeStepSeq(main, seq, draft[i], d_main_logits,
                                         stream, nullptr);
    if (!s.ok()) return cleanup(s);
    std::vector<uint16_t> lg(static_cast<size_t>(vocab));
    if (cudaMemcpy(lg.data(), d_main_logits, lg.size() * sizeof(uint16_t),
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      return cleanup(Status::Fail("D2H main logits"));
    }
    int best = 0;
    float bestv = -1e30f;
    for (int v = 0; v < vocab; ++v) {
      const float x = Bf16ToFloatHost(lg[static_cast<size_t>(v)]);
      if (x > bestv) {
        bestv = x;
        best = v;
      }
    }
    main_pred[i] = best;
    if (best != draft[i]) break;
    a = i + 1;
  }

  // 4. Rollback: restore the recurrent state AND the seq state machine to
  //    pre-verification, then re-run the a accepted tokens to advance the
  //    recurrent state correctly and to obtain the next trunk + the bonus
  //    token. The paged KV/indexer caches are overwritten identically by the
  //    re-run (no rollback needed).
  //
  //    NOTE: when a=0, we do NOT re-run any tokens (the verification phase
  //    already advanced the seq by 1, which is the correct final state).
  //    When a>0, we re-run the a accepted tokens to advance the seq by a.
  {
    Status s = model::ModelRestoreState(main, snap, stream);
    if (!s.ok()) return cleanup(s);
  }
  if (a > 0) {
    seq->position = saved_position;
    seq->history = saved_history;
  }
  // Bonus token: the main model's own next token at position P+a.
  //   - a == 0: the main model's argmax at position P (main_pred[0], already
  //     computed during verification). The seq is already at position P+1
  //     (advanced by the verification decode step), so no re-run is needed.
  //   - a > 0: the main model's argmax at position P+a-1, obtained from the
  //     re-run's last decode step.
  int32_t bonus = -1;
  if (a == 0) {
    bonus = main_pred[0];
    // The seq is already at position P+1 (advanced by the verification
    // decode step at position P). The next_trunk is the main model's trunk
    // at position P-1 (the prefill's last row), which is the correct state
    // for the next speculative step.
    if (cudaMemcpy(next_trunk, trunk_in, hc_dim * sizeof(uint16_t),
                   cudaMemcpyDeviceToDevice) != cudaSuccess) {
      return cleanup(Status::Fail("D2D next_trunk (a=0)"));
    }
  } else {
    for (int i = 0; i < a; ++i) {
      Status s = model::ModelDecodeStepSeq(main, seq, draft[i], d_main_logits,
                                           stream, d_trunk_step);
      if (!s.ok()) return cleanup(s);
      if (i == a - 1) {
        // Copy the accepted prefix's final trunk to the caller's next_trunk.
        if (cudaMemcpy(next_trunk, d_trunk_step, hc_dim * sizeof(uint16_t),
                       cudaMemcpyDeviceToDevice) != cudaSuccess) {
          return cleanup(Status::Fail("D2D next_trunk"));
        }
        std::vector<uint16_t> lg(static_cast<size_t>(vocab));
        if (cudaMemcpy(lg.data(), d_main_logits, lg.size() * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          return cleanup(Status::Fail("D2H bonus logits"));
        }
        int best = 0;
        float bestv = -1e30f;
        for (int v = 0; v < vocab; ++v) {
          const float x = Bf16ToFloatHost(lg[static_cast<size_t>(v)]);
          if (x > bestv) {
            bestv = x;
            best = v;
          }
        }
        bonus = best;
      }
    }
  }

  // 5. Emit the accepted tokens + the bonus token; advance the sequence.
  for (int i = 0; i < a; ++i) accepted[i] = draft[i];
  accepted[a] = bonus;
  for (int i = 0; i <= a; ++i) accepted_tokens[i] = accepted[i];
  *accepted_count = a + 1;
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
                                  positions, m.kv_cache, m.page_table,
                                  m.idx_raw, m.idx_comp, T, d_attn_ws, attn_ws,
                                  stream);
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
