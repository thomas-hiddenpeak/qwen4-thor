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
#include "q4t/mtp/subset_head.h"
#include <filesystem>
#include "q4t/mtp/position_copy.h"

#include <algorithm>

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

#include "q4t/diagnostic/execution_observer.h"
#include "q4t/model/full_attention.h"
#include "q4t/model/hyperconnection.h"
#include "q4t/model/linear.h"
#include "q4t/model/model_head.h"
#include "q4t/model/moe.h"

namespace q4t {
namespace mtp {

namespace {

// The observer is disabled by default. Its CPU scopes add no CUDA work or
// synchronization and keep existing API waits distinct from submission spans.
template <typename Function>
auto ObserveExecution(const char* label, Function&& function) {
  diagnostic::Scope scope(label);
  return function();
}

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

void FreeScratch(MtpModel& m) {
  if (m.d_ids_scratch) cudaFree(m.d_ids_scratch);
  if (m.d_pos_scratch) cudaFree(m.d_pos_scratch);
  if (m.d_spec_logits) cudaFree(m.d_spec_logits);
  if (m.d_spec_trunk) cudaFree(m.d_spec_trunk);
  if (m.d_spec_multi) cudaFree(m.d_spec_multi);
  if (m.d_spec_sample) cudaFree(m.d_spec_sample);
  if (m.d_g) cudaFree(m.d_g);
  if (m.d_ms_ids) cudaFree(m.d_ms_ids);
  if (m.d_ms_pos) cudaFree(m.d_ms_pos);
  if (m.d_ms_sample) cudaFree(m.d_ms_sample);
  if (m.d_ms_multi) cudaFree(m.d_ms_multi);
  if (m.d_ms_gather) cudaFree(m.d_ms_gather);
  if (m.d_ms_g_pool) cudaFree(m.d_ms_g_pool);
  if (m.d_ms_ext_seq) cudaFree(m.d_ms_ext_seq);
  if (m.d_ms_vlogits) cudaFree(m.d_ms_vlogits);
  if (m.d_ms_vtrunk) cudaFree(m.d_ms_vtrunk);
  if (m.d_ms_ext_logits) cudaFree(m.d_ms_ext_logits);
  if (m.d_ms_ext_multi) cudaFree(m.d_ms_ext_multi);
  if (m.d_ms_ext_sample) cudaFree(m.d_ms_ext_sample);
  if (m.d_ms_ext_ids) cudaFree(m.d_ms_ext_ids);
  if (m.d_ms_ext_pos) cudaFree(m.d_ms_ext_pos);
  if (m.d_ms_drafts) cudaFree(m.d_ms_drafts);
  if (m.d_ms_gather_off) cudaFree(m.d_ms_gather_off);
  m.d_ids_scratch = nullptr;
  m.d_pos_scratch = nullptr;
  m.d_spec_logits = nullptr;
  m.d_spec_trunk = nullptr;
  m.d_spec_multi = nullptr;
  m.d_spec_sample = nullptr;
  m.d_g = nullptr;
  m.d_ms_ids = nullptr;
  m.d_ms_pos = nullptr;
  m.d_ms_sample = nullptr;
  m.d_ms_multi = nullptr;
  m.d_ms_gather = nullptr;
  m.d_ms_g_pool = nullptr;
  m.d_ms_ext_seq = nullptr;
  m.d_ms_vlogits = nullptr;
  m.d_ms_vtrunk = nullptr;
  m.d_ms_ext_logits = nullptr;
  m.d_ms_ext_multi = nullptr;
  m.d_ms_ext_sample = nullptr;
  m.d_ms_ext_ids = nullptr;
  m.d_ms_ext_pos = nullptr;
  m.d_ms_drafts = nullptr;
  m.d_ms_gather_off = nullptr;
  m.gather_off_capacity = 0;
  m.k_max = 0;
}

}  // namespace

void MtpModel::Free() {
  metadata_upload.reset();
  if (subset_head) cudaFree(subset_head);
  if (subset_ids) cudaFree(subset_ids);
  subset_head = nullptr;
  subset_ids = nullptr;
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
  FreeScratch(*this);
  embed_tokens = nullptr;
  lm_head = nullptr;
  ws_bytes = 0;
  kv_bytes = 0;
  idx_bytes = 0;
  max_seq = 1;
}

Status LoadMtp(const MtpConfig& cfg, const uint16_t* main_embed,
               const uint16_t* main_lm_head, MtpModel* out,
               cudaStream_t stream) {
  if (!runtime::ValidSequenceCapacities(cfg.seq_capacities, cfg.max_len,
                                        cfg.max_seq))
    return Status::Fail("invalid MTP slot capacities");
  if (!out || !main_embed || !main_lm_head)
    return Status::Fail("LoadMtp: model and borrowed weights are required");
  if (cfg.initial_scratch_capacity < 0 || cfg.initial_scratch_capacity > 17)
    return Status::Fail("LoadMtp: initial scratch capacity must be in [0,17]");
  if (out->subset_head || out->subset_ids || out->embed_tokens ||
      out->lm_head || out->fc_embedding ||
      out->d_ids_scratch || out->k_max > 0)
    return Status::Fail("LoadMtp: output already owns resources");
  // Sub-loaders attach allocations to out as they proceed. Keep one failure
  // owner until every load and copy has completed, including the null stream.
  struct PendingLoad {
    MtpModel* model;
    cudaStream_t stream;
    ~PendingLoad() {
      if (!model) return;
      const cudaError_t error = cudaStreamSynchronize(stream);
      if (error != cudaSuccess) {
        std::fprintf(stderr, "[q4t] MTP failed-load drain: %s\n",
                     cudaGetErrorString(error));
      }
      model->Free();
    }
  } pending{out, stream};
  out->cfg = cfg;
  out->metadata_upload = std::make_unique<model::MetadataUploadBatch>();
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
    return loader->LoadTensorToDevice(name, dst, bytes, stream);
  };

