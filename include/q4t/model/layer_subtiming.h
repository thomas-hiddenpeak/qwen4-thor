// Observation-only per-sub-operation GPU timing for decoder layers.
// Gated by Q4T_LAYER_SUBTIMING. Sums each sub-op across all layers and
// reports once per forward (embedding/attention/MoE/HC breakdown). Never
// touches compute; off by default.
#pragma once

#include <cstdint>

#include <cuda_runtime.h>

#include <cmath>
#include <cstdio>
#include <cstring>
#include <vector>

namespace q4t {
namespace model {

inline bool QsaSubTimingRequested(bool layer_timing, int tokens,
                                  bool checkpointed, const char* value) {
  return layer_timing && tokens > 8 && !checkpointed && value &&
         std::strcmp(value, "1") == 0;
}

// Sub-op slots: 0=ple, 1=gr_read(attn_hc), 2=attention, 3=write_and_read
// (mlp_hc), 4=moe, 5=write(final). Detail slots 6..11 are NESTED inside
// slot 2 (attention) and split it: linear layers 6=lin_inproj (in_proj
// GEMMs), 7=lin_state (conv+GDN+normgate), 8=lin_outproj; full layers
// 9=full_qkv (q/k/v projections), 10=full_core (QSA indexer + sparse
// attention), 11=full_oproj. Detail slots are diagnostic only: they do
// not sum to the top-level total. Optional slots 12..16 further split full_core
// into main prepare, index projections, index prepare, score/select and sparse
// attention. They require Q4T_QSA_SUBTIMING=1 and a non-checkpointed large-T
// forward. T alone does not identify pure prefill versus a mixed batch.
struct LayerSubTiming {
  static constexpr int kBaseOps = 12;
  static constexpr int kOps = 17;
  std::vector<cudaEvent_t> ev;
  // Per (layer, op) pair: set when the end event has been recorded. Detail
  // slots are nested inside slot 2 (array order != stream order), and ops
  // not applicable to a layer type are never recorded. cudaEventElapsedTime
  // on an unrecorded event returns cudaErrorInvalidResourceHandle, which
  // would poison the next cudaGetLastError() check (e.g. a later kernel
  // launch), so elapsed time is only computed for recorded pairs.
  std::vector<uint8_t> pair_done;
  int num_layers = 0;
  int op_count = kBaseOps;
  int qsa_expected_layers = 0;
  bool qsa_valid = true;
  bool active = false;
  // Index of the most recently RECORDED event; Report syncs this (not the
  // last array element) so no pending event is ever destroyed.
  size_t last_recorded = 0;

  static LayerSubTiming& Instance() {
    static LayerSubTiming inst;
    return inst;
  }
  void Begin(int nl, int expected_qsa_layers = 0) {
    if (active) return;
    num_layers = nl;
    qsa_expected_layers = expected_qsa_layers;
    op_count = expected_qsa_layers > 0 ? kOps : kBaseOps;
    qsa_valid = true;
    last_recorded = 0;
    ev.resize(2 * static_cast<size_t>(op_count) * nl);
    pair_done.assign(static_cast<size_t>(nl) * op_count, 0);
    for (auto& e : ev)
      if (cudaEventCreate(&e) != cudaSuccess) {
        for (auto& f : ev)
          if (f) cudaEventDestroy(f);
        ev.clear();
        pair_done.clear();
        qsa_valid = false;
        if (qsa_expected_layers > 0)
          std::fprintf(stderr, "[qsa-subtiming] valid=0 error=create_event\n");
        return;
      }
    active = true;
  }
  void Mark(int layer, int op, int which, cudaStream_t stream) {
    if (!active) return;
    if (layer < 0 || layer >= num_layers || op < 0 || op >= op_count) return;
    const size_t pair = static_cast<size_t>(layer) * op_count + op;
    const size_t idx = 2 * pair + which;
    const cudaError_t status = cudaEventRecord(ev[idx], stream);
    if (op >= kBaseOps) {
      if (status != cudaSuccess) {
        qsa_valid = false;
        return;
      }
      // Both boundaries must exist for every QSA sub-operation.
      pair_done[pair] |= which == 0 ? 1 : 2;
    } else if (which == 1) {
      pair_done[pair] = 1;
    }
    last_recorded = idx;
  }
  // Sync the last recorded event, print per-sub-op totals, destroy events.
  void Report(int T) {
    if (!active) return;
    active = false;
    const cudaError_t se = cudaEventSynchronize(ev[last_recorded]);
    if (se != cudaSuccess) {
      qsa_valid = false;
      std::fprintf(stderr, "[sub-timing] sync failed: %s\n",
                   cudaGetErrorString(se));
      cudaGetLastError();
      if (qsa_expected_layers > 0)
        std::fprintf(stderr, "[qsa-subtiming] T=%d valid=0 error=sync\n", T);
    } else {
      double sum[kOps] = {0};
      for (int l = 0; l < num_layers; ++l)
        for (int o = 0; o < op_count; ++o) {
          const size_t pair = static_cast<size_t>(l) * op_count + o;
          if (!pair_done[pair]) continue;
          if (o >= kBaseOps && pair_done[pair] != 3) {
            qsa_valid = false;
            continue;
          }
          float ms = 0.0f;
          const cudaError_t elapsed =
              cudaEventElapsedTime(&ms, ev[2 * pair], ev[2 * pair + 1]);
          if (o >= kBaseOps &&
              (elapsed != cudaSuccess || !std::isfinite(ms) || ms < 0.f)) {
            qsa_valid = false;
            continue;
          }
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
      if (qsa_expected_layers > 0) {
        int complete_layers = 0;
        for (int l = 0; l < num_layers; ++l) {
          bool complete = true;
          for (int o = kBaseOps; o < kOps; ++o)
            complete &= pair_done[static_cast<size_t>(l) * op_count + o] == 3;
          complete_layers += complete;
        }
        qsa_valid &= complete_layers == qsa_expected_layers;
        if (qsa_valid) {
          std::fprintf(stderr,
                       "[qsa-subtiming] T=%d valid=1 layers=%d "
                       "prepare=%.3f index_proj=%.3f index_prepare=%.3f "
                       "score_select=%.3f sparse=%.3f total=%.3f ms\n",
                       T, complete_layers, sum[12], sum[13], sum[14], sum[15],
                       sum[16],
                       sum[12] + sum[13] + sum[14] + sum[15] + sum[16]);
        } else {
          std::fprintf(stderr,
                       "[qsa-subtiming] T=%d valid=0 layers=%d expected=%d "
                       "error=incomplete_or_failed_events\n",
                       T, complete_layers, qsa_expected_layers);
        }
      }
    }
    for (auto& e : ev) cudaEventDestroy(e);
    ev.clear();
    pair_done.clear();
  }

  // An early model error must not leave a partial prefill measurement alive
  // for the next forward. CUDA defers event destruction while it is in use.
  void DiscardQsa() {
    if (!active || qsa_expected_layers <= 0) return;
    active = false;
    qsa_valid = false;
    std::fprintf(stderr, "[qsa-subtiming] valid=0 error=forward_aborted\n");
    for (auto& e : ev) cudaEventDestroy(e);
    ev.clear();
    pair_done.clear();
  }
};

// Only the new explicit QSA mode opts into this early-return cleanup.
struct QsaSubTimingGuard {
  bool enabled = false;
  ~QsaSubTimingGuard() {
    if (enabled) LayerSubTiming::Instance().DiscardQsa();
  }
};

}  // namespace model
}  // namespace q4t
