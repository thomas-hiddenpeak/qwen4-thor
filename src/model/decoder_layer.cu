// Complete qwen4_exp decoder layer — implementation. See
// include/q4t/model/decoder_layer.h for the orchestration.
//
//   hyper_input [T, hc*hs]
//     1. mixed_attn, attn_frame = GRRead(hyper_input, normed_scratch)
//     2. attn_out = attn_block(mixed_attn)        (linear or full)
//     3. combined_a = GRWrite(attn_out, attn_frame)
//     4. mixed_mlp, mlp_frame = GRRead(combined_a, normed_scratch)
//     5. mlp_out = moe(mixed_mlp)
//     6. out = GRWrite(mlp_out, mlp_frame)
//
// The layer carves its device `workspace` into per-submodule regions (they run
// sequentially, with existing auxiliary-stream joins). GRRead normed/down/up
// scratch borrows the MoE region before MoE starts; GEMM scratch remains
// separate.
#include "q4t/model/decoder_layer.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/model/decoder_workspace.h"
#include "q4t/status.h"

namespace q4t {
namespace model {
namespace {

constexpr size_t kGemmWs = 32u * 1024u * 1024u;  // cuBLASLt scratch per GEMM

// Debug: when Q4T_MLP_DUMP=<tag>, copy this layer's trunk at the MoE boundary
// (moe_in = mlp_hc.mix output = MoE input, moe_out = MoE output) to
// <tag>_m<idx>.{moe_in,moe_out}.bin (BF16, [T,hs]). Each forward calls
// DecoderLayerForward once per layer in order, so idx (0-based, reset when the
// tag changes) is the layer. This isolates the MoE GEMM: if moe_in is exact
// but moe_out diverges between the batch and incremental paths, the NVFP4 MoE
// (shape-dependent cuBLASLt) is the source of the batch-vs-incremental gap.
void DumpMoeBoundary(const uint16_t* moe_in, const uint16_t* moe_out, int T,
                     int hs) {
  const char* e = std::getenv("Q4T_MLP_DUMP");
  if (!e || !*e) return;
  // Own counter (separate from DumpLayerOut / DumpTrunkBeforeMix) so each
  // dump's _m<idx> is the layer index regardless of call order within a layer.
  static std::string last_tag_moe;
  static int idx_moe = 0;
  if (std::string(e) != last_tag_moe) {
    last_tag_moe = e;
    idx_moe = 0;
  }
  const int li = idx_moe++;
  const std::string base = std::string(e) + "_m" + std::to_string(li);
  auto w = [&](const char* name, const uint16_t* src) {
    std::string p = base + "." + name + ".bin";
    std::vector<uint16_t> h(static_cast<size_t>(T) * hs);
    cudaMemcpy(h.data(), src, h.size() * sizeof(uint16_t),
               cudaMemcpyDeviceToHost);
    FILE* f = std::fopen(p.c_str(), "wb");
    if (f) {
      std::fwrite(h.data(), sizeof(uint16_t), h.size(), f);
      std::fclose(f);
    }
  };
  w("moe_in", moe_in);
  w("moe_out", moe_out);
}

// Debug: when Q4T_MLP_DUMP=<tag>, copy this layer's OUTPUT trunk (the residual
// stream `out`, [T, hc_dim] BF16) to <tag>_m<idx>.out.bin. Used with
// DumpMoeBoundary to close the loop: if layer 0's moe_out is exact but its
// `out` diverges, the mlp_hc.combine GEMM is the source; if `out` is exact but
// the NEXT layer's x diverges, the next attn_hc.mix GEMM is the source.
void DumpLayerOut(const uint16_t* out, int T, int hc_dim) {
  const char* e = std::getenv("Q4T_MLP_DUMP");
  if (!e || !*e) return;
  static std::string last_tag_out;
  static int idx_out = 0;
  if (std::string(e) != last_tag_out) {
    last_tag_out = e;
    idx_out = 0;
  }
  const int li = idx_out++;
  const std::string p =
      std::string(e) + "_m" + std::to_string(li) + ".out.bin";
  std::vector<uint16_t> h(static_cast<size_t>(T) * hc_dim);
  cudaMemcpy(h.data(), out, h.size() * sizeof(uint16_t),
             cudaMemcpyDeviceToHost);
  FILE* f = std::fopen(p.c_str(), "wb");
  if (f) {
    std::fwrite(h.data(), sizeof(uint16_t), h.size(), f);
    std::fclose(f);
  }
}

// Debug: when Q4T_MLP_DUMP=<tag>, copy this layer's trunk BEFORE attn_hc.mix
// (the input to the HC mix GEMM) to <tag>_m<idx>.trunk_in.bin (BF16, [T,
// hc_dim]). For PLE layers this is the PLE-corrected trunk (hyper_input +
// ple_out); for non-PLE layers it's the raw hyper_input. Used to isolate
// whether the PLE SSD-stream path (n-gram hash → NVMe FP8 lookup → key/value
// projection GEMMs) or the attn_hc.mix GEMM introduces the batch-vs-
// incremental divergence. Without this, an exact previous-layer `out` does NOT
// establish an identical input to the PLE layer's attn_hc.mix, because
// PleLayerForward (with trunk_add) modifies the trunk in between.
void DumpTrunkBeforeMix(const uint16_t* trunk, int T, int hc_dim) {
  const char* e = std::getenv("Q4T_MLP_DUMP");
  if (!e || !*e) return;
  static std::string last_tag_trunk;
  static int idx_trunk = 0;
  if (std::string(e) != last_tag_trunk) {
    last_tag_trunk = e;
    idx_trunk = 0;
  }
  const int li = idx_trunk++;
  const std::string p =
      std::string(e) + "_m" + std::to_string(li) + ".trunk_in.bin";
  std::vector<uint16_t> h(static_cast<size_t>(T) * hc_dim);
  cudaMemcpy(h.data(), trunk, h.size() * sizeof(uint16_t),
             cudaMemcpyDeviceToHost);
  FILE* f = std::fopen(p.c_str(), "wb");
  if (f) {
    std::fwrite(h.data(), sizeof(uint16_t), h.size(), f);
    std::fclose(f);
  }
}

// Debug: when Q4T_MLP_DUMP=<tag>, copy the raw PLE `embeddings` (the NVMe
// SSD-stream lookup output, [T, ple_embed_dim] BF16) to <tag>.ple_emb.bin.
// This is the decisive discriminator for the batch-vs-incremental divergence:
//   - identical between the two paths  -> the n-gram hash + NVMe gather is
//     correct, and the divergence is from the PLE projection GEMMs
//     (key_proj/value_proj, shape-dependent cuBLASLt, same class as HC mix).
//   - different                        -> a REAL PLE bug in the n-gram history
//     / NVMe lookup across the prefill->decode boundary.
// Only the PLE layer calls this, so no layer index is needed.
void DumpPleEmbeddings(const uint16_t* embeddings, int T, int ple_embed_dim) {
  const char* e = std::getenv("Q4T_MLP_DUMP");
  if (!e || !*e) return;
  const std::string p = std::string(e) + ".ple_emb.bin";
  std::vector<uint16_t> h(static_cast<size_t>(T) * ple_embed_dim);
  cudaMemcpy(h.data(), embeddings, h.size() * sizeof(uint16_t),
             cudaMemcpyDeviceToHost);
  FILE* f = std::fopen(p.c_str(), "wb");
  if (f) {
    std::fwrite(h.data(), sizeof(uint16_t), h.size(), f);
    std::fclose(f);
  }
}

// Align a byte count up to 256 (cuBLASLt wants aligned scratch pointers).
inline size_t AlignUp(size_t x) { return (x + 255u) & ~size_t(255u); }

// Attention-block workspace size for `T` tokens. Full attention carves its
// intermediates from the workspace (scales with T) plus a GEMM scratch; linear
// attention cudaMallocs its own intermediates and only needs GEMM scratch.
size_t AttnWs(int T, bool is_full) {
  if (is_full) {
    // d_qg(T*12288) + d_k,d_v(T*512 ea) + d_q,d_gate(T*6144 ea) +
    // d_iq(T*512) + d_ik,d_ik_raw(T*128 ea) + d_logits(T*2048 f32) +
    // d_topk(T*2052 i32) + d_attn(T*6144), all BF16 except logits/topk,
    // plus the 32 MiB GEMM scratch. 70 KiB/token is a safe upper bound.
    return kGemmWs + static_cast<size_t>(T) * 70u * 1024u;
  }
  return kGemmWs;  // linear: GEMM scratch only
}

}  // namespace

DecoderWorkspaceLayout MakeDecoderWorkspaceLayout(
    int T, bool is_full, bool has_ple, int hc, int hs, int E, int moe_is,
    int shared_is, int k, const FullAttentionWeights* full, int lowrank) {
  DecoderWorkspaceLayout layout;
  layout.attention_bytes = is_full && full
                               ? FullAttentionWorkspaceBytes(*full, T)
                               : AttnWs(T, is_full);
  layout.moe_bytes = MoEForwardWorkspaceBytes(T, k, hs, moe_is, shared_is, E);
  layout.ple_bytes = has_ple ? PleLayerWorkspaceBytes(T, hc, hs) : 0;
  const size_t hidden_bytes = static_cast<size_t>(T) * hs * sizeof(uint16_t);
  const size_t hyper_bytes = hidden_bytes * hc;
  layout.hidden_bytes = hidden_bytes;
  layout.hyper_bytes = hyper_bytes;
  layout.hc_down_bytes = static_cast<size_t>(T) * lowrank * sizeof(uint16_t);
  layout.hc_up_bytes = hyper_bytes;
  const size_t read_bytes = AlignUp(hyper_bytes) +
                            AlignUp(layout.hc_down_bytes) +
                            AlignUp(layout.hc_up_bytes);
  auto carve = [&](size_t bytes) {
    const size_t offset = layout.total_bytes;
    layout.total_bytes += AlignUp(bytes);
    return offset;
  };
  layout.attention = carve(layout.attention_bytes);
  layout.moe =
      carve(layout.moe_bytes >= read_bytes ? layout.moe_bytes : read_bytes);
  layout.moe_gemm = carve(kGemmWs);
  layout.hc_gemm = carve(kGemmWs);
  layout.ple = carve(layout.ple_bytes);
  layout.mixed = carve(hidden_bytes);
  layout.block = carve(hidden_bytes);
  layout.normed = layout.moe;  // Last read: inject, before MoE overwrites it.
  layout.hc_down = layout.normed + AlignUp(hyper_bytes);
  layout.hc_up = layout.hc_down + AlignUp(layout.hc_down_bytes);
  layout.combined = carve(hyper_bytes);
  layout.ple_trunk = carve(has_ple ? hyper_bytes : 0);
  // Both frames reuse this region in stream order. It survives each sublayer
  // and must remain outside the MoE/GRRead scratch alias.
  layout.gate_bytes = static_cast<size_t>(T) * hc * sizeof(uint16_t);
  layout.gate = carve(layout.gate_bytes);
  return layout;
}

std::array<DecoderResourceView, 13> DescribeDecoderWorkspace(
    const DecoderWorkspaceLayout& layout) {
  using Phase = DecoderPhase;
  constexpr auto bit = [](Phase phase) {
    return uint32_t{1} << static_cast<unsigned>(phase);
  };
  constexpr uint32_t read = bit(Phase::kAttentionRead) | bit(Phase::kMlpRead);
  constexpr uint32_t mixed = read | bit(Phase::kAttention) | bit(Phase::kMoe);
  constexpr uint32_t block = bit(Phase::kAttention) |
                             bit(Phase::kAttentionWrite) | bit(Phase::kMoe) |
                             bit(Phase::kMlpWrite);
  constexpr uint32_t residual = bit(Phase::kAttentionWrite) |
                                bit(Phase::kMlpRead) | bit(Phase::kMoe) |
                                bit(Phase::kMlpWrite);
  constexpr uint32_t ple_trunk = bit(Phase::kPle) | bit(Phase::kAttentionRead) |
                                 bit(Phase::kAttention) |
                                 bit(Phase::kAttentionWrite);
  constexpr uint32_t gate = read | block;
  return {{{"attention", layout.attention, layout.attention_bytes,
            bit(Phase::kAttention)},
           {"moe", layout.moe, layout.moe_bytes, bit(Phase::kMoe)},
           {"moe_gemm", layout.moe_gemm, kGemmWs, bit(Phase::kMoe)},
           {"hc_gemm", layout.hc_gemm, kGemmWs, read},
           {"ple", layout.ple, layout.ple_bytes, bit(Phase::kPle)},
           {"mixed", layout.mixed, layout.hidden_bytes, mixed},
           {"block", layout.block, layout.hidden_bytes, block},
           {"normed", layout.normed, layout.hyper_bytes, read},
           {"down", layout.hc_down, layout.hc_down_bytes, read},
           {"up", layout.hc_up, layout.hc_up_bytes, read},
           {"combined", layout.combined, layout.hyper_bytes, residual},
           {"ple_trunk", layout.ple_trunk,
            layout.ple_bytes ? layout.hyper_bytes : 0, ple_trunk},
           {"gate", layout.gate, layout.gate_bytes, gate}}};
}

size_t DecoderLayerWorkspaceBytes(int T, bool is_full_attention, bool has_ple,
                                  int hs, int E, int moe_is, int shared_is,
                                  int k, const FullAttentionWeights* full,
                                  int lowrank) {
  return MakeDecoderWorkspaceLayout(T, is_full_attention, has_ple, 4, hs, E,
                                    moe_is, shared_is, k, full, lowrank)
      .total_bytes;
}

void DecoderLayer::Free() {
  linear.Free();
  full.Free();
  mlp.Free();
  attn_hc.Free();
  mlp_hc.Free();
  ple.Free();
  auto freep = [](void* p) { if (p) cudaFree(p); };
  freep(ssm_state);
  freep(conv_state);
  freep(ple_conv_state);
  freep(kv_cache);
  freep(page_table);
  freep(idx_raw);
  freep(idx_comp);
  ssm_state = nullptr;
  conv_state = ple_conv_state = kv_cache = idx_raw = idx_comp = nullptr;
  page_table = nullptr;
  // routed (NVFP4) buffers (MoEWeightLayout has no Free; free manually).
  if (routed.gu_packed) cudaFree(routed.gu_packed);
  if (routed.gu_sf) cudaFree(routed.gu_sf);
  if (routed.dn_packed) cudaFree(routed.dn_packed);
  if (routed.dn_sf) cudaFree(routed.dn_sf);
  if (routed.gu_w_scale2) cudaFree(routed.gu_w_scale2);
  if (routed.gu_input_scale) cudaFree(routed.gu_input_scale);
  if (routed.dn_w_scale2) cudaFree(routed.dn_w_scale2);
  if (routed.dn_input_scale) cudaFree(routed.dn_input_scale);
  routed = quant::MoEWeightLayout();
}

void DecoderLayer::ResetState(int seq_id, cudaStream_t stream) const {
  // Zero ONE sequence's recurrent-state slice. The pooled buffers are laid
  // out as [max_seq, ...]; the per-sequence stride is expressed in BYTES and
  // applied via a char* so the element size of the typed pointer (float vs
  // uint16_t) cannot double the stride. (A previous version added the byte
  // stride to the typed pointer, which for the uint16_t full-attention
  // caches (kv_cache/idx_raw/idx_comp) doubled the stride and made seq_id
  // >= 2 write past the allocation -> illegal memory access.)
  const size_t seq = static_cast<size_t>(seq_id);
  if (is_full_attention) {
    const size_t kv_bytes = static_cast<size_t>(
        (max_len + kKvPageSize - 1) / kKvPageSize) * kKvPageSize * 2 * 2 *
        256 * 2;  // one sequence's paged KV, in bytes
    const size_t idx_bytes =
        static_cast<size_t>(max_len) * 128 * 2;  // one sequence's idx, bytes
    if (kv_cache)
      cudaMemsetAsync(reinterpret_cast<char*>(kv_cache) + seq * kv_bytes, 0,
                      kv_bytes, stream);
    if (idx_raw)
      cudaMemsetAsync(reinterpret_cast<char*>(idx_raw) + seq * idx_bytes, 0,
                      idx_bytes, stream);
    if (idx_comp)
      cudaMemsetAsync(reinterpret_cast<char*>(idx_comp) + seq * idx_bytes, 0,
                      idx_bytes, stream);
  } else {
    if (ssm_state)
      cudaMemsetAsync(ssm_state + seq * 48 * 128 * 128, 0,
                      static_cast<size_t>(48) * 128 * 128 * 4, stream);
    if (conv_state)
      cudaMemsetAsync(conv_state + seq * 10240 * 3, 0,
                      static_cast<size_t>(10240) * 3 * 2, stream);
  }
  if (ple_conv_state)
    cudaMemsetAsync(ple_conv_state + seq * 10240 * 9, 0,
                    static_cast<size_t>(10240) * 9 * 2, stream);
}

Status LoadDecoderLayer(const io::WeightLoader& loader, int layer_id, int hs,
                        int hc, int lowrank, float eps, int E, int moe_is,
                        int shared_is, int k, int max_len, int max_seq,
                        DecoderLayer* out, cudaStream_t stream) {
  out->layer_id = layer_id;
  out->hs = hs;
  out->hc = hc;
  out->hc_dim = hc * hs;
  out->is_full_attention = (layer_id % 4 == 3);
  out->topk = k;
  out->max_len = max_len;
  // ple_layer_ids = [2] is 1-indexed -> 0-indexed layer 1.
  out->has_ple = (layer_id == 1);
  const std::string base = "model.language_model.layers." +
                           std::to_string(layer_id);

  Status s;
  // 1. Hyper-Connection GatedResiduals (both use_mix + use_combine).
  s = LoadHyperConnection(loader, base + ".attn_hyper_connection", hc, hs,
                          lowrank, eps, true, &out->attn_hc, stream);
  if (!s.ok()) return s;
  s = LoadHyperConnection(loader, base + ".mlp_hyper_connection", hc, hs,
                          lowrank, eps, true, &out->mlp_hc, stream);
  if (!s.ok()) return s;

  // 2. Attention block (linear or full).
  if (out->is_full_attention) {
    s = LoadFullAttention(loader, base + ".self_attn", hs, 24, 2, 256, 64, 1e7f,
                          eps, 4, 1, 128, 2048, 4, &out->full, stream);
    if (!s.ok()) return s;
    out->full.max_len = max_len;  // for the persistent 3D MRoPE table.
    // Full-attention caches.
    const int nkv = 2, hd = 256, idx_hd = 128;
    auto alloc = [](void** p, size_t bytes) -> Status {
      if (cudaMalloc(p, bytes) != cudaSuccess) return Status::Fail("cudaMalloc");
      return Status();
    };
    // Paged KV, POOLED over max_seq sequences: kv_cache [max_seq, n_pages,
    // kKvPageSize, nkv, 2, hd], page_table [max_seq, max_len], idx_raw/comp
    // [max_seq, max_len, idx_hd]. Sequence s uses slice s (seq_id offset).
    const int n_pages = (max_len + kKvPageSize - 1) / kKvPageSize;
    const size_t seq = static_cast<size_t>(max_seq);
    const size_t kv_bytes =
        seq * static_cast<size_t>(n_pages) * kKvPageSize * nkv * 2 * hd * 2;
    if (!(s = alloc(reinterpret_cast<void**>(&out->kv_cache), kv_bytes)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->page_table),
                    seq * static_cast<size_t>(max_len) * 4)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->idx_raw),
                    seq * static_cast<size_t>(max_len) * idx_hd * 2)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->idx_comp),
                    seq * static_cast<size_t>(max_len) * idx_hd * 2)))
      return s;
    cudaMemset(out->kv_cache, 0, kv_bytes);
    cudaMemset(out->idx_raw, 0, seq * static_cast<size_t>(max_len) * idx_hd * 2);
    cudaMemset(out->idx_comp, 0, seq * static_cast<size_t>(max_len) * idx_hd * 2);
    std::vector<int> page_table(seq * static_cast<size_t>(max_len));
    for (size_t i = 0; i < page_table.size(); ++i)
      page_table[i] = (i % max_len) / kKvPageSize;  // identity per sequence
    if (cudaMemcpy(out->page_table, page_table.data(),
                   page_table.size() * 4, cudaMemcpyHostToDevice) !=
        cudaSuccess)
      return Status::Fail("cudaMemcpy page_table failed");
  } else {
    s = LoadLinearAttention(loader, base + ".linear_attn", hs, 16, 48, 128, 128,
                            4, eps, &out->linear, stream);
    if (!s.ok()) return s;
    // Linear caches, POOLED over max_seq concurrent sequences (Phase 2
    // continuous batching): ssm_state [max_seq, nv, kd, vd], conv_state
    // [max_seq, in_qkv, conv_k-1]. Sequence s uses slice s (seq_id offset);
    // max_seq == 1 is the legacy single-sequence layout (bit-identical).
    const int nv = 48, kd = 128, vd = 128, in_qkv = 10240, conv_k = 4;
    const size_t seq = static_cast<size_t>(max_seq);
    auto alloc = [](void** p, size_t bytes) -> Status {
      if (cudaMalloc(p, bytes) != cudaSuccess) return Status::Fail("cudaMalloc");
      return Status();
    };
    if (!(s = alloc(reinterpret_cast<void**>(&out->ssm_state),
                    seq * static_cast<size_t>(nv) * kd * vd * 4)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->conv_state),
                    seq * static_cast<size_t>(in_qkv) * (conv_k - 1) * 2)))
      return s;
    cudaMemset(out->ssm_state, 0, seq * static_cast<size_t>(nv) * kd * vd * 4);
    cudaMemset(out->conv_state, 0,
               seq * static_cast<size_t>(in_qkv) * (conv_k - 1) * 2);
  }

  // 3b. PLE short-conv state, POOLED over max_seq sequences: [max_seq, hc*hs,
  //     (K-1)*dilation] = [max_seq, 10240, 9] BF16. Allocated for the PLE
  //     layer only (has_ple); zeroed (fresh sequence).
  if (out->has_ple) {
    const int hc_dim = hc * hs;
    const int state_len = (out->ple.conv_kernel - 1) * out->ple.conv_dilation;
    const size_t seq = static_cast<size_t>(max_seq);
    auto alloc = [](void** p, size_t bytes) -> Status {
      if (cudaMalloc(p, bytes) != cudaSuccess) return Status::Fail("cudaMalloc");
      return Status();
    };
    if (!(s = alloc(reinterpret_cast<void**>(&out->ple_conv_state),
                    seq * static_cast<size_t>(hc_dim) * state_len * 2)))
      return s;
    cudaMemset(out->ple_conv_state, 0,
               seq * static_cast<size_t>(hc_dim) * state_len * 2);
  }

  // 3. MoE (routed NVFP4 + BF16 router/shared).
  s = quant::LoadMoEWeights(loader, layer_id, E, hs, moe_is, &out->routed,
                            stream);
  if (!s.ok()) return s;
  s = LoadMoEExtra(loader, base + ".mlp", E, hs, shared_is, &out->mlp, stream);
  if (!s.ok()) return s;

  // 4. PLE layer (layer 1 only).
  if (out->has_ple) {
    s = LoadPleLayer(loader, base + ".ple", hc, hs, hs, 4, 3, eps, &out->ple,
                     stream);
    if (!s.ok()) return s;
  }

  return Status();
}

