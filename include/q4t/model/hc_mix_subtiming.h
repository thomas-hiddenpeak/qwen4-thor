// Observation-only per-HC-mix-step GPU timing for the hyper-connection mix.
// Gated by Q4T_LAYER_SUBTIMING (shared with the layer sub-timing). Accumulates
// each of the seven HC mix steps across all layers and reports once per
// forward. Never touches compute; off by default.
#pragma once

#include <cuda_runtime.h>

#include <cstdio>
#include <vector>

namespace q4t::model {

class HcMixSubTiming {
 public:
  // 0=norm, 1=down_gemm, 2=silu, 3=up_gemm, 4=gate, 5=inject, 6=combine
  static constexpr int kOps = 7;
  static HcMixSubTiming& Instance() {
    static HcMixSubTiming inst;
    return inst;
  }
  void Begin() {
    if (active_) return;
    active_ = true;
    pairs_.clear();
    last_recorded_ = nullptr;
  }
  bool active() const { return active_; }
  void Start(int op, cudaStream_t stream) {
    if (!active_ || op < 0 || op >= kOps) return;
    cudaEvent_t a = nullptr, b = nullptr;
    if (cudaEventCreate(&a) != cudaSuccess ||
        cudaEventCreate(&b) != cudaSuccess) {
      if (a) cudaEventDestroy(a);
      if (b) cudaEventDestroy(b);
      return;
    }
    cudaEventRecord(a, stream);
    last_recorded_ = a;
    pairs_.push_back(Pair{a, b, op});
  }
  void Stop(int op, cudaStream_t stream) {
    if (!active_) return;
    for (auto it = pairs_.rbegin(); it != pairs_.rend(); ++it) {
      if (it->op == op && !it->stopped) {
        cudaEventRecord(it->b, stream);
        last_recorded_ = it->b;
        it->stopped = true;
        return;
      }
    }
  }
  void Report(int T) {
    if (!active_) return;
    active_ = false;
    if (!pairs_.empty()) {
      const cudaError_t se = cudaEventSynchronize(last_recorded_);
      if (se != cudaSuccess) {
        std::fprintf(stderr, "[hc-mix-subtiming] sync failed: %s\n",
                     cudaGetErrorString(se));
        cudaGetLastError();
      } else {
        double sum[kOps] = {0};
        for (const auto& p : pairs_) {
          float ms = 0.0f;
          cudaEventElapsedTime(&ms, p.a, p.b);
          sum[p.op] += ms;
        }
        std::fprintf(stderr,
                     "[hc-mix-subtiming] T=%d norm=%.2f down_gemm=%.2f "
                     "silu=%.2f up_gemm=%.2f gate=%.2f inject=%.2f "
                     "combine=%.2f total=%.2f ms\n",
                     T, sum[0], sum[1], sum[2], sum[3], sum[4], sum[5],
                     sum[6], sum[0] + sum[1] + sum[2] + sum[3] + sum[4] +
                         sum[5] + sum[6]);
      }
    }
    for (auto& p : pairs_) {
      cudaEventDestroy(p.a);
      cudaEventDestroy(p.b);
    }
    pairs_.clear();
  }

 private:
  struct Pair {
    cudaEvent_t a, b;
    int op;
    bool stopped = false;
  };
  std::vector<Pair> pairs_;
  bool active_ = false;
  cudaEvent_t last_recorded_ = nullptr;
};

}  // namespace q4t::model
