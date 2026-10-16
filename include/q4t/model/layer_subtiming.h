// Observation-only per-sub-operation GPU timing for decoder layers.
// Gated by Q4T_LAYER_SUBTIMING. Sums each sub-op across all layers and
// reports once per forward (embedding/attention/MoE/HC breakdown). Never
// touches compute; off by default.
#pragma once

#include <cstdint>

#include <cuda_runtime.h>

#include <cstdio>
#include <vector>

namespace q4t {
namespace model {

// Sub-op slots: 0=ple, 1=gr_read(attn_hc), 2=attention, 3=write_and_read
// (mlp_hc), 4=moe, 5=write(final). Detail slots 6..11 are NESTED inside
// slot 2 (attention) and split it: linear layers 6=lin_inproj (in_proj
// GEMMs), 7=lin_state (conv+GDN+normgate), 8=lin_outproj; full layers
// 9=full_qkv (q/k/v projections), 10=full_core (QSA indexer + sparse
// attention), 11=full_oproj. Detail slots are diagnostic only: they do
// not sum to the top-level total.
struct LayerSubTiming {
  static constexpr int kOps = 12;
  std::vector<cudaEvent_t> ev;
  // Per (layer, op) pair: set when the end event has been recorded. Detail
  // slots are nested inside slot 2 (array order != stream order), and ops
  // not applicable to a layer type are never recorded. cudaEventElapsedTime
  // on an unrecorded event returns cudaErrorInvalidResourceHandle, which
  // would poison the next cudaGetLastError() check (e.g. a later kernel
  // launch), so elapsed time is only computed for recorded pairs.
  std::vector<uint8_t> pair_done;
  int num_layers = 0;
  bool active = false;
  // Index of the most recently RECORDED event; Report syncs this (not the
  // last array element) so no pending event is ever destroyed.
  size_t last_recorded = 0;

  static LayerSubTiming& Instance() {
    static LayerSubTiming inst;
    return inst;
  }
  void Begin(int nl) {
    if (active) return;
    num_layers = nl;
    last_recorded = 0;
    ev.resize(2 * static_cast<size_t>(kOps) * nl);
    pair_done.assign(static_cast<size_t>(nl) * kOps, 0);
    for (auto& e : ev)
      if (cudaEventCreate(&e) != cudaSuccess) {
        for (auto& f : ev)
          if (f) cudaEventDestroy(f);
        ev.clear();
        pair_done.clear();
        return;
      }
    active = true;
  }
  void Mark(int layer, int op, int which, cudaStream_t stream) {
    if (!active) return;
    if (layer < 0 || layer >= num_layers || op < 0 || op >= kOps) return;
    const size_t idx = 2 * (static_cast<size_t>(layer) * kOps + op) + which;
    cudaEventRecord(ev[idx], stream);
    last_recorded = idx;
    if (which == 1) pair_done[static_cast<size_t>(layer) * kOps + op] = 1;
  }
  // Sync the last recorded event, print per-sub-op totals, destroy events.
  void Report(int T) {
    if (!active) return;
    active = false;
    const cudaError_t se = cudaEventSynchronize(ev[last_recorded]);
    if (se != cudaSuccess) {
      std::fprintf(stderr, "[sub-timing] sync failed: %s\n",
                   cudaGetErrorString(se));
      cudaGetLastError();
    } else {
      double sum[kOps] = {0};
      for (int l = 0; l < num_layers; ++l)
        for (int o = 0; o < kOps; ++o) {
          if (!pair_done[static_cast<size_t>(l) * kOps + o]) continue;
          float ms = 0.0f;
          cudaEventElapsedTime(&ms, ev[2 * (static_cast<size_t>(l) * kOps + o)],
                               ev[2 * (static_cast<size_t>(l) * kOps + o) + 1]);
          sum[o] += ms;
        }
      std::fprintf(stderr,
                   "[sub-timing] T=%d ple=%.2f grread=%.2f attn=%.2f "
                   "wrar=%.2f moe=%.2f write=%.2f total=%.2f ms "
                   "[detail lin_inproj=%.2f lin_state=%.2f "
                   "lin_outproj=%.2f full_qkv=%.2f full_core=%.2f "
                   "full_oproj=%.2f]\n",
                   T, sum[0], sum[1], sum[2], sum[3], sum[4], sum[5],
                   sum[0] + sum[1] + sum[2] + sum[3] + sum[4] + sum[5],
                   sum[6], sum[7], sum[8], sum[9], sum[10], sum[11]);
    }
    for (auto& e : ev) cudaEventDestroy(e);
    ev.clear();
    pair_done.clear();
  }
};

}  // namespace model
}  // namespace q4t