Status DecoderLayerForward(const DecoderLayer& layer,
                           const uint16_t* hyper_input,
                           const uint16_t* ple_embeddings, uint16_t* out,
                           const int* positions, const int* rope_pos, int T,
                           void* workspace, size_t workspace_bytes,
                           cudaStream_t stream, float* ssm_ckpt,
                           uint16_t* conv_ckpt, int num_ckpt, int seq_id,
                           const int* d_seq_id, const int* d_rope_pos,
                           int tokens_per_seq, uint16_t* ple_conv_ckpt,
                           const RaggedBatch* ragged, int max_position) {
  const int hs = layer.hs, hc_dim = layer.hc_dim, hc = layer.hc;
  if (T <= 0) return Status();
  // Pooled recurrent-state slices for this sequence (seq_id selects the
  // [max_seq, ...] slice; 0 = legacy single-sequence layout). In B2
  // multi-sequence decode (d_seq_id != null) ALL persistent state (linear
  // SSM/conv, PLE conv, full-attention KV/page_table/indexer) is passed as the
  // POOLED base — the multi-seq kernels index the per-token slice via
  // d_seq_id. In single-sequence mode (d_seq_id == null) each pointer is the
  // per-sequence slice (seq_id selects it; 0 = legacy layout).
  const size_t seq = static_cast<size_t>(seq_id);
  float* ssm_state =
      layer.ssm_state ? (d_seq_id ? layer.ssm_state
                                  : layer.ssm_state + seq * 48 * 128 * 128)
                      : nullptr;
  uint16_t* conv_state =
      layer.conv_state ? (d_seq_id ? layer.conv_state
                                   : layer.conv_state + seq * 10240 * 3)
                       : nullptr;
  uint16_t* ple_conv_state = layer.ple_conv_state
                                 ? (d_seq_id ? layer.ple_conv_state
                                            : layer.ple_conv_state + seq * 10240 * 9)
                                 : nullptr;
  const size_t kv_seq_bytes = static_cast<size_t>(
      (layer.max_len + kKvPageSize - 1) / kKvPageSize) * kKvPageSize * 2 * 2 *
      256 * 2;  // one sequence's paged KV (nkv=2, 2 for K+V, hd=256, bf16)
  // kv_cache is a uint16_t* but kv_seq_bytes is a BYTE stride; offset via
  // char* so the element size does not double the stride (mirrors ResetState).
  uint16_t* kv_cache = layer.kv_cache
                           ? (d_seq_id
                                  ? layer.kv_cache
                                  : reinterpret_cast<uint16_t*>(
                                        reinterpret_cast<char*>(layer.kv_cache) +
                                        seq * kv_seq_bytes))
                           : nullptr;
  int* page_table =
      layer.page_table ? (d_seq_id ? layer.page_table
                                   : layer.page_table + seq * layer.max_len)
                       : nullptr;
  uint16_t* idx_raw =
      layer.idx_raw ? (d_seq_id ? layer.idx_raw
                                : layer.idx_raw + seq * layer.max_len * 128)
                    : nullptr;
  uint16_t* idx_comp =
      layer.idx_comp ? (d_seq_id ? layer.idx_comp
                                 : layer.idx_comp + seq * layer.max_len * 128)
                     : nullptr;

  const DecoderWorkspaceLayout layout = MakeDecoderWorkspaceLayout(
      T, layer.is_full_attention, layer.has_ple, hc, hs, layer.routed.E,
      layer.routed.moe_is, layer.mlp.shared_is, layer.topk, &layer.full,
      std::max(layer.attn_hc.lowrank, layer.mlp_hc.lowrank));
  if (layout.total_bytes > workspace_bytes) {
    return Status::Fail("DecoderLayerForward: workspace too small");
  }
  char* base = static_cast<char*>(workspace);
  void* d_attn_ws = base + layout.attention;
  void* d_moe_ws = base + layout.moe;
  void* d_moe_gemm = base + layout.moe_gemm;
  void* d_hc_ws = base + layout.hc_gemm;
  void* d_ple_ws = base + layout.ple;
  uint16_t* d_mixed = reinterpret_cast<uint16_t*>(base + layout.mixed);
  uint16_t* d_block = reinterpret_cast<uint16_t*>(base + layout.block);
  uint16_t* d_normed = reinterpret_cast<uint16_t*>(base + layout.normed);
  uint16_t* d_combined = reinterpret_cast<uint16_t*>(base + layout.combined);
  const HyperConnectionMixScratch hc_mix_scratch{
      reinterpret_cast<uint16_t*>(base + layout.hc_down),
      reinterpret_cast<uint16_t*>(base + layout.hc_up), layout.hc_down_bytes,
      layout.hc_up_bytes};
  const GatedResidualGateStorage gate_storage{
      reinterpret_cast<uint16_t*>(base + layout.gate), layout.gate_bytes};

  Status s;

  // The trunk residual the rest of the layer reads. For the PLE layer this is
  // a scratch copy (hyper_input is const); otherwise it aliases hyper_input.
  // d_ple_trunk is carved from the workspace scratch region (no cudaMalloc).
  const uint16_t* d_trunk = hyper_input;
  uint16_t* d_ple_trunk = nullptr;
  if (layer.has_ple) {
    d_ple_trunk = reinterpret_cast<uint16_t*>(base + layout.ple_trunk);
    // 1. PLE: d_ple_trunk = hyper_input + ple(ple_embeddings, hyper_input).
    DumpPleEmbeddings(ple_embeddings, T, layer.ple.ple_embed_dim);
    // Fused: PleLayerForward with trunk_add=hyper_input computes
    // d_ple_trunk = hyper_input + ple_out in one launch (no separate
    // PleAddTrunkKernel).
    s = PleLayerForward(layer.ple, ple_embeddings, hyper_input, d_ple_trunk, T,
                        ple_conv_state, d_ple_ws, layout.ple_bytes, stream,
                        hyper_input, d_seq_id, tokens_per_seq, ple_conv_ckpt,
                        num_ckpt, ragged);
    if (!s.ok()) {
      return s;
    }
    d_trunk = d_ple_trunk;
  }
  DumpTrunkBeforeMix(d_trunk, T, hc_dim);

  // GRRead prepares inject before the sublayer; normed is now temporary.
  GatedResidualFrame attn_frame;
  s = attn_frame.Read(layer.attn_hc, d_trunk, d_mixed, d_normed, T, d_hc_ws,
                      kGemmWs, stream, &hc_mix_scratch, &gate_storage);
  if (!s.ok()) {
    return s;
  }
  // 2. attention block.
  if (layer.is_full_attention) {
    // B2 multi-seq: the full-attention kernels select the per-token rope slice
    // via d_seq_id, so pass the POOLED rope base (d_rope_pos); otherwise the
    // per-sequence slice (rope_pos).
    const int* fa_rope = d_seq_id ? d_rope_pos : rope_pos;
    s = FullAttentionForward(layer.full, d_mixed, d_block, positions, fa_rope,
                             kv_cache, page_table, idx_raw, idx_comp, T,
                             d_attn_ws, layout.attention_bytes, stream,
                             d_seq_id, max_position);
  } else {
    s = LinearAttentionForward(layer.linear, d_mixed, d_block, ssm_state,
                               conv_state, T, d_attn_ws, layout.attention_bytes,
                               stream, ssm_ckpt, conv_ckpt, num_ckpt, d_seq_id,
                               tokens_per_seq, ragged);
  }
  if (!s.ok()) {
    return s;
  }
  // GRWrite consumes only the borrowed residual and prepared inject gate.
  s = attn_frame.Write(d_block, d_combined);
  if (!s.ok()) {
    return s;
  }
  GatedResidualFrame mlp_frame;
  s = mlp_frame.Read(layer.mlp_hc, d_combined, d_mixed, d_normed, T, d_hc_ws,
                     kGemmWs, stream, &hc_mix_scratch, &gate_storage);
  if (!s.ok()) {
    return s;
  }
  // 5. MoE.
  s = MoEForward(d_mixed, layer.routed, layer.mlp, d_block, T, layer.topk,
                 d_moe_ws, layout.moe_bytes, d_moe_gemm, kGemmWs, stream);
  if (!s.ok()) {
    return s;
  }
  DumpMoeBoundary(d_mixed, d_block, T, hs);
  s = mlp_frame.Write(d_block, out);
  DumpLayerOut(out, T, hc_dim);
  return s;
}

}  // namespace model
}  // namespace q4t
