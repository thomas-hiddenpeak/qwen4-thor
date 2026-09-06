// Complete qwen4_exp decoder layer — implementation. See
// include/q4t/model/decoder_layer.h for the orchestration.
//
//   hyper_input [T, hc*hs]
//     1. mixed_attn, res_a = attn_hc.mix(hyper_input)
//     2. attn_out = attn_block(mixed_attn)        (linear or full)
//     3. combined_a = attn_hc.combine(attn_out, hyper_input, res_a)
//     4. mixed_mlp, res_m = mlp_hc.mix(combined_a)
//     5. mlp_out = moe(mixed_mlp)
//     6. out = mlp_hc.combine(mlp_out, combined_a, res_m)
//
// The layer carves its device `workspace` into per-submodule regions (they run
// sequentially on one stream, but each gets its own region so a submodule's
// cuBLASLt scratch / carved intermediates never alias another's).
#include "q4t/model/decoder_layer.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

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
// PleLayerForward + PleAddTrunkKernel modify the trunk in between.
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

// Elementwise BF16 add: o[i] = a[i] + b[i]. Used to form the PLE-corrected
// trunk (hyper_input + ple_out). No __restrict__ on the pointers because the
// caller passes o == b (in-place add); each element is read before written, so
// this is safe. (Trivial kernel, not a hot path.)
__global__ void PleAddTrunkKernel(const uint16_t* a, const uint16_t* b,
                                  uint16_t* o, int total) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= total) return;
  uint32_t ba = static_cast<uint32_t>(a[i]) << 16;
  uint32_t bb = static_cast<uint32_t>(b[i]) << 16;
  float fa, fb;
  std::memcpy(&fa, &ba, sizeof(fa));
  std::memcpy(&fb, &bb, sizeof(fb));
  const __nv_bfloat16 r = __float2bfloat16_rn(fa + fb);
  o[i] = *reinterpret_cast<const uint16_t*>(&r);
}

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