  Status s;
  if (!cfg.subset_head_dir.empty()) {
    SubsetHeadData data;
    s = ReadSubsetHead(cfg.subset_head_dir,
                       std::filesystem::path(cfg.mtp_dir).parent_path().string(),
                       cfg.vocab, cfg.hs, &data);
    if (!s.ok()) return s;
    if (!(s = alloc(&out->subset_head, kSubsetWeightBytes))) return s;
    if (cudaMalloc(reinterpret_cast<void**>(&out->subset_ids),
                   kSubsetIdBytes) != cudaSuccess)
      return Status::Fail("subset head: allocate IDs");
    // Synchronous copies: pageable data is released before other MTP loads.
    if (cudaMemcpy(out->subset_head, data.weights.data(), kSubsetWeightBytes,
                   cudaMemcpyHostToDevice) != cudaSuccess ||
        cudaMemcpy(out->subset_ids, data.ids.data(), kSubsetIdBytes,
                   cudaMemcpyHostToDevice) != cudaSuccess)
      return Status::Fail("subset head: upload");
    std::fprintf(stderr, "[q4t] BF16 subset head rows=%d bytes=%zu\n",
                 kSubsetRows, kSubsetWeightBytes + kSubsetIdBytes);
  }
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
  //    Pooled over max_seq sequences (Phase 2 MTP continuous batching): each
  //    sequence owns an independent [seq_id, ...] slice so concurrent MTP
  //    requests don't corrupt each other's draft state. max_seq == 1
  //    reproduces the legacy single-sequence layout bit-for-bit.
  const int n_pages = (cfg.max_len + model::kKvPageSize - 1) /
                      model::kKvPageSize;
  const int max_seq = cfg.max_seq > 0 ? cfg.max_seq : 1;
  if (!runtime::ValidSequenceCapacities(cfg.seq_capacities, cfg.max_len,
                                        max_seq))
    return Status::Fail("invalid MTP slot capacities");
  if (!(s = model::ConfigureAttentionSlots(&out->full_attn,
                                           cfg.seq_capacities)))
    return s;
  const size_t pool_tokens =
      runtime::SequencePoolTokens(cfg.seq_capacities, cfg.max_len, max_seq);
  const size_t kv_tokens =
      cfg.seq_capacities.empty()
          ? static_cast<size_t>(max_seq) * n_pages * model::kKvPageSize
          : pool_tokens;
  out->max_seq = max_seq;
  out->kv_bytes =
      static_cast<size_t>(n_pages) * model::kKvPageSize * cfg.nkv * 2 *
      cfg.hd * sizeof(uint16_t);
  out->idx_bytes =
      static_cast<size_t>(cfg.max_len) * cfg.idx_head_dim * sizeof(uint16_t);
  if (cudaMalloc(reinterpret_cast<void**>(&out->kv_cache),
                 kv_tokens * cfg.nkv * 2 * cfg.hd * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc kv_cache");
  if (cudaMalloc(reinterpret_cast<void**>(&out->page_table),
                 static_cast<size_t>(max_seq) * cfg.max_len * sizeof(int)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc page_table");
  if (cudaMalloc(reinterpret_cast<void**>(&out->idx_raw),
                 pool_tokens * cfg.idx_head_dim * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc idx_raw");
  if (cudaMalloc(reinterpret_cast<void**>(&out->idx_comp),
                 pool_tokens * cfg.idx_head_dim * sizeof(uint16_t)) !=
      cudaSuccess)
    return Status::Fail("cudaMalloc idx_comp");
  // 3D MRoPE identity table [max_seq, 3, max_len]: all three rows = position
  // (pure text, delta=0). Pooled so the multi-seq attention kernels can index
  // the per-seq slice via d_seq_id (the identity is the same for every seq).
  if (cudaMalloc(reinterpret_cast<void**>(&out->d_rope_pos),
                 static_cast<size_t>(max_seq) * 3u * cfg.max_len *
                     sizeof(int)) != cudaSuccess)
    return Status::Fail("cudaMalloc d_rope_pos");
  {
    std::vector<int> rp(3 * static_cast<size_t>(cfg.max_len), 0);
    for (int p = 0; p < cfg.max_len; ++p)
      for (int r = 0; r < 3; ++r)
        rp[r * cfg.max_len + p] = p;
    for (int s = 0; s < max_seq; ++s)
      if (cudaMemcpy(out->d_rope_pos +
                         static_cast<size_t>(s) * 3 * cfg.max_len,
                     rp.data(), rp.size() * sizeof(int),
                     cudaMemcpyHostToDevice) != cudaSuccess)
        return Status::Fail("H2D d_rope_pos");
  }
  {
    std::vector<int> pt(cfg.max_len);
    for (int s = 0; s < max_seq; ++s) {
      const size_t page_base =
          cfg.seq_capacities.empty()
              ? 0
              : runtime::SequenceOffset(cfg.seq_capacities, s, cfg.max_len) /
                    model::kKvPageSize;
      const int capacity =
          runtime::SequenceCapacity(cfg.seq_capacities, s, cfg.max_len);
      for (int p = 0; p < cfg.max_len; ++p)
        pt[p] = p < capacity ? page_base + p / model::kKvPageSize : -1;
      if (cudaMemcpy(out->page_table + static_cast<size_t>(s) * cfg.max_len,
                     pt.data(), static_cast<size_t>(cfg.max_len) * sizeof(int),
                     cudaMemcpyHostToDevice) != cudaSuccess)
        return Status::Fail("H2D page_table");
    }
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

  if (cfg.initial_scratch_capacity > 0) {
    s = MtpReserveScratch(*out, cfg.initial_scratch_capacity);
    if (!s.ok()) return s;
  }
  if (cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("stream sync failed");
  }
  pending.model = nullptr;
  return Status();
}

size_t MtpPerSeqKvBytes(const MtpConfig& cfg) {
  const int n_pages = (cfg.max_len + model::kKvPageSize - 1) /
                      model::kKvPageSize;
  return static_cast<size_t>(n_pages) * model::kKvPageSize * cfg.nkv * 2 *
         cfg.hd * sizeof(uint16_t);
}

Status MtpResetState(const MtpModel& m, cudaStream_t stream, int seq_id) {
  const int max_seq = m.max_seq > 0 ? m.max_seq : 1;
  // Per-seq byte offsets into the pooled buffers. seq_id < 0 resets ALL
  // sequences (the whole pool); seq_id >= 0 resets one slice. Byte offsets go
  // through char* (decoupled from the element type, mirroring the B1 fix).
  if (seq_id < -1 || seq_id >= max_seq)
    return Status::Fail("MTP reset slot out of range");
  const auto& capacities = m.cfg.seq_capacities;
  const size_t offset =
      seq_id >= 0 ? runtime::SequenceOffset(capacities, seq_id, m.cfg.max_len)
                  : 0;
  const size_t count =
      seq_id >= 0
          ? runtime::SequenceCapacity(capacities, seq_id, m.cfg.max_len)
          : runtime::SequencePoolTokens(capacities, m.cfg.max_len, max_seq);
  const size_t kv_off =
      capacities.empty()
          ? (seq_id >= 0 ? static_cast<size_t>(seq_id) * m.kv_bytes : 0)
          : offset * m.cfg.nkv * 2 * m.cfg.hd * 2;
  const size_t idx_off = offset * m.cfg.idx_head_dim * 2;
  const size_t kv_total =
      capacities.empty()
          ? (seq_id >= 0 ? m.kv_bytes
                         : static_cast<size_t>(max_seq) * m.kv_bytes)
          : count * m.cfg.nkv * 2 * m.cfg.hd * 2;
  const size_t idx_total = count * m.cfg.idx_head_dim * 2;
  if (m.kv_cache &&
      cudaMemsetAsync(reinterpret_cast<char*>(m.kv_cache) + kv_off, 0, kv_total,
                      stream) != cudaSuccess)
    return Status::Fail("memset kv_cache");
  if (m.idx_raw &&
      cudaMemsetAsync(reinterpret_cast<char*>(m.idx_raw) + idx_off, 0,
                      idx_total, stream) != cudaSuccess)
    return Status::Fail("memset idx_raw");
  if (m.idx_comp &&
      cudaMemsetAsync(reinterpret_cast<char*>(m.idx_comp) + idx_off, 0,
                      idx_total, stream) != cudaSuccess)
    return Status::Fail("memset idx_comp");
  return Status();
}

Status MtpReserveScratch(MtpModel& m, int k_max) {
  if (k_max <= 0) return Status::Fail("MtpReserveScratch: k_max must be > 0");
  if (k_max <= m.k_max) return Status();  // already sized for this k
  const int hs = m.cfg.hs, hc_dim = m.hc_dim(), vocab = m.cfg.vocab;
  FreeScratch(m);
  struct PendingScratch {
    MtpModel* model;
    ~PendingScratch() {
      if (model) FreeScratch(*model);
    }
  } pending{&m};
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
  // Multi-seq speculative scratch (Stage 2b): the batched draft loop packs
  // B <= max_seq sequences per step (T = B), and the batched extend packs the
  // accepted prefixes (T = sum(a_b+1) <= B*(k+1)). ALWAYS allocated (not just
  // for max_seq > 1): since Stage 2c the scheduler routes EVERY MTP request
  // through MtpSpeculativeStepMulti, including a single request (B=1), so the
  // scratch must exist even when max_seq == 1 (the buffers are then tiny).
  {
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_ids),
                   static_cast<size_t>(m.max_seq) * sizeof(int32_t)) !=
        cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_ids");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_pos),
                   static_cast<size_t>(m.max_seq) * sizeof(int)) !=
        cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_pos");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_sample),
                   static_cast<size_t>(m.max_seq) * hs * sizeof(uint16_t)) !=
        cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_sample");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_multi),
                   static_cast<size_t>(m.max_seq) * hc_dim * sizeof(uint16_t)) !=
        cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_multi");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_gather),
                   static_cast<size_t>(m.max_seq) * k_max * hc_dim *
                       sizeof(uint16_t)) != cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_gather");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_g_pool),
                   static_cast<size_t>(m.max_seq) * hc_dim * sizeof(uint16_t)) !=
        cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_g_pool");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_ext_seq),
                   static_cast<size_t>(m.max_seq) * k_max * sizeof(int)) !=
        cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_ext_seq");
    const size_t Tmx = static_cast<size_t>(m.max_seq) * k_max;
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_vlogits),
                   Tmx * vocab * sizeof(uint16_t)) != cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_vlogits");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_vtrunk),
                   Tmx * hc_dim * sizeof(uint16_t)) != cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_vtrunk");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_ext_logits),
                   Tmx * vocab * sizeof(uint16_t)) != cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_ext_logits");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_ext_multi),
                   Tmx * hc_dim * sizeof(uint16_t)) != cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_ext_multi");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_ext_sample),
                   Tmx * hs * sizeof(uint16_t)) != cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_ext_sample");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_ext_ids),
                   Tmx * sizeof(int32_t)) != cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_ext_ids");
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_ext_pos),
                   Tmx * sizeof(int)) != cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_ext_pos");
    // Plan D: GPU-resident draft token matrix [max_seq, k_max].
    if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_drafts),
                   Tmx * sizeof(int32_t)) != cudaSuccess)
      return Status::Fail("MtpReserveScratch: d_ms_drafts");
  }
  const size_t offset_rows = static_cast<size_t>(m.max_seq) * k_max;
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_ms_gather_off),
                 offset_rows * sizeof(int)) != cudaSuccess)
    return Status::Fail("MtpReserveScratch: d_ms_gather_off");
  m.gather_off_capacity = offset_rows;
  m.k_max = k_max;
  pending.model = nullptr;
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
  b += static_cast<size_t>(T) * sizeof(int);  // d_positions (device positions)
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

