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

#include <cuda_runtime.h>

#include <cstdint>
#include <string>

#include "q4t/status.h"

namespace q4t {
namespace model {
namespace {

constexpr size_t kGemmWs = 32u * 1024u * 1024u;  // cuBLASLt scratch per GEMM

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

size_t DecoderLayerWorkspaceBytes(int T, bool is_full_attention, int hs, int E,
                                  int moe_is, int shared_is, int k) {
  const size_t attn = AlignUp(AttnWs(T, is_full_attention));
  const size_t moe_carve = AlignUp(
      MoEForwardWorkspaceBytes(T, k, hs, moe_is, shared_is, E));
  return attn + moe_carve + kGemmWs + kGemmWs;  // + moe gemm + hc scratch
}

void DecoderLayer::Free() {
  linear.Free();
  full.Free();
  mlp.Free();
  attn_hc.Free();
  mlp_hc.Free();
  auto freep = [](void* p) { if (p) cudaFree(p); };
  freep(ssm_state);
  freep(conv_state);
  freep(kv_cache);
  freep(idx_raw);
  freep(idx_comp);
  ssm_state = conv_state = kv_cache = idx_raw = idx_comp = nullptr;
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
    if (!(s = alloc(reinterpret_cast<void**>(&out->kv_cache),
                    static_cast<size_t>(max_len) * nkv * 2 * hd * 2)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->idx_raw),
                    static_cast<size_t>(max_len) * idx_hd * 2)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->idx_comp),
                    static_cast<size_t>(max_len) * idx_hd * 2)))
      return s;
    cudaMemset(out->kv_cache, 0, static_cast<size_t>(max_len) * nkv * 2 * hd * 2);
    cudaMemset(out->idx_raw, 0, static_cast<size_t>(max_len) * idx_hd * 2);
    cudaMemset(out->idx_comp, 0, static_cast<size_t>(max_len) * idx_hd * 2);
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
                    static_cast<size_t>(nv) * kd * vd * 2)))
      return s;
    if (!(s = alloc(reinterpret_cast<void**>(&out->conv_state),
                    static_cast<size_t>(in_qkv) * (conv_k - 1) * 2)))
      return s;
    cudaMemset(out->ssm_state, 0, static_cast<size_t>(nv) * kd * vd * 2);
    cudaMemset(out->conv_state, 0,
               static_cast<size_t>(in_qkv) * (conv_k - 1) * 2);
  }

  // 3. MoE (routed NVFP4 + BF16 router/shared).
  s = quant::LoadMoEWeights(loader, layer_id, E, hs, moe_is, &out->routed,
                            stream);
  if (!s.ok()) return s;
  s = LoadMoEExtra(loader, base + ".mlp", E, hs, shared_is, &out->mlp, stream);
  if (!s.ok()) return s;

  return Status();
}

Status DecoderLayerForward(const DecoderLayer& layer,
                           const uint16_t* hyper_input, uint16_t* out,
                           const int* positions, int T, void* workspace,
                           size_t workspace_bytes, cudaStream_t stream) {
  const int hs = layer.hs, hc_dim = layer.hc_dim;
  if (T <= 0) return Status();

  // Carve the workspace into per-submodule regions.
  const size_t attn_ws = AttnWs(T, layer.is_full_attention);
  const size_t moe_carve = MoEForwardWorkspaceBytes(
      T, layer.topk, hs, layer.routed.moe_is, layer.mlp.shared_is, layer.routed.E);
  if (attn_ws + moe_carve + kGemmWs + kGemmWs > workspace_bytes) {
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

  // 1. attn_hc.mix(hyper_input) -> mixed_attn, res_a.
  s = HyperConnectionMix(layer.attn_hc, hyper_input, d_mixed, d_res_a, T,
                         d_hc_ws, kGemmWs, stream);
  if (!s.ok()) {
    free_all();
    return s;
  }
  // 2. attention block.
  if (layer.is_full_attention) {
    s = FullAttentionForward(layer.full, d_mixed, d_block, positions,
                             layer.kv_cache, layer.idx_raw, layer.idx_comp, T,
                             d_attn_ws, attn_ws, stream);
  } else {
    s = LinearAttentionForward(layer.linear, d_mixed, d_block, layer.ssm_state,
                               layer.conv_state, T, d_attn_ws, attn_ws, stream);
  }
  if (!s.ok()) {
    free_all();
    return s;
  }
  // 3. attn_hc.combine(attn_out, hyper_input, res_a) -> combined_a.
  s = HyperConnectionCombine(layer.attn_hc, d_block, hyper_input, d_res_a,
                             d_combined, T, d_hc_ws, kGemmWs, stream);
  if (!s.ok()) {
    free_all();
    return s;
  }
  // 4. mlp_hc.mix(combined_a) -> mixed_mlp, res_m.
  s = HyperConnectionMix(layer.mlp_hc, d_combined, d_mixed, d_res_m, T,
                         d_hc_ws, kGemmWs, stream);
  if (!s.ok()) {
    free_all();
    return s;
  }
  // 5. MoE.
  s = MoEForward(d_mixed, layer.routed, layer.mlp, d_block, T, layer.topk,
                 d_moe_ws, moe_carve, d_moe_gemm, kGemmWs, stream);
  if (!s.ok()) {
    free_all();
    return s;
  }
  // 6. mlp_hc.combine(mlp_out, combined_a, res_m) -> out.
  s = HyperConnectionCombine(layer.mlp_hc, d_block, d_combined, d_res_m, out, T,
                             d_hc_ws, kGemmWs, stream);
  free_all();
  return s;
}

}  // namespace model
}  // namespace q4t