size_t DecoderLayerWorkspaceBytes(int T, bool is_full_attention, bool has_ple,
                                  int hs, int E, int moe_is, int shared_is,
                                  int k, const FullAttentionWeights* full) {
  const size_t attn = is_full_attention && full
                          ? AlignUp(FullAttentionWorkspaceBytes(*full, T))
                          : AlignUp(AttnWs(T, is_full_attention));
  const size_t moe_carve = AlignUp(
      MoEForwardWorkspaceBytes(T, k, hs, moe_is, shared_is, E));
  size_t total = attn + moe_carve + kGemmWs + kGemmWs;  // + moe gemm + hc scratch
  if (has_ple) total += AlignUp(PleLayerWorkspaceBytes(T, 4, hs));
  return total;
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

void DecoderLayer::ResetState(cudaStream_t stream) const {
  if (is_full_attention) {
    // Paged KV: allocation is n_pages * kKvPageSize positions (>= max_len).
    // Zero the whole allocation (identity mapping makes it bit-identical to
    // the legacy contiguous zeroing).
    if (kv_cache) {
      const int n_pages = (max_len + kKvPageSize - 1) / kKvPageSize;
      const size_t kv_bytes =
          static_cast<size_t>(n_pages) * kKvPageSize * 2 * 2 * 256 * 2;
      cudaMemsetAsync(kv_cache, 0, kv_bytes, stream);
    }
    if (idx_raw)
      cudaMemsetAsync(idx_raw, 0,
                      static_cast<size_t>(max_len) * 128 * 2, stream);
    if (idx_comp)
      cudaMemsetAsync(idx_comp, 0,
                      static_cast<size_t>(max_len) * 128 * 2, stream);
  } else {
    if (ssm_state)
      cudaMemsetAsync(ssm_state, 0,
                      static_cast<size_t>(48) * 128 * 128 * 4, stream);
    if (conv_state)
      cudaMemsetAsync(conv_state, 0,
                      static_cast<size_t>(10240) * 3 * 2, stream);
  }
  if (ple_conv_state)
    cudaMemsetAsync(ple_conv_state, 0,
                    static_cast<size_t>(10240) * 9 * 2, stream);
}

Status LoadDecoderLayer(const io::WeightLoader& loader, int layer_id, int hs,
                        int hc, int lowrank, float eps, int E, int moe_is,
                        int shared_is, int k, int max_len, DecoderLayer* out,
                        cudaStream_t stream) {
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
    // Full-attention caches.
    const int nkv = 2, hd = 256, idx_hd = 128;
    auto alloc = [](void** p, size_t bytes) -> Status {
      if (cudaMalloc(p, bytes) != cudaSuccess) return Status::Fail("cudaMalloc");
      return Status();
    };
    // Paged KV: n_pages * kKvPageSize positions (>= max_len).
    const int n_pages = (max_len + kKvPageSize - 1) / kKvPageSize;
    const size_t kv_bytes =
        static_cast<size_t>(n_pages) * kKvPageSize * nkv * 2 * hd * 2;
    if (!(s = alloc(reinterpret_cast<void**>(&out->kv_cache), kv_bytes)))
      return s;
    // Page table: identity mapping (page_table[p] = p / kKvPageSize) makes
    // the physical layout bit-identical to the legacy contiguous layout.
    if (!(s = alloc(reinterpret_cast<void**>(&out->page_table),
                    static_cast<size_t>(max_len) * 4)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->idx_raw),
                    static_cast<size_t>(max_len) * idx_hd * 2)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->idx_comp),
                    static_cast<size_t>(max_len) * idx_hd * 2)))
      return s;
    cudaMemset(out->kv_cache, 0, kv_bytes);
    cudaMemset(out->idx_raw, 0, static_cast<size_t>(max_len) * idx_hd * 2);
    cudaMemset(out->idx_comp, 0, static_cast<size_t>(max_len) * idx_hd * 2);
    std::vector<int> page_table(max_len);
    for (int p = 0; p < max_len; ++p) page_table[p] = p / kKvPageSize;
    if (cudaMemcpy(out->page_table, page_table.data(),
                   static_cast<size_t>(max_len) * 4,
                   cudaMemcpyHostToDevice) != cudaSuccess)
      return Status::Fail("cudaMemcpy page_table failed");
  } else {
    s = LoadLinearAttention(loader, base + ".linear_attn", hs, 16, 48, 128, 128,
                            4, eps, &out->linear, stream);
    if (!s.ok()) return s;
    // Linear caches: ssm_state [nv, kd, vd], conv_state [in_qkv, conv_k-1].
    const int nv = 48, kd = 128, vd = 128, in_qkv = 10240, conv_k = 4;
    auto alloc = [](void** p, size_t bytes) -> Status {
      if (cudaMalloc(p, bytes) != cudaSuccess) return Status::Fail("cudaMalloc");
      return Status();
    };
    if (!(s = alloc(reinterpret_cast<void**>(&out->ssm_state),
                    static_cast<size_t>(nv) * kd * vd * 4)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->conv_state),
                    static_cast<size_t>(in_qkv) * (conv_k - 1) * 2)))
      return s;
    cudaMemset(out->ssm_state, 0, static_cast<size_t>(nv) * kd * vd * 4);
    cudaMemset(out->conv_state, 0,
               static_cast<size_t>(in_qkv) * (conv_k - 1) * 2);
  }

  // 3b. PLE short-conv state [hc*hs, (K-1)*dilation] = [10240, 9] BF16.
  //     Allocated for the PLE layer only (has_ple); zeroed (fresh sequence).
  if (out->has_ple) {
    const int hc_dim = hc * hs;
    const int state_len = (out->ple.conv_kernel - 1) * out->ple.conv_dilation;
    auto alloc = [](void** p, size_t bytes) -> Status {
      if (cudaMalloc(p, bytes) != cudaSuccess) return Status::Fail("cudaMalloc");
      return Status();
    };
    if (!(s = alloc(reinterpret_cast<void**>(&out->ple_conv_state),
                    static_cast<size_t>(hc_dim) * state_len * 2)))
      return s;
    cudaMemset(out->ple_conv_state, 0, static_cast<size_t>(hc_dim) * state_len * 2);
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
                           const int* positions, int T, void* workspace,
                           size_t workspace_bytes, cudaStream_t stream) {
  const int hs = layer.hs, hc_dim = layer.hc_dim, hc = layer.hc;
  if (T <= 0) return Status();

  // Carve the workspace into per-submodule regions.
  const size_t attn_ws = layer.is_full_attention
                             ? FullAttentionWorkspaceBytes(layer.full, T)
                             : AttnWs(T, false);
  const size_t moe_carve = MoEForwardWorkspaceBytes(
      T, layer.topk, hs, layer.routed.moe_is, layer.mlp.shared_is, layer.routed.E);
  const size_t ple_ws =
      layer.has_ple ? PleLayerWorkspaceBytes(T, hc, hs) : 0;
  if (attn_ws + moe_carve + kGemmWs + kGemmWs + ple_ws > workspace_bytes) {
    return Status::Fail("DecoderLayerForward: workspace too small");
  }
  char* base = static_cast<char*>(workspace);
  void* d_attn_ws = base;
  char* p = base + AlignUp(attn_ws);
  void* d_moe_ws = p;
  p += AlignUp(moe_carve);
  void* d_moe_gemm = p;
  p += kGemmWs;
  void* d_hc_ws = p;
  p += kGemmWs;
  void* d_ple_ws = p;  // only used when layer.has_ple

  // Scratch for the [T, hs] / [T, hc*hs] block activations (cudaMalloc'd,
  // freed at the end — mirrors the HC/linear convention).
  uint16_t* d_mixed = nullptr;  // [T, hs]
  uint16_t* d_block = nullptr;  // [T, hs]
  uint16_t* d_res_a = nullptr;  // [T, hc*hs]
  uint16_t* d_res_m = nullptr;  // [T, hc*hs]
  uint16_t* d_combined = nullptr;  // [T, hc*hs]
  auto malloc5 = [&](uint16_t** p, size_t bytes) -> Status {
    if (cudaMalloc(reinterpret_cast<void**>(p), bytes) != cudaSuccess)
      return Status::Fail("cudaMalloc scratch");
    return Status();
  };
  auto free_all = [&]() {
    cudaFree(d_mixed);
    cudaFree(d_block);
    cudaFree(d_res_a);
    cudaFree(d_res_m);
    cudaFree(d_combined);
  };
  Status s;
  if (!(s = malloc5(&d_mixed, static_cast<size_t>(T) * hs * 2))) return s;
  if (!(s = malloc5(&d_block, static_cast<size_t>(T) * hs * 2))) {
    free_all();
    return s;
  }
  if (!(s = malloc5(&d_res_a, static_cast<size_t>(T) * hc_dim * 2))) {
    free_all();
    return s;
  }
  if (!(s = malloc5(&d_res_m, static_cast<size_t>(T) * hc_dim * 2))) {
    free_all();
    return s;
  }
  if (!(s = malloc5(&d_combined, static_cast<size_t>(T) * hc_dim * 2))) {
    free_all();
    return s;
  }

  // The trunk residual the rest of the layer reads. For the PLE layer this is
  // a scratch copy (hyper_input is const); otherwise it aliases hyper_input.
  const uint16_t* d_trunk = hyper_input;
  uint16_t* d_ple_trunk = nullptr;
  if (layer.has_ple) {
    if (cudaMalloc(reinterpret_cast<void**>(&d_ple_trunk),
                   static_cast<size_t>(T) * hc_dim * 2) != cudaSuccess) {
      free_all();
      return Status::Fail("cudaMalloc ple trunk");
    }
    // 1. PLE: d_ple_trunk = hyper_input + ple(ple_embeddings, hyper_input).
    DumpPleEmbeddings(ple_embeddings, T, layer.ple.ple_embed_dim);
    s = PleLayerForward(layer.ple, ple_embeddings, hyper_input, d_ple_trunk, T,
                        layer.ple_conv_state, d_ple_ws, ple_ws, stream);
    if (!s.ok()) {
      cudaFree(d_ple_trunk);
      free_all();
      return s;
    }
    {
      const int total = static_cast<int>(static_cast<size_t>(T) * hc_dim);
      const int block = 256;
      PleAddTrunkKernel<<<(total + block - 1) / block, block, 0, stream>>>(
          hyper_input, d_ple_trunk, d_ple_trunk, total);
      if (cudaGetLastError() != cudaSuccess) {
        cudaFree(d_ple_trunk);
        free_all();
        return Status::Fail("ple add trunk launch");
      }
    }
    d_trunk = d_ple_trunk;
  }
  DumpTrunkBeforeMix(d_trunk, T, hc_dim);

  // 2. attn_hc.mix(trunk) -> mixed_attn, res_a.
  s = HyperConnectionMix(layer.attn_hc, d_trunk, d_mixed, d_res_a, T,
                         d_hc_ws, kGemmWs, stream);
  if (!s.ok()) {
    if (d_ple_trunk) cudaFree(d_ple_trunk);
    free_all();
    return s;
  }
  // 2. attention block.
  if (layer.is_full_attention) {
    s = FullAttentionForward(layer.full, d_mixed, d_block, positions,
                             layer.kv_cache, layer.page_table, layer.idx_raw,
                             layer.idx_comp, T, d_attn_ws, attn_ws, stream);
  } else {
    s = LinearAttentionForward(layer.linear, d_mixed, d_block, layer.ssm_state,
                               layer.conv_state, T, d_attn_ws, attn_ws, stream);
  }
  if (!s.ok()) {
    free_all();
    return s;
  }
  // 3. attn_hc.combine(attn_out, trunk, res_a) -> combined_a.
  s = HyperConnectionCombine(layer.attn_hc, d_block, d_trunk, d_res_a,
                             d_combined, T, d_hc_ws, kGemmWs, stream);
  if (!s.ok()) {
    if (d_ple_trunk) cudaFree(d_ple_trunk);
    free_all();
    return s;
  }
  // 4. mlp_hc.mix(combined_a) -> mixed_mlp, res_m.
  s = HyperConnectionMix(layer.mlp_hc, d_combined, d_mixed, d_res_m, T,
                         d_hc_ws, kGemmWs, stream);
  if (!s.ok()) {
    if (d_ple_trunk) cudaFree(d_ple_trunk);
    free_all();
    return s;
  }
  // 5. MoE.
  s = MoEForward(d_mixed, layer.routed, layer.mlp, d_block, T, layer.topk,
                 d_moe_ws, moe_carve, d_moe_gemm, kGemmWs, stream);
  if (!s.ok()) {
    if (d_ple_trunk) cudaFree(d_ple_trunk);
    free_all();
    return s;
  }
  DumpMoeBoundary(d_mixed, d_block, T, hs);
  // 6. mlp_hc.combine(mlp_out, combined_a, res_m) -> out.
  s = HyperConnectionCombine(layer.mlp_hc, d_block, d_combined, d_res_m, out, T,
                             d_hc_ws, kGemmWs, stream);
  DumpLayerOut(out, T, hc_dim);
  if (d_ple_trunk) cudaFree(d_ple_trunk);
  free_all();
  return s;
}

}  // namespace model
}  // namespace q4t