__global__ void RemapDraftIds(int32_t* tokens, const int32_t* ids, int rows) {
  const int row = blockIdx.x * blockDim.x + threadIdx.x;
  if (row < rows) tokens[row] = ids[tokens[row]];
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

Status DraftArgmax(const MtpModel& model, const uint16_t* logits, int rows,
                   int32_t* output, cudaStream_t stream) {
  Status status = ArgmaxBf16Rows(logits, rows, model.DraftVocab(), output, stream);
  if (!status.ok() || !model.subset_ids || rows <= 0) return status;
  RemapDraftIds<<<(rows + 255) / 256, 256, 0, stream>>>(
      output, model.subset_ids, rows);
  return cudaGetLastError() == cudaSuccess
             ? Status() : Status::Fail("subset head: remap launch");
}


// ---------------------------------------------------------------------------
// Diagnostic (Q4T_MTP_GAP_DEBUG): per-row top-1/top-2 logit values + top-1
// index. Used to classify MTP rejections as "main confident" (large gap,
// draft wrong) vs "near-tie" (small gap, verify numerically borderline).
// Pure observation; never affects the accept decision.
__global__ void Top2GapBf16RowsKernel(const uint16_t* __restrict__ logits,
                                      int vocab, float* __restrict__ out) {
  const int row = blockIdx.y;
  const uint16_t* lg = logits + static_cast<size_t>(row) * vocab;
  float bestv = -1e30f, secondv = -1e30f;
  int besti = 0;
  for (int v = threadIdx.x; v < vocab; v += kArgmaxBlock) {
    const float x = Bf16ToFloat(lg[v]);
    if (x > bestv) {
      secondv = bestv;
      bestv = x;
      besti = v;
    } else if (x > secondv) {
      secondv = x;
    }
  }
  __shared__ float s_bv[kArgmaxBlock];
  __shared__ float s_sv[kArgmaxBlock];
  __shared__ int s_bi[kArgmaxBlock];
  s_bv[threadIdx.x] = bestv;
  s_sv[threadIdx.x] = secondv;
  s_bi[threadIdx.x] = besti;
  __syncthreads();
  for (int s = kArgmaxBlock / 2; s > 0; s >>= 1) {
    if (threadIdx.x < s) {
      const float a_bv = s_bv[threadIdx.x];
      const float a_sv = s_sv[threadIdx.x];
      const int a_bi = s_bi[threadIdx.x];
      const float b_bv = s_bv[threadIdx.x + s];
      const float b_sv = s_sv[threadIdx.x + s];
      const int b_bi = s_bi[threadIdx.x + s];
      if (b_bv > a_bv || (b_bv == a_bv && b_bi < a_bi)) {
        s_sv[threadIdx.x] = (a_bv > b_sv) ? a_bv : b_sv;
        s_bv[threadIdx.x] = b_bv;
        s_bi[threadIdx.x] = b_bi;
      } else {
        s_sv[threadIdx.x] = (b_bv > a_sv) ? b_bv : a_sv;
      }
    }
    __syncthreads();
  }
  // out layout per row: [v1, v2, i1]
  out[static_cast<size_t>(row) * 3 + 0] = s_bv[0];
  out[static_cast<size_t>(row) * 3 + 1] = s_sv[0];
  out[static_cast<size_t>(row) * 3 + 2] = static_cast<float>(s_bi[0]);
}

Status Top2GapBf16Rows(const uint16_t* logits, int rows, int vocab, float* out,
                       cudaStream_t stream) {
  if (rows <= 0) return Status();
  const dim3 block(kArgmaxBlock);
  const dim3 grid(1, rows);
  Top2GapBf16RowsKernel<<<grid, block, 0, stream>>>(logits, vocab, out);
  return cudaGetLastError() == cudaSuccess
             ? Status()
             : Status::Fail("Top2GapBf16Rows: kernel launch");
}

// ---------------------------------------------------------------------------
// Draft-extend (see mtp.h): run the draft model over T tokens to build its
// full-attention KV, returning the last row's first-draft argmax + trunk.
// ---------------------------------------------------------------------------
Status MtpDraftExtend(const MtpModel& m, const int32_t* shifted_ids,
                      const uint16_t* main_trunk, const int* positions, int T,
                      int32_t* out_d0, uint16_t* out_g, cudaStream_t stream,
                      int seq_id, bool finalize) {
  if (T <= 0) return Status::Fail("MtpDraftExtend: T must be > 0");
  if (seq_id < 0 || seq_id >= m.max_seq)
    return Status::Fail("MtpDraftExtend: seq_id out of range");
  if (!shifted_ids || !positions || !main_trunk ||
      (finalize && (!out_d0 || !out_g)) || (!finalize && T > m.cfg.max_prefill))
    return Status::Fail("MtpDraftExtend: invalid chunk arguments");
  for (int t = 0; t < T; ++t)
    if (positions[t] < 0 || positions[t] >= m.cfg.Capacity(seq_id))
      return Status::Fail("MtpDraftExtend: position exceeds slot capacity");
  diagnostic::Batch execution("mtp.prompt_extend", seq_id, -1, T);
  diagnostic::Scope prompt_scope("mtp.prompt_extend.total", -1, T);
  const int hc_dim = m.hc_dim();
  const int hs = m.cfg.hs;
  const int vocab = m.DraftVocab();

  // Chunked draft-extend (T > max_prefill): the forward workspace is sized for
  // max_prefill tokens, so a long prompt is run in chunks of max_prefill. The
  // draft model is a SINGLE layer (one full-attention + one MoE, no linear
  // SSM/conv) and its full-attention KV/indexer is written at ABSOLUTE
  // positions (the per-seq pooled slice, zeroed by MtpResetState before the
  // first chunk), so each chunk continues from the KV the previous chunks
  // wrote -- bit-identical to one big forward (same contract as the main
  // model's chunked prefill). Only the LAST chunk computes logits (its final
  // row is the first-draft-token argmax); intermediate chunks skip the lm_head
  // GEMM (a full-prompt [T, vocab] buffer would be ~22 GB at 44K).
  if (T > m.cfg.max_prefill) {
    if (!m.metadata_upload)
      return Status::Fail("MtpDraftExtend: upload owner missing");
    auto& upload = *m.metadata_upload;
    const int chunk = m.cfg.max_prefill;
    const int last_c = (T % chunk == 0) ? chunk : (T % chunk);
    int32_t* d_ids = nullptr;
    int* d_pos = nullptr;
    uint16_t* d_sample = nullptr;
    uint16_t* d_multi = nullptr;
    uint16_t* d_logits = nullptr;  // [last_c, vocab] -- last chunk's rows
    int* d_seqid = nullptr;
    auto cleanup = [&](Status s) -> Status {
      diagnostic::Scope cleanup_scope("mtp.prompt_extend.cleanup");
      Status drained = ObserveExecution("mtp.wait.metadata_drain", [&] {
        return upload.Drain();
      });
      if (!drained.ok()) s = drained;
      if (d_seqid) cudaFree(d_seqid);
      if (d_ids) cudaFree(d_ids);
      if (d_pos) cudaFree(d_pos);
      if (d_sample) cudaFree(d_sample);
      if (d_multi) cudaFree(d_multi);
      if (d_logits) cudaFree(d_logits);
      return s;
    };
    if (cudaMalloc(reinterpret_cast<void**>(&d_ids),
                   static_cast<size_t>(chunk) * sizeof(int32_t)) != cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_pos),
                   static_cast<size_t>(chunk) * sizeof(int)) != cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_sample),
                   static_cast<size_t>(chunk) * hs * sizeof(uint16_t)) !=
            cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_multi),
                   static_cast<size_t>(chunk) * hc_dim * sizeof(uint16_t)) !=
            cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_logits),
                   static_cast<size_t>(last_c) * vocab * sizeof(uint16_t)) !=
            cudaSuccess)
      return cleanup(Status::Fail("MtpDraftExtend: chunked cudaMalloc"));
    if (m.max_seq > 1) {
      if (cudaMalloc(reinterpret_cast<void**>(&d_seqid),
                     static_cast<size_t>(chunk) * sizeof(int)) != cudaSuccess)
        return cleanup(
            Status::Fail("MtpDraftExtend: chunked cudaMalloc d_seqid"));
    }
    Status s;
    for (int base = 0; base < T; base += chunk) {
      diagnostic::Scope chunk_scope("mtp.prompt_extend.chunk", base,
                                    std::min(chunk, T - base));
      s = ObserveExecution("mtp.api.metadata_begin", [&] {
        return upload.Begin(stream);
      });
      if (!s.ok()) return cleanup(s);
      const int c = (T - base < chunk) ? (T - base) : chunk;
      const bool last = (base + c >= T);
      if (!(s = upload.Copy(d_ids, shifted_ids + base,
                            static_cast<size_t>(c) * sizeof(int32_t)))
               .ok())
        return cleanup(s);
      if (!(s = upload.Copy(d_pos, positions + base,
                            static_cast<size_t>(c) * sizeof(int)))
               .ok())
        return cleanup(s);
      if (m.max_seq > 1) {
        std::vector<int> seqid(c, seq_id);
        if (!(s = upload.Copy(d_seqid, seqid.data(), c * sizeof(int))).ok())
          return cleanup(s);
      }
      s = ObserveExecution("mtp.api.metadata_finish", [&] {
        return upload.Finish();
      });
      if (!s.ok()) return cleanup(s);
      // main_trunk is the FULL-prompt trunk [T, hc_dim]; this chunk reads its
      // [base, base+c) rows.
      s = MtpForward(m, d_ids, d_pos,
                     main_trunk + static_cast<size_t>(base) * hc_dim, d_sample,
                     d_multi, last ? d_logits : nullptr, c, stream, d_seqid,
                     last);
      if (!s.ok()) return cleanup(s);
    }
    // out_g = last row's multi_hidden (draft trunk g_{last}).
    if (ObserveExecution("mtp.api.prompt_out_g_d2d", [&] {
          return cudaMemcpy(
              out_g, d_multi + static_cast<size_t>(last_c - 1) * hc_dim,
              static_cast<size_t>(hc_dim) * sizeof(uint16_t),
              cudaMemcpyDeviceToDevice);
        }) != cudaSuccess)
      return cleanup(Status::Fail("MtpDraftExtend: D2D out_g"));
    // out_d0 = argmax of the last chunk's final logits row (first draft token).
    {
      Status s2 = DraftArgmax(
          m, d_logits + static_cast<size_t>(last_c - 1) * vocab, 1, d_ids,
          stream);
      if (!s2.ok()) return cleanup(s2);
      if (ObserveExecution("mtp.api.prompt_argmax_d2h", [&] {
            return cudaMemcpy(out_d0, d_ids, sizeof(int32_t),
                              cudaMemcpyDeviceToHost);
          }) != cudaSuccess)
        return cleanup(Status::Fail("MtpDraftExtend: D2H argmax"));
    }
    return cleanup(Status());
  }

  // Use the persistent per-step scratch when T fits (the common internal-extend
  // case, T = a+1 <= k+1); otherwise fall back to per-call allocation (the
  // initial prompt extend, T = P, which is large and one-shot).
  const bool use_scratch = m.k_max > 0 && T <= m.k_max;
  int32_t* d_ids = use_scratch ? m.d_ids_scratch : nullptr;
  int* d_pos = use_scratch ? m.d_pos_scratch : nullptr;
  uint16_t* d_sample = use_scratch ? m.d_spec_sample : nullptr;  // [T, hs]
  uint16_t* d_multi = use_scratch ? m.d_spec_multi : nullptr;    // [T, hc_dim]
  uint16_t* d_logits = use_scratch ? m.d_spec_logits : nullptr;  // [T, vocab]
  // Per-token d_seq_id (constant = seq_id) for the pooled draft KV. Only
  // needed when the draft state is pooled (max_seq > 1); the single-seq
  // layout (max_seq == 1) passes nullptr to MtpForward (bit-identical).
  int* d_seqid = nullptr;
  auto cleanup = [&](Status s) -> Status {
    diagnostic::Scope cleanup_scope("mtp.prompt_extend.cleanup");
    if (d_seqid) cudaFree(d_seqid);
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
        (finalize && cudaMalloc(reinterpret_cast<void**>(&d_logits),
                                static_cast<size_t>(T) * vocab *
                                    sizeof(uint16_t)) != cudaSuccess))
      return cleanup(Status::Fail("MtpDraftExtend: cudaMalloc"));
  }

  if (ObserveExecution("mtp.api.prompt_ids_h2d", [&] {
        return cudaMemcpy(d_ids, shifted_ids,
                          static_cast<size_t>(T) * sizeof(int32_t),
                          cudaMemcpyHostToDevice);
      }) != cudaSuccess)
    return cleanup(Status::Fail("MtpDraftExtend: H2D ids"));
  if (ObserveExecution("mtp.api.prompt_positions_h2d", [&] {
        return cudaMemcpy(d_pos, positions,
                          static_cast<size_t>(T) * sizeof(int),
                          cudaMemcpyHostToDevice);
      }) != cudaSuccess)
    return cleanup(Status::Fail("MtpDraftExtend: H2D positions"));
  if (m.max_seq > 1) {
    if (cudaMalloc(reinterpret_cast<void**>(&d_seqid),
                   static_cast<size_t>(T) * sizeof(int)) != cudaSuccess)
      return cleanup(Status::Fail("MtpDraftExtend: cudaMalloc d_seqid"));
    std::vector<int> seqid(T, seq_id);
    if (cudaMemcpy(d_seqid, seqid.data(), T * sizeof(int),
                   cudaMemcpyHostToDevice) != cudaSuccess)
      return cleanup(Status::Fail("MtpDraftExtend: H2D d_seqid"));
  }

  Status s = MtpForward(m, d_ids, d_pos, main_trunk, d_sample, d_multi,
                        d_logits, T, stream, d_seqid, finalize);
  if (!s.ok() || !finalize) return cleanup(s);

  // out_g = last row's multi_hidden (draft trunk g_{last}).
  if (ObserveExecution("mtp.api.prompt_out_g_d2d", [&] {
        return cudaMemcpy(out_g, d_multi + static_cast<size_t>(T - 1) * hc_dim,
                          static_cast<size_t>(hc_dim) * sizeof(uint16_t),
                          cudaMemcpyDeviceToDevice);
      }) != cudaSuccess)
    return cleanup(Status::Fail("MtpDraftExtend: D2D out_g"));

  // out_d0 = argmax of the last row's logits (first draft token), on the GPU
  // (1 kernel + 4-byte D2H). d_ids is reused as the 1-int output (the input
  // ids are no longer needed after MtpForward).
  {
    Status s = DraftArgmax(m, d_logits + static_cast<size_t>(T - 1) * vocab,
                           1, d_ids, stream);
    if (!s.ok()) return cleanup(s);
    if (ObserveExecution("mtp.api.prompt_argmax_d2h", [&] {
          return cudaMemcpy(out_d0, d_ids, sizeof(int32_t),
                            cudaMemcpyDeviceToHost);
        }) != cudaSuccess)
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
  const int capacity = std::min(main.cfg.Capacity(0), mtp.cfg.Capacity(0));
  if (P < 0 || k >= capacity || P > capacity - k - 1)
    return Status::Fail("MtpSpeculativeStep: slot capacity exceeded");
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
  // GPU-side phase spans (cudaEvent): wall-clock timing also includes host
  // sync stalls; the event deltas show how much of each phase the GPU
  // actually spent (busy + stream idle) between the recorded markers.
  cudaEvent_t ev_d0 = nullptr, ev_d1 = nullptr, ev_d2 = nullptr, ev_d3 = nullptr;
  if (getenv("Q4T_MTP_TIMING") != nullptr) {
    if (cudaEventCreate(&ev_d0) != cudaSuccess ||
        cudaEventCreate(&ev_d1) != cudaSuccess ||
        cudaEventCreate(&ev_d2) != cudaSuccess ||
        cudaEventCreate(&ev_d3) != cudaSuccess) {
      return Status::Fail("MtpSpeculativeStep: cudaEventCreate");
    }
  }
  auto cleanup = [&](Status s) -> Status {
    if (!use_scratch) {
      if (d_vlogits) cudaFree(d_vlogits);
      if (d_vtrunk) cudaFree(d_vtrunk);
      if (d_g) cudaFree(d_g);
    }
    if (ev_d0) cudaEventDestroy(ev_d0);
    if (ev_d1) cudaEventDestroy(ev_d1);
    if (ev_d2) cudaEventDestroy(ev_d2);
    if (ev_d3) cudaEventDestroy(ev_d3);
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
  if (timing) cudaEventRecord(ev_d0, stream);
  // 1. Draft loop: drafts[0] = d0 (given); generate drafts[1..k-1].
  // Device-side loop: the draft tokens stay on the device. d_drafts[j] is
  // the argmax output of iteration j and is fed directly as iteration
  // j+1's input, so the GPU pipeline never drains for a host round trip
  // (the old per-iteration D2H argmax + H2D id fully serialized each draft
  // step on a host sync). One D2H of all k-1 generated drafts happens after
  // the loop (the accept loop / extend / history need them on the host).
  std::vector<int32_t> drafts(k);
  drafts[0] = d0;
  if (cudaMemcpy(d_g, g_in, static_cast<size_t>(hc_dim) * sizeof(uint16_t),
                 cudaMemcpyDeviceToDevice) != cudaSuccess)
    return cleanup(Status::Fail("MtpSpeculativeStep: D2D g_in"));
  // d_drafts: device array holding drafts[0..k-1] (k ints; d_ids_scratch is
  // sized k_max >= k). d_pos: the k-1 draft positions (d_pos_scratch is
  // sized k_max >= k). Both are reused by the verify argmax below only
  // AFTER the final D2H, so the reuse is safe.
  int32_t* d_drafts = use_scratch ? mtp.d_ids_scratch : nullptr;
  int* d_pos = use_scratch ? mtp.d_pos_scratch : nullptr;
  if (!use_scratch) {
    if (cudaMalloc(reinterpret_cast<void**>(&d_drafts),
                   static_cast<size_t>(k) * sizeof(int32_t)) != cudaSuccess ||
        cudaMalloc(reinterpret_cast<void**>(&d_pos),
                   static_cast<size_t>(k) * sizeof(int)) != cudaSuccess) {
      if (d_drafts) cudaFree(d_drafts);
      if (d_pos) cudaFree(d_pos);
      return cleanup(
          Status::Fail("MtpSpeculativeStep: cudaMalloc draft"));
    }
  }
  if (cudaMemcpy(d_drafts, &d0, sizeof(int32_t), cudaMemcpyHostToDevice) !=
      cudaSuccess)
    return cleanup(Status::Fail("MtpSpeculativeStep: H2D draft0"));
  std::vector<int> pos_h(k > 1 ? k - 1 : 1);
  for (int j = 1; j < k; ++j) pos_h[j - 1] = P + j - 1;  // EAGLE shift.
  if (k > 1 &&
      cudaMemcpy(d_pos, pos_h.data(),
                 static_cast<size_t>(k - 1) * sizeof(int),
                 cudaMemcpyHostToDevice) != cudaSuccess)
    return cleanup(Status::Fail("MtpSpeculativeStep: H2D draft positions"));
  for (int j = 1; j < k; ++j) {
    Status s = MtpForward(mtp, d_drafts + (j - 1), d_pos + (j - 1), d_g,
                          mtp.d_sample, mtp.d_trunk, mtp.d_logits, 1, stream);
    if (!s.ok()) return cleanup(s);
    // GPU argmax (1 kernel) writes drafts[j] into the device array; the
    // next iteration reads it directly (no host round trip).
    s = DraftArgmax(mtp, mtp.d_logits, 1, d_drafts + j, stream);
    if (!s.ok()) return cleanup(s);
    if (cudaMemcpy(d_g, mtp.d_trunk,
                   static_cast<size_t>(hc_dim) * sizeof(uint16_t),
                   cudaMemcpyDeviceToDevice) != cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStep: D2D roll g"));
  }
  // Single D2H for all generated drafts.
  if (k > 1 &&
      cudaMemcpy(drafts.data() + 1, d_drafts + 1,
                 static_cast<size_t>(k - 1) * sizeof(int32_t),
                 cudaMemcpyDeviceToHost) != cudaSuccess)
    return cleanup(Status::Fail("MtpSpeculativeStep: D2H drafts"));
  if (!use_scratch) {
    cudaFree(d_drafts);
    cudaFree(d_pos);
  }

  // Test-only: Q4T_MTP_TEST_DRAFTS="t1,t2,..." overrides the generated
  // draft tokens drafts[1..k-1] (drafts[0] stays d0). The draft loop still
  // ran, so all draft-KV side effects are unchanged; only the token values
  // fed to the verify batch are replaced. Default off (no behavior change);
  // fails loudly if set but malformed (exactly k-1 tokens required).
  if (const char* env_drafts = std::getenv("Q4T_MTP_TEST_DRAFTS")) {
    std::vector<int32_t> forced;
    const char* rest = env_drafts;
    while (*rest) {
      char* end = nullptr;
      const long v = std::strtol(rest, &end, 10);
      if (end == rest) break;
      forced.push_back(static_cast<int32_t>(v));
      rest = (*end == ',') ? end + 1 : (*end == '\0' ? end : nullptr);
    }
    if (forced.size() != static_cast<size_t>(k - 1))
      return cleanup(Status::Fail(
          "Q4T_MTP_TEST_DRAFTS: expected exactly k-1 comma-separated "
          "tokens"));
    for (int j = 1; j < k; ++j) drafts[j] = forced[j - 1];
  }

  if (timing) cudaEventRecord(ev_d1, stream);
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
        /*save_checkpoints=*/true, seq->seq_id);
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
  if (timing) cudaEventRecord(ev_d2, stream);
  const auto t_verify1 = std::chrono::steady_clock::now();
  std::vector<int32_t> m_argmax(k + 1);
  if (cudaMemcpy(m_argmax.data(), d_argmax,
                 static_cast<size_t>(k + 1) * sizeof(int32_t),
                 cudaMemcpyDeviceToHost) != cudaSuccess)
    return cleanup(Status::Fail("MtpSpeculativeStep: D2H verify argmax"));
  if (!use_scratch) cudaFree(d_argmax);
  // Diagnostic (Q4T_MTP_GAP_DEBUG): top-2 gap per verify row, logged with the
  // accept decision. Pure observation; does not affect a/correction.
  if (const char* gap_env = std::getenv("Q4T_MTP_GAP_DEBUG")) {
    float* d_gap = nullptr;
    if (cudaMalloc(reinterpret_cast<void**>(&d_gap),
                   static_cast<size_t>(k + 1) * 3 * sizeof(float)) ==
        cudaSuccess) {
      if (Top2GapBf16Rows(d_vlogits, k + 1, vocab, d_gap, stream).ok()) {
        float gap_h[(k + 1) * 3];
        if (cudaMemcpy(gap_h, d_gap, static_cast<size_t>(k + 1) * 3 * sizeof(float),
                       cudaMemcpyDeviceToHost) == cudaSuccess) {
          std::fprintf(stderr,
                       "[mtp-gap] P=%d a_pending: ", P);
          for (int i = 0; i < k; ++i) {
            const float v1 = gap_h[i * 3 + 0];
            const float v2 = gap_h[i * 3 + 1];
            const int i1 = static_cast<int>(gap_h[i * 3 + 2]);
            std::fprintf(stderr,
                         "i%d{draft=%d main=%d gap=%.4f v1=%.3f v2=%.3f top1=%d}%s",
                         i, drafts[i], m_argmax[i], v1 - v2, v1, v2, i1,
                         i + 1 < k ? " " : "\n");
          }
        }
      }
      cudaFree(d_gap);
    }
  }
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
    Status s = model::ModelRestoreCheckpoint(main, a, stream, seq->seq_id);
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
  if (timing) cudaEventRecord(ev_d3, stream);
  if (timing) {
    const auto t_end = std::chrono::steady_clock::now();
    auto ms = [](auto lo, auto hi) {
      return std::chrono::duration<double, std::milli>(hi - lo).count();
    };
    std::fprintf(stderr,
                 "[mtp-timing] draft=%.1f verify=%.1f extend+accept=%.1f ms "
                 "(a=%d k=%d)",
                 ms(t_draft0, t_draft1), ms(t_draft1, t_verify1),
                 ms(t_verify1, t_end), a, k);
    if (cudaEventSynchronize(ev_d3) == cudaSuccess) {
      float gd = 0.f, gv = 0.f, ge = 0.f;
      cudaEventElapsedTime(&gd, ev_d0, ev_d1);
      cudaEventElapsedTime(&gv, ev_d1, ev_d2);
      cudaEventElapsedTime(&ge, ev_d2, ev_d3);
      std::fprintf(stderr,
                   " gpu[ draft=%.1f verify=%.1f extend=%.1f ]\n", gd, gv, ge);
    }
    std::fprintf(stderr, "\n");
    cudaEventDestroy(ev_d0);
    cudaEventDestroy(ev_d1);
    cudaEventDestroy(ev_d2);
    cudaEventDestroy(ev_d3);
    ev_d0 = ev_d1 = ev_d2 = ev_d3 = nullptr;
  }
  return cleanup(Status());
}


// ---------------------------------------------------------------------------
// Multi-sequence speculative step (Phase 2 Stage 2b). See mtp.h for the full
// contract. All three segments are batched:
//   1. Batched draft loop: each step packs B sequences' draft[j-1] into ONE
//      MtpForward (d_seq_id, T=B, one token per sequence — Stage 1's multi-seq
//      path); the rolling trunk is per-sequence (d_ms_g_pool [max_seq, hc_dim]).
//   2. Multi-seq verify: ModelVerifyMulti (Stage 2a) packs B sequences' (k+1)
//      tokens into ONE main forward; per-seq per-token checkpoints roll back
//      each sequence to its own accepted prefix.
//   3. Batched extend: the accepted prefixes [d_0..d_{a_b-1}, next_b] of all
//      sequences are packed contiguously (T = sum(a_b+1)) into ONE MtpForward;
//      the hidden gather (d_ms_gather) collects each sequence's verify-trunk
//      rows (h_{P_b}..h_{P_b+a_b}) contiguous.
// ---------------------------------------------------------------------------
namespace {
// Gather the per-sequence verify-trunk rows into a contiguous [T, hc_dim]
// buffer for the batched extend MtpForward. Row r of `out` = src[off[r] +
// r] (off[b] = b*(k+1), the sequence-major verify-trunk base). One block per
// row; each block copies one hc_dim row.
__global__ void GatherTrunkRowsKernel(const uint16_t* __restrict__ src,
                                      const int* __restrict__ off,
                                      uint16_t* __restrict__ out, int hc_dim,
                                      int T) {
  const int r = blockIdx.x;
  const uint16_t* s = src + static_cast<size_t>(off[r]) * hc_dim;
  uint16_t* o = out + static_cast<size_t>(r) * hc_dim;
  for (int i = threadIdx.x; i < hc_dim; i += blockDim.x) o[i] = s[i];
}
// Plan D: gather the previous draft column for the batched draft loop.
// out[b] = src[seq_of[b] * k_stride + col]. One thread per sequence (B small).
// Keeps draft tokens GPU-resident so the loop needs no host sync per step.
__global__ void GatherDraftKernel(const int32_t* __restrict__ src, int k_stride,
                                  int col, int32_t* __restrict__ out,
                                  const int* __restrict__ seq_of, int B) {
  const int b = blockIdx.x;
  if (b >= B) return;
  out[b] = src[static_cast<size_t>(seq_of[b]) * k_stride + col];
}
// Plan D: scatter the new draft column (the argmax result) back to the
// persistent draft matrix. dst[seq_of[b] * k_stride + col] = in[b].
__global__ void ScatterDraftKernel(const int32_t* __restrict__ in,
                                   int32_t* __restrict__ dst, int k_stride,
                                   int col, const int* __restrict__ seq_of,
                                   int B) {
  const int b = blockIdx.x;
  if (b >= B) return;
  dst[static_cast<size_t>(seq_of[b]) * k_stride + col] = in[b];
}
// Plan D: extract the [B, k] draft sub-matrix to host after the loop. The
// matrix is indexed by seq_id (seq_of[b]), not batch position b, so the rows
// are NOT contiguous — gather them: out[b*k_cols + c] =
// src[seq_of[b]*k_stride + c]. One block per sequence.
__global__ void GatherDraftMatrixKernel(const int32_t* __restrict__ src,
                                        int k_stride, int k_cols,
                                        int32_t* __restrict__ out,
                                        const int* __restrict__ seq_of, int B) {
  const int b = blockIdx.x;
  if (b >= B) return;
  const int32_t* row = src + static_cast<size_t>(seq_of[b]) * k_stride;
  int32_t* orow = out + static_cast<size_t>(b) * k_cols;
  for (int c = threadIdx.x; c < k_cols; c += blockDim.x) orow[c] = row[c];
}
}  // namespace

Status MtpSpeculativeStepMulti(const model::Model& main, const MtpModel& mtp,
                               const model::ModelSequence* const* seqs,
                               const int32_t* b_tok, const int32_t* d0,
                               const uint16_t* const* g_in, int B, int k,
                               int32_t* accepted_tokens, int* accepted_count,
                               int32_t* next_b, int32_t* next_d0,
                               uint16_t** next_g, cudaStream_t stream,
                               std::span<const int32_t> stop_tokens,
                               std::span<const int32_t> suffix_draft,
                               std::span<const SuffixProposal> batch_suffix,
                               std::span<const uint8_t> complete_terminal) {
  if (!seqs || !b_tok || !d0 || !g_in || !accepted_tokens || !accepted_count ||
      !next_b || !next_d0 || !next_g)
    return Status::Fail("MtpSpeculativeStepMulti: null arg");
  if (B <= 0 || B > mtp.max_seq)
    return Status::Fail("MtpSpeculativeStepMulti: B out of range");
  if (!complete_terminal.empty() &&
      complete_terminal.size() != static_cast<size_t>(B))
    return Status::Fail("MtpSpeculativeStepMulti: terminal flags size");
  const auto invalidate_outputs = [&] {
    for (int b = 0; b < B; ++b) {
      accepted_count[b] = 0;
      next_b[b] = -1;
      next_d0[b] = -1;
    }
  };
  invalidate_outputs();
  if (k <= 0 || mtp.k_max <= 0 || k >= mtp.k_max)
    return Status::Fail("MtpSpeculativeStepMulti: scratch not reserved for k");
  const int vocab = mtp.cfg.vocab;
  const int hc_dim = mtp.hc_dim();
  if (!batch_suffix.empty()) {
    if (!suffix_draft.empty() || k != 3 ||
        batch_suffix.size() != static_cast<size_t>(B))
      return Status::Fail("MtpSpeculativeStepMulti: invalid suffix batch");
    for (int b = 0; b < B; ++b) {
      const auto& proposal = batch_suffix[b];
      if (proposal.ready &&
          (proposal.tokens[0] != d0[b] ||
           std::any_of(proposal.tokens.begin(), proposal.tokens.end(),
                       [vocab](int32_t t) { return t < 0 || t >= vocab; })))
        return Status::Fail("MtpSpeculativeStepMulti: invalid suffix row");
    }
  }
  if (!suffix_draft.empty()) {
    if (B != 1 || k != 3 || suffix_draft.size() != 3 ||
        suffix_draft[0] != d0[0] ||
        std::any_of(
            suffix_draft.begin(), suffix_draft.end(),
            [vocab](int32_t token) { return token < 0 || token >= vocab; }))
      return Status::Fail("MtpSpeculativeStepMulti: invalid fixed-k3 suffix");
  }
  const auto is_stop = [stop_tokens](int32_t token) {
    return std::find(stop_tokens.begin(), stop_tokens.end(), token) !=
           stop_tokens.end();
  };
  for (int b = 0; b < B; ++b) {
    if (!seqs[b] || !g_in[b] || !next_g[b])
      return Status::Fail("MtpSpeculativeStepMulti: null sequence buffer");
    if (b_tok[b] < 0 || b_tok[b] >= vocab || is_stop(b_tok[b]) || d0[b] < 0 ||
        d0[b] >= vocab)
      return Status::Fail("MtpSpeculativeStepMulti: invalid pending token");
    if (seqs[b]->stage != model::ModelSequence::Stage::kDecode)
      return Status::Fail("MtpSpeculativeStepMulti: seq not in decode stage");
    if (seqs[b]->seq_id < 0 || seqs[b]->seq_id >= mtp.max_seq)
      return Status::Fail("MtpSpeculativeStepMulti: invalid sequence slot");
    const int capacity = std::min(main.cfg.Capacity(seqs[b]->seq_id),
                                  mtp.cfg.Capacity(seqs[b]->seq_id));
    if (seqs[b]->position < 0 || k >= capacity ||
        seqs[b]->position > capacity - k - 1)
      return Status::Fail("MtpSpeculativeStepMulti: slot capacity exceeded");
    for (int prior = 0; prior < b; ++prior)
      if (seqs[prior]->seq_id == seqs[b]->seq_id)
        return Status::Fail("MtpSpeculativeStepMulti: duplicate sequence slot");
  }

  diagnostic::Batch execution("mtp.multi", seqs[0]->seq_id,
                              seqs[0]->position, B * (k + 1));
  diagnostic::Scope step_scope("mtp.step", seqs[0]->position, B * (k + 1));

  // Persistent multi-seq scratch (reserved even for max_seq == 1).
  // All buffers are sized for the worst case B = max_seq, T = k_max, so the
  // extend gather needs no cudaMalloc/cudaFree. Other diagnostic/forward
  // allocations are outside this scratch contract.
  if (mtp.d_ms_ids == nullptr || mtp.k_max < k + 1)
    return Status::Fail("MtpSpeculativeStepMulti: scratch not reserved "
                        "(call MtpReserveScratch with k_max >= k+1)");
  int32_t* d_ids = mtp.d_ms_ids;      // [B] draft-loop packed ids
  int* d_pos = mtp.d_ms_pos;          // [B] draft-loop positions
  uint16_t* d_sample = mtp.d_ms_sample;  // [B, hs]
  uint16_t* d_multi = mtp.d_ms_multi;    // [B, hc_dim]
  uint16_t* d_g_pool = mtp.d_ms_g_pool;  // [max_seq, hc_dim] rolling trunk
  uint16_t* d_gather = mtp.d_ms_gather;  // [B*(k+1), hc_dim] extend hidden gather
  uint16_t* d_vlogits = mtp.d_ms_vlogits;  // [B*(k+1), vocab] verify logits
  uint16_t* d_vtrunk = mtp.d_ms_vtrunk;    // [B*(k+1), hc_dim] verify trunk
  uint16_t* d_ext_logits = mtp.d_ms_ext_logits;  // [T_ext, vocab]
  uint16_t* d_ext_multi = mtp.d_ms_ext_multi;    // [T_ext, hc_dim]
  uint16_t* d_ext_sample = mtp.d_ms_ext_sample;  // [T_ext, hs]
  int32_t* d_ext_ids = mtp.d_ms_ext_ids;         // [T_ext]
  int* d_ext_pos = mtp.d_ms_ext_pos;             // [T_ext]
  const size_t Tvm = static_cast<size_t>(B) * (k + 1);
  // GPU-side phase spans (cudaEvent): wall-clock timing also includes host
  // sync stalls; the event deltas show how much of each phase the GPU
  // actually spent (busy + stream idle) between the recorded markers.
  cudaEvent_t ev_d0 = nullptr, ev_d1 = nullptr, ev_d2 = nullptr,
      ev_d3 = nullptr;
  const bool timing = getenv("Q4T_MTP_TIMING") != nullptr;
  if (timing) {
    if (cudaEventCreate(&ev_d0) != cudaSuccess ||
        cudaEventCreate(&ev_d1) != cudaSuccess ||
        cudaEventCreate(&ev_d2) != cudaSuccess ||
        cudaEventCreate(&ev_d3) != cudaSuccess) {
      return Status::Fail("MtpSpeculativeStepMulti: cudaEventCreate");
    }
  }
  auto cleanup = [&](Status st) -> Status {
    // Complete before step-owned upload sources are destroyed, on every exit.
    const cudaError_t error = ObserveExecution("mtp.wait.cleanup_sync", [&] {
      return cudaStreamSynchronize(stream);
    });
    if (error != cudaSuccess)
      st = Status::Fail(std::string("MtpSpeculativeStepMulti: completion: ") +
                        cudaGetErrorString(error));
    if (!st.ok()) invalidate_outputs();
    if (ev_d0) cudaEventDestroy(ev_d0);
    if (ev_d1) cudaEventDestroy(ev_d1);
    if (ev_d2) cudaEventDestroy(ev_d2);
    if (ev_d3) cudaEventDestroy(ev_d3);
    return st;
  };
  // Phase timing (Q4T_MTP_TIMING): draft / verify / extend+accept, same
  // format as the single-seq MtpSpeculativeStep. Wall-clock around each
  // phase's GPU work (the D2H at the end of each phase forces the sync).
  const bool dbg = getenv("Q4T_MTP_DEBUG") != nullptr;
  const auto t_draft0 = std::chrono::steady_clock::now();
  if (timing) cudaEventRecord(ev_d0, stream);
  auto t_draft1 = t_draft0;
  auto t_verify1 = t_draft0;

  std::vector<int> P(B);
  std::vector<int> seq_of(B);  // pooled-state slice index of sequence b
  for (int b = 0; b < B; ++b) {
    P[b] = seqs[b]->position;
    seq_of[b] = seqs[b]->seq_id;
  }
  // 1. Batched draft loop (Plan D: GPU-resident draft tokens). Lower-level
  //    MoE dispatch still synchronizes host counts. Seed the rolling trunk
  //    and first draft column (drafts[0] = d0), then generate drafts[1..k-1]
  //    for all sequences. Each step: GatherDraftKernel pulls the previous
  //    column (device), MtpForward runs the draft model, ArgmaxBf16Rows
  //    produces the new column (device), ScatterDraftKernel writes it back
  //    (device). Token IDs remain on GPU. One D2H of the [B, k] draft matrix
  //    happens after the loop (the verify/extend packing needs it on host).
  if (mtp.d_ms_drafts == nullptr)
    return cleanup(
        Status::Fail("MtpSpeculativeStepMulti: draft matrix not reserved"));
  const int k_stride = mtp.k_max;       // column stride of d_ms_drafts
  int32_t* d_drafts = mtp.d_ms_drafts;  // [max_seq, k_max]
  std::vector<int32_t> drafts_host(static_cast<size_t>(B) * k);
  // Keep every async upload source alive through cleanup's completion.
  // Packed row and pooled slot are different coordinates: MtpForward reads
  // hidden_states[row], while KV/indexer and the draft matrix use seq_id.
  std::vector<int> neural_rows, neural_slots, h_pos;
  std::vector<int32_t> seeds, neural_drafts;
  for (int b = 0; b < B; ++b) {
    const auto proposal = !suffix_draft.empty()
        ? suffix_draft
        : (!batch_suffix.empty() && batch_suffix[b].ready
               ? std::span<const int32_t>(batch_suffix[b].tokens)
               : std::span<const int32_t>());
    if (!proposal.empty()) {
      // Global IDs bypass DraftArgmax's subset mapping entirely.
      std::copy(proposal.begin(), proposal.end(), drafts_host.begin() + b * k);
    } else {
      neural_rows.push_back(b);
      neural_slots.push_back(seq_of[b]);
      seeds.push_back(d0[b]);
    }
  }
  const int N = static_cast<int>(neural_rows.size());
  if (N > 0) {
    if (cudaMemcpyAsync(mtp.d_ms_ext_seq, neural_slots.data(), N * sizeof(int),
                        cudaMemcpyHostToDevice, stream) != cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStepMulti: H2D draft seq"));
    for (int row = 0; row < N; ++row) {
      if (ObserveExecution("mtp.api.seed_g_d2d", [&] {
            return cudaMemcpy(
                d_g_pool + static_cast<size_t>(row) * hc_dim,
                g_in[neural_rows[row]], static_cast<size_t>(hc_dim) * 2,
                cudaMemcpyDeviceToDevice);
          }) != cudaSuccess)
        return cleanup(Status::Fail("MtpSpeculativeStepMulti: D2D g_in"));
    }
    if (cudaMemcpyAsync(d_ids, seeds.data(), N * sizeof(int32_t),
                        cudaMemcpyHostToDevice, stream) != cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStepMulti: H2D draft seed"));
    ScatterDraftKernel<<<N, 32, 0, stream>>>(d_ids, d_drafts, k_stride, 0,
                                             mtp.d_ms_ext_seq, N);
    if (cudaGetLastError() != cudaSuccess)
      return cleanup(
          Status::Fail("MtpSpeculativeStepMulti: scatter draft seed"));
    h_pos.resize(static_cast<size_t>(N) * k);
    for (int j = 1; j < k; ++j)
      for (int row = 0; row < N; ++row)
        h_pos[static_cast<size_t>(j) * N + row] =
            P[neural_rows[row]] + j - 1;
    for (int j = 1; j < k; ++j) {
      diagnostic::Scope token_scope("mtp.draft_token",
                                     P[neural_rows[0]] + j - 1, N);
      GatherDraftKernel<<<N, 32, 0, stream>>>(d_drafts, k_stride, j - 1, d_ids,
                                              mtp.d_ms_ext_seq, N);
      if (cudaGetLastError() != cudaSuccess)
        return cleanup(Status::Fail("MtpSpeculativeStepMulti: gather draft"));
      if (cudaMemcpyAsync(d_pos, h_pos.data() + static_cast<size_t>(j) * N,
                          N * sizeof(int), cudaMemcpyHostToDevice,
                          stream) != cudaSuccess)
        return cleanup(Status::Fail("MtpSpeculativeStepMulti: H2D draft pos"));
      const auto pos_begin = h_pos.begin() + static_cast<size_t>(j) * N;
      const int max_position = *std::max_element(pos_begin, pos_begin + N);
      Status s = MtpForward(mtp, d_ids, d_pos, d_g_pool, d_sample, d_multi,
                            d_vlogits, N, stream, mtp.d_ms_ext_seq, true,
                            max_position);
      if (!s.ok()) return cleanup(s);
      s = DraftArgmax(mtp, d_vlogits, N, d_ext_ids, stream);
      if (!s.ok()) return cleanup(s);
      ScatterDraftKernel<<<N, 32, 0, stream>>>(d_ext_ids, d_drafts, k_stride, j,
                                               mtp.d_ms_ext_seq, N);
      if (cudaGetLastError() != cudaSuccess)
        return cleanup(Status::Fail("MtpSpeculativeStepMulti: scatter draft"));
      // Trunks remain packed in neural-row order, never indexed by slot.
      if (cudaMemcpyAsync(d_g_pool, d_multi, size_t(N) * hc_dim * 2,
                          cudaMemcpyDeviceToDevice, stream) != cudaSuccess)
        return cleanup(Status::Fail("MtpSpeculativeStepMulti: roll g"));
    }
    GatherDraftMatrixKernel<<<N, 32, 0, stream>>>(
        d_drafts, k_stride, k, d_ext_ids, mtp.d_ms_ext_seq, N);
    if (cudaGetLastError() != cudaSuccess)
      return cleanup(
          Status::Fail("MtpSpeculativeStepMulti: gather draft matrix"));
    neural_drafts.resize(static_cast<size_t>(N) * k);
    if (ObserveExecution("mtp.api.draft_matrix_d2h", [&] {
          return cudaMemcpy(neural_drafts.data(), d_ext_ids,
                            neural_drafts.size() * sizeof(int32_t),
                            cudaMemcpyDeviceToHost);
        }) != cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStepMulti: D2H draft matrix"));
    for (int row = 0; row < N; ++row)
      std::copy_n(neural_drafts.begin() + row * k, k,
                  drafts_host.begin() + neural_rows[row] * k);
  }
  if (timing) cudaEventRecord(ev_d1, stream);
  t_draft1 = std::chrono::steady_clock::now();

  // 2. Multi-seq verify: pack [b_b, d_0..d_{k-1}] per sequence (k+1 tokens,
  //    sequence-major) and run ONE main forward (ModelVerifyMulti).
  std::vector<int32_t> vtok(Tvm);
  std::vector<int> vbase(B), vseq(B);
  for (int b = 0; b < B; ++b) {
    vtok[static_cast<size_t>(b) * (k + 1)] = b_tok[b];
    for (int i = 0; i < k; ++i)
      vtok[static_cast<size_t>(b) * (k + 1) + 1 + i] =
          drafts_host[static_cast<size_t>(b) * k + i];
    vbase[b] = P[b];
    vseq[b] = seq_of[b];
  }
  // PLE n-gram history: per sequence, the tokens at positions < P_b (the
  // prompt, = seqs[b].history before this step advances it). Row b holds
  // positions 0..P_b-1; the verify kernel reads history[b*hist_len + src] for
  // src < P_b, so hist_len must be >= max P_b (pad shorter prompts with EOS).
  int hist_len = 0;
  for (int b = 0; b < B; ++b)
    hist_len = std::max(hist_len, static_cast<int>(seqs[b]->history.size()));
  std::vector<int32_t> vhist(static_cast<size_t>(B) * hist_len,
                             main.cfg.eos_token_id);
  for (int b = 0; b < B; ++b) {
    const auto& h = seqs[b]->history;
    for (size_t i = 0; i < h.size(); ++i)
      vhist[static_cast<size_t>(b) * hist_len + i] = h[i];
  }
  Status s = ObserveExecution("mtp.verify_forward", [&] {
    return model::ModelVerifyMulti(main, vtok.data(), vbase.data(),
                                   vseq.data(), vhist.data(), hist_len, B,
                                   k + 1, d_vlogits, stream, d_vtrunk);
  });
  if (!s.ok()) return cleanup(s);
  // Argmax over the B*(k+1) rows -> main predictions m_{b,i}.
  std::vector<int32_t> m_argmax(Tvm);
  s = ArgmaxBf16Rows(d_vlogits, static_cast<int>(Tvm), vocab,
                     reinterpret_cast<int32_t*>(d_ext_ids), stream);
  if (!s.ok()) return cleanup(s);
  if (ObserveExecution("mtp.api.verify_argmax_d2h", [&] {
        return cudaMemcpy(m_argmax.data(), d_ext_ids, Tvm * sizeof(int32_t),
                          cudaMemcpyDeviceToHost);
      }) != cudaSuccess)
    return cleanup(Status::Fail("MtpSpeculativeStepMulti: D2H verify argmax"));
  // Diagnostic (Q4T_MTP_GAP_DEBUG): top-2 gap per verify row, logged per
  // sequence with the draft/main tokens. Pure observation; no behavior change.
  if (std::getenv("Q4T_MTP_GAP_DEBUG") != nullptr) {
    float* d_gap = nullptr;
    if (cudaMalloc(reinterpret_cast<void**>(&d_gap),
                   static_cast<size_t>(Tvm) * 3 * sizeof(float)) ==
        cudaSuccess) {
      if (Top2GapBf16Rows(d_vlogits, static_cast<int>(Tvm), vocab, d_gap,
                          stream).ok()) {
        std::vector<float> gap_h(static_cast<size_t>(Tvm) * 3);
        if (cudaMemcpy(gap_h.data(), d_gap,
                       static_cast<size_t>(Tvm) * 3 * sizeof(float),
                       cudaMemcpyDeviceToHost) == cudaSuccess) {
          for (int b = 0; b < B; ++b) {
            std::fprintf(stderr, "[mtp-gap] P=%d: ", P[b]);
            for (int i = 0; i < k; ++i) {
              const size_t row = static_cast<size_t>(b) * (k + 1) + i;
              const float v1 = gap_h[row * 3 + 0];
              const float v2 = gap_h[row * 3 + 1];
              const int i1 = static_cast<int>(gap_h[row * 3 + 2]);
              std::fprintf(
                  stderr,
                  "i%d{d=%d m=%d gap=%.4f v1=%.3f v2=%.3f top1=%d}%s", i,
                  drafts_host[static_cast<size_t>(b) * k + i],
                  m_argmax[static_cast<size_t>(b) * (k + 1) + i], v1 - v2, v1,
                  v2, i1, i + 1 < k ? " " : "\n");
            }
          }
        }
      }
      cudaFree(d_gap);
    }
  }
  if (timing) cudaEventRecord(ev_d2, stream);
  t_verify1 = std::chrono::steady_clock::now();
  // Accept per sequence + roll back the recurrent state to the accepted
  // prefix (checkpoint[a_b]; a_b == k needs no restore).
  std::vector<int> a(B);
  for (int b = 0; b < B; ++b) {
    int acc = 0;
    for (int i = 0; i < k; ++i) {
      const int mi = m_argmax[static_cast<size_t>(b) * (k + 1) + i];
      if (is_stop(mi) || drafts_host[static_cast<size_t>(b) * k + i] != mi)
        break;
      acc = i + 1;
    }
    a[b] = acc;
    if (acc < k) {
      s = ObserveExecution("mtp.restore_checkpoint", [&] {
        return model::ModelRestoreCheckpoint(main, acc, stream, seq_of[b]);
      });
      if (!s.ok()) return cleanup(s);
    }
  }
  if (dbg) {
    for (int b = 0; b < B; ++b) {
      std::fprintf(stderr, "[mtp-debug2] P=%d a=%d k=%d draft=", P[b], a[b], k);
      for (int i = 0; i < k; ++i)
        std::fprintf(stderr, "%d ",
                     drafts_host[static_cast<size_t>(b) * k + i]);
      std::fprintf(stderr, "main=");
      for (int i = 0; i < k; ++i)
        std::fprintf(stderr, "%d ",
                     m_argmax[static_cast<size_t>(b) * (k + 1) + i]);
      std::fprintf(stderr, "corr=%d\n",
                   m_argmax[static_cast<size_t>(b) * (k + 1) + a[b]]);
    }
  }

  // 3. Batched extend: pack each sequence's accepted prefix
  //    [d_0..d_{a_b-1}, next_b] contiguously (T_ext = sum(a_b+1)) and run ONE
  //    MtpForward to rebuild the draft KV + produce next_d0/next_g per seq.
  std::vector<int32_t> corr(B);
  std::vector<bool> terminal(B, false);
  int T_ext = 0;
  for (int b = 0; b < B; ++b) {
    corr[b] = m_argmax[static_cast<size_t>(b) * (k + 1) + a[b]];
    // Cacheable sessions require the shifted draft prefix even at EOS. The
    // target acceptance/output contract is unchanged; only this row's final
    // draft extend, normally elided at termination, is materialized.
    terminal[b] = is_stop(corr[b]) &&
                  (complete_terminal.empty() || !complete_terminal[b]);
    if (!terminal[b]) T_ext += a[b] + 1;
  }
  std::vector<int32_t> ext_ids(T_ext);
  std::vector<int> ext_pos(T_ext);
  std::vector<int> ext_seq(T_ext);
  std::vector<int> gather_off(T_ext);
  int r = 0;
  for (int b = 0; b < B; ++b) {
    if (terminal[b]) continue;
    const int base = static_cast<int>(static_cast<size_t>(b) * (k + 1));
    for (int i = 0; i < a[b]; ++i) {
      ext_ids[r] = drafts_host[static_cast<size_t>(b) * k + i];
      ext_pos[r] = P[b] + i;
      ext_seq[r] = seq_of[b];
      gather_off[r] = base + i;  // verify-trunk row h_{P_b+i}
      ++r;
    }
    ext_ids[r] = corr[b];
    ext_pos[r] = P[b] + a[b];
    ext_seq[r] = seq_of[b];
    gather_off[r] = base + a[b];  // verify-trunk row h_{P_b+a_b}
    ++r;
  }
  if (T_ext > 0) {
    diagnostic::Scope extend_scope("mtp.accepted_extend", P[0], T_ext);
    // Gather the per-seq verify-trunk rows contiguous (the extend hidden).
    {
      if (!mtp.d_ms_gather_off ||
          static_cast<size_t>(T_ext) > mtp.gather_off_capacity)
        return cleanup(Status::Fail(
            "MtpSpeculativeStepMulti: gather offset scratch capacity"));
      // gather_off stays in step scope until cleanup drains on every exit.
      // The same drain makes the device scratch reusable by the next step.
      if (cudaMemcpyAsync(mtp.d_ms_gather_off, gather_off.data(),
                          T_ext * sizeof(int), cudaMemcpyHostToDevice,
                          stream) != cudaSuccess)
        return cleanup(Status::Fail("MtpSpeculativeStepMulti: H2D off"));
      GatherTrunkRowsKernel<<<T_ext, 256, 0, stream>>>(
          d_vtrunk, mtp.d_ms_gather_off, d_gather, hc_dim, T_ext);
      if (cudaGetLastError() != cudaSuccess)
        return cleanup(Status::Fail("MtpSpeculativeStepMulti: gather launch"));
    }
    // Per-token d_seq_id for the packed extend rows (each row's sequence). Use
    // the persistent d_ms_ext_seq buffer (sized max_seq*k_max >= T_ext).
    if (T_ext > mtp.max_seq * mtp.k_max) {
      return cleanup(
          Status::Fail("MtpSpeculativeStepMulti: T_ext exceeds ext_seq cap"));
    }
    if (cudaMemcpyAsync(mtp.d_ms_ext_seq, ext_seq.data(), T_ext * sizeof(int),
                        cudaMemcpyHostToDevice, stream) != cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStepMulti: H2D ext seq"));
    if (cudaMemcpyAsync(d_ext_ids, ext_ids.data(), T_ext * sizeof(int32_t),
                        cudaMemcpyHostToDevice, stream) != cudaSuccess ||
        cudaMemcpyAsync(d_ext_pos, ext_pos.data(), T_ext * sizeof(int),
                        cudaMemcpyHostToDevice, stream) != cudaSuccess)
      return cleanup(Status::Fail("MtpSpeculativeStepMulti: H2D extend"));
    s = MtpForward(mtp, d_ext_ids, d_ext_pos, d_gather, d_ext_sample, d_ext_multi,
                   d_ext_logits, T_ext, stream, mtp.d_ms_ext_seq, true,
                   *std::max_element(ext_pos.begin(), ext_pos.end()));
    if (!s.ok()) return cleanup(s);
    // Per-seq outputs: next_d0 = argmax of the sequence's last extend row;
    // next_g = that row's multi_hidden.
    r = 0;
    for (int b = 0; b < B; ++b) {
      if (terminal[b]) continue;
      const int last = r + a[b];  // the correction row (0-indexed) of seq b.
      s = DraftArgmax(mtp,
          d_ext_logits + static_cast<size_t>(last) * mtp.DraftVocab(),
          1, d_ext_ids, stream);
      if (!s.ok()) return cleanup(s);
      if (ObserveExecution("mtp.api.next_d0_d2h", [&] {
            return cudaMemcpy(&next_d0[b], d_ext_ids, sizeof(int32_t),
                              cudaMemcpyDeviceToHost);
          }) != cudaSuccess)
        return cleanup(Status::Fail("MtpSpeculativeStepMulti: D2H next_d0"));
      if (ObserveExecution("mtp.api.next_g_d2d", [&] {
            return cudaMemcpy(next_g[b],
                              d_ext_multi + static_cast<size_t>(last) * hc_dim,
                              static_cast<size_t>(hc_dim) * 2,
                              cudaMemcpyDeviceToDevice);
          }) != cudaSuccess)
        return cleanup(Status::Fail("MtpSpeculativeStepMulti: D2D next_g"));
      r += a[b] + 1;
    }
  }

  // 4. Emit accepted tokens. The caller advances each sequence's state
  //    machine (position += 1 + a_b; history += [b_b, d_0..d_{a_b-1}]) —
  //    this function does NOT touch seqs[b].position/history, so it can be
  //    called from the scheduler thread (Stage 2c) without racing the
  //    request thread that owns the ModelSequence.
  for (int b = 0; b < B; ++b) {
    accepted_tokens[static_cast<size_t>(b) * (k + 1)] = b_tok[b];
    for (int i = 0; i < a[b]; ++i)
      accepted_tokens[static_cast<size_t>(b) * (k + 1) + 1 + i] =
          drafts_host[static_cast<size_t>(b) * k + i];
    accepted_count[b] = 1 + a[b];
    next_b[b] = corr[b];
  }
  if (timing) cudaEventRecord(ev_d3, stream);
  if (timing) {
    const auto t_end = std::chrono::steady_clock::now();
    auto ms = [](auto lo, auto hi) {
      return std::chrono::duration<double, std::milli>(hi - lo).count();
    };
    std::string a_str;
    for (int b = 0; b < B; ++b) {
      if (b) a_str += ",";
      a_str += std::to_string(a[b]);
    }
    std::fprintf(stderr,
                 "[mtp-timing] B=%d draft=%.1f verify=%.1f extend+accept=%.1f "
                 "ms (k=%d a=[%s]) suffix=%d",
                 B, ms(t_draft0, t_draft1), ms(t_draft1, t_verify1),
                 ms(t_verify1, t_end), k, a_str.c_str(), !suffix_draft.empty());
    if (cudaEventSynchronize(ev_d3) == cudaSuccess) {
      float gd = 0.f, gv = 0.f, ge = 0.f;
      cudaEventElapsedTime(&gd, ev_d0, ev_d1);
      cudaEventElapsedTime(&gv, ev_d1, ev_d2);
      cudaEventElapsedTime(&ge, ev_d2, ev_d3);
      std::fprintf(stderr,
                   " gpu[ draft=%.1f verify=%.1f extend=%.1f ]\n", gd, gv,
                   ge);
    } else {
      std::fprintf(stderr, "\n");
    }
    cudaEventDestroy(ev_d0);
    cudaEventDestroy(ev_d1);
    cudaEventDestroy(ev_d2);
    cudaEventDestroy(ev_d3);
    ev_d0 = ev_d1 = ev_d2 = ev_d3 = nullptr;
  }
  return cleanup(Status());
}

Status MtpForward(const MtpModel& m, const int32_t* input_ids,
                  const int* positions, const uint16_t* hidden_states,
                  uint16_t* sample_hidden, uint16_t* multi_hidden,
                  uint16_t* logits, int T, cudaStream_t stream,
                  const int* d_seq_id, bool compute_logits, int max_position) {
  diagnostic::Scope forward_scope("mtp.forward", max_position, T);
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
  // Positions may originate on host or device; preserve stream ordering.
  int* d_positions = static_cast<int*>(carve(static_cast<size_t>(T) * sizeof(int)));
  if (detail::CopyPositionsForForward(d_positions, positions, T, stream) !=
      cudaSuccess)
    return Status::Fail("MtpForward: positions copy failed");

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
  // 4c. full attention. d_seq_id selects the per-token draft KV/indexer/rope
  // slice (multi-seq draft, Phase 2); null = single-seq (bit-identical).
  s = ObserveExecution("mtp.attention", [&] {
    return model::FullAttentionForward(m.full_attn, d_mixed_attn, d_attn_out,
                                       d_positions, m.d_rope_pos, m.kv_cache,
                                       m.page_table, m.idx_raw, m.idx_comp, T,
                                       d_attn_ws, attn_ws, stream, d_seq_id,
                                       max_position);
  });
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
  s = ObserveExecution("mtp.moe", [&] {
    return MoeBf16Forward(d_mixed_mlp, m.moe, m.moe_extra, d_mlp_out, T,
                          cfg.topk, d_moe_ws, moe_carve, d_moe_gemm,
                          kGemmWs, stream);
  });
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

  // 6. logits = lm_head(sample_hidden) [T, vocab]. Skipped when
  // compute_logits is false (chunked draft-extend intermediate chunks: the
  // [T, vocab] buffer is only needed for the final chunk's argmax — at 262K
  // a full-prompt logits buffer would be ~130 GB).
  if (compute_logits) {
    diagnostic::Scope head_scope("mtp.head", max_position, T);
    model::ModelHeadWeights head_view;
    head_view.lm_head = m.subset_head ? m.subset_head
                                    : const_cast<uint16_t*>(m.lm_head);
    head_view.vocab = m.DraftVocab();
    head_view.hs = hs;
    s = CheckGemm(model::Bf16Gemm(sample_hidden, head_view.lm_head, logits, T,
                                  head_view.vocab, hs, 1.0f, 0.0f, d_hc_gemm, kGemmWs,
                                  stream));
    if (!s.ok()) return s;
  }
  return Status();
}

}  // namespace mtp
}  // namespace q4t
