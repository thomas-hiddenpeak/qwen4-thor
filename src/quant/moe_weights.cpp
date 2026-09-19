// NVFP4 routed-expert MoE weight loader (direct-to-packed).
#include "q4t/quant/moe_weights.h"
#include <chrono>
#include <cstdio>
#include <cstdlib>

#include <cuda_runtime.h>

#include <array>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>

namespace q4t {
namespace quant {

namespace {

// Build the checkpoint tensor name for one expert projection/suffix.
std::string ExpertName(int layer_id, int expert, const char* proj,
                       const char* suffix) {
  return "model.language_model.layers." + std::to_string(layer_id) +
         ".mlp.experts." + std::to_string(expert) + "." + proj + "." + suffix;
}

// H2D-copy a host buffer to a device pointer, returning a Status on error.
Status H2D(const void* src, void* dst, size_t bytes, cudaStream_t stream) {
  if (bytes == 0) return Status();
  if (cudaMemcpyAsync(dst, src, bytes, cudaMemcpyHostToDevice, stream) !=
      cudaSuccess) {
    return Status::Fail(std::string("cudaMemcpyAsync failed: ") +
                        cudaGetErrorString(cudaGetLastError()));
  }
  return Status();
}

}  // namespace

Status LoadMoEWeights(const io::WeightLoader& loader, int layer_id, int E,
                      int hs, int moe_is, MoEWeightLayout* out,
                      cudaStream_t stream) {
  if (E <= 0 || hs <= 0 || moe_is <= 0) {
    return Status::Fail("invalid MoE dims");
  }
  if ((hs % 32) != 0 || (moe_is % 32) != 0) {
    return Status::Fail("hs and moe_is must be multiples of 32 (FP4 GEMM)");
  }
  // Per-expert SF blocks require the row counts to be multiples of the 128-row
  // swizzle atom so each expert's block is a valid standalone swizzled buffer.
  if ((2 * moe_is) % 128 != 0 || (hs % 128) != 0) {
    return Status::Fail(
        "2*moe_is and hs must be multiples of 128 (per-expert SF blocks)");
  }

  out->E = E;
  out->hs = hs;
  out->moe_is = moe_is;

  const size_t gu_sf_block = SfBufferSize(2 * moe_is, hs);
  const size_t dn_sf_block = SfBufferSize(hs, moe_is);
  const size_t gu_packed_bytes = static_cast<size_t>(2 * E * moe_is) * (hs / 2);
  const size_t dn_packed_bytes = static_cast<size_t>(E * hs) * (moe_is / 2);
  const size_t scal_bytes = static_cast<size_t>(E) * sizeof(float);

  auto alloc = [&](void** p, size_t bytes) -> Status {
    if (cudaMalloc(p, bytes) != cudaSuccess) {
      return Status::Fail(std::string("cudaMalloc failed (") +
                          std::to_string(bytes) + " bytes)");
    }
    return Status();
  };
  Status s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->gu_packed), gu_packed_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->gu_sf),
                  static_cast<size_t>(E) * gu_sf_block)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->dn_packed), dn_packed_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->dn_sf),
                  static_cast<size_t>(E) * dn_sf_block)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->gu_w_scale2), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->gu_input_scale), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->dn_w_scale2), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->dn_input_scale), scal_bytes)))
    return s;

  out->gu_w_scale2_h.resize(E);
  out->gu_input_scale_h.resize(E);
  out->dn_w_scale2_h.resize(E);
  out->dn_input_scale_h.resize(E);

  // Per-expert staging sizes (shared by all worker threads).
  const size_t gu_w_bytes = static_cast<size_t>(moe_is) * (hs / 2);
  const size_t gu_s_bytes = static_cast<size_t>(moe_is) * (hs / 16);
  const size_t dn_w_bytes = static_cast<size_t>(hs) * (moe_is / 2);

  // Parallel expert load. The serial path spent ~49 s (of a 58 s load) on
  // 48 layers x 512 experts x ~10 small ReadTensor calls (string-name lookup +
  // mmap page fault + memcpy) on ONE thread (~1.2 GB/s). Each expert writes to
  // a disjoint device offset and uses its own thread-local staging, so the
  // experts are embarrassingly parallel; a pool of min(hw, E) threads drives
  // the host-side reads at aggregate memory bandwidth. This also fixes a
  // latent async race in the old loop: the per-expert swizzle vectors were
  // destroyed at iteration end while their H2D copies were still in flight on
  // `stream` (use-after-free), masked only by timing. Per-thread staging + a
  // single sync at the end removes it.
  int nthreads = std::max(
      1, std::min<int>(std::thread::hardware_concurrency(), E));
  if (const char* env = std::getenv("Q4T_MOE_THREADS")) {
    const int v = std::atoi(env);
    if (v > 0) nthreads = std::min(v, E);
  }
  std::atomic<int> next_expert{0};
  std::atomic<int> first_err{0};  // 0 = ok, else cudaGetLastError / -1
  const bool lt = std::getenv("Q4T_LOAD_TIMING") != nullptr;
  auto nowms = [] {
    return std::chrono::duration<double, std::milli>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
  };
  const double t_par0 = lt ? nowms() : 0.0;
  // Per-thread phase accumulators [read, h2d, swizzle]; summed after join.
  std::vector<std::array<double, 3>> phase_acc(nthreads, {0.0, 0.0, 0.0});
  std::vector<std::thread> pool;
  pool.reserve(nthreads);
  for (int t = 0; t < nthreads; ++t) {
    pool.emplace_back([&, t]() {
      // Thread-local staging (no sharing across threads).
      std::vector<uint8_t> gu_w(gu_w_bytes);
      std::vector<uint8_t> up_w(gu_w_bytes);
      std::vector<uint8_t> dn_w(dn_w_bytes);
      std::vector<uint8_t> gate_s(gu_s_bytes);
      std::vector<uint8_t> up_s(gu_s_bytes);
      std::vector<uint8_t> gu_s_merged(2 * gu_s_bytes);
      std::vector<uint8_t> dn_s(static_cast<size_t>(hs) * (moe_is / 16));
      std::vector<uint8_t> gu_sw(gu_sf_block);
      std::vector<uint8_t> dn_sw(dn_sf_block);
      float scal[4];  // [gu_ws2, gu_isc, dn_ws2, dn_isc]
      while (true) {
        if (first_err.load(std::memory_order_relaxed) != 0) return;
        const int e = next_expert.fetch_add(1, std::memory_order_relaxed);
        if (e >= E) return;
        Status es;
        std::string n;
        // --- Phase 1: host reads of the three packed weights ---
        const double t0 = lt ? nowms() : 0.0;
        n = ExpertName(layer_id, e, "gate_proj", "weight");
        if (!(es = loader.ReadTensor(n, gu_w.data()))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        n = ExpertName(layer_id, e, "up_proj", "weight");
        if (!(es = loader.ReadTensor(n, up_w.data()))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        n = ExpertName(layer_id, e, "down_proj", "weight");
        if (!(es = loader.ReadTensor(n, dn_w.data()))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (lt) phase_acc[t][0] += nowms() - t0;
        // --- Phase 2: submit weight H2D (async on stream) ---
        const double t1 = lt ? nowms() : 0.0;
        uint8_t* gu_dst = out->gu_packed_expert(e);
        if (!(es = H2D(gu_w.data(), gu_dst, gu_w_bytes, stream))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (!(es = H2D(up_w.data(), gu_dst + moe_is * (hs / 2), gu_w_bytes,
                       stream))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        uint8_t* dn_dst = out->dn_packed_expert(e);
        if (!(es = H2D(dn_w.data(), dn_dst, dn_w_bytes, stream))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (lt) phase_acc[t][1] += nowms() - t1;
        // --- Phase 3: host reads of the three weight_scales ---
        const double t2 = lt ? nowms() : 0.0;
        n = ExpertName(layer_id, e, "gate_proj", "weight_scale");
        if (!(es = loader.ReadTensor(n, gate_s.data()))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        n = ExpertName(layer_id, e, "up_proj", "weight_scale");
        if (!(es = loader.ReadTensor(n, up_s.data()))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        n = ExpertName(layer_id, e, "down_proj", "weight_scale");
        if (!(es = loader.ReadTensor(n, dn_s.data()))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (lt) phase_acc[t][0] += nowms() - t2;
        // --- Phase 4: merge + swizzle both scale blocks (host compute) ---
        const double t3 = lt ? nowms() : 0.0;
        std::memcpy(gu_s_merged.data(), gate_s.data(), gu_s_bytes);
        std::memcpy(gu_s_merged.data() + gu_s_bytes, up_s.data(), gu_s_bytes);
        SwizzleSfInto(gu_s_merged.data(), 2 * moe_is, hs, gu_sw.data());
        SwizzleSfInto(dn_s.data(), hs, moe_is, dn_sw.data());
        if (lt) phase_acc[t][2] += nowms() - t3;
        // --- Phase 5: submit scale H2D ---
        const double t4 = lt ? nowms() : 0.0;
        if (!(es = H2D(gu_sw.data(), out->gu_sf_expert(e), gu_sf_block,
                       stream))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (!(es = H2D(dn_sw.data(), out->dn_sf_expert(e), dn_sf_block,
                       stream))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (lt) phase_acc[t][1] += nowms() - t4;
        // --- Phase 6: host reads of the four scalar scales ---
        const double t5 = lt ? nowms() : 0.0;
        n = ExpertName(layer_id, e, "gate_proj", "weight_scale_2");
        if (!(es = loader.ReadTensor(n, &scal[0]))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        n = ExpertName(layer_id, e, "gate_proj", "input_scale");
        if (!(es = loader.ReadTensor(n, &scal[1]))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        n = ExpertName(layer_id, e, "down_proj", "weight_scale_2");
        if (!(es = loader.ReadTensor(n, &scal[2]))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        n = ExpertName(layer_id, e, "down_proj", "input_scale");
        if (!(es = loader.ReadTensor(n, &scal[3]))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (lt) phase_acc[t][0] += nowms() - t5;
        out->gu_w_scale2_h[e] = scal[0];
        out->gu_input_scale_h[e] = scal[1];
        out->dn_w_scale2_h[e] = scal[2];
        out->dn_input_scale_h[e] = scal[3];
        // --- Phase 7: submit scalar H2D ---
        const double t6 = lt ? nowms() : 0.0;
        if (!(es = H2D(&scal[0], out->gu_w_scale2 + e, sizeof(float),
                       stream))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (!(es = H2D(&scal[1], out->gu_input_scale + e, sizeof(float),
                       stream))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (!(es = H2D(&scal[2], out->dn_w_scale2 + e, sizeof(float),
                       stream))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (!(es = H2D(&scal[3], out->dn_input_scale + e, sizeof(float),
                       stream))) {
          first_err.store(-1, std::memory_order_relaxed);
          return;
        }
        if (lt) phase_acc[t][1] += nowms() - t6;
      }
    });
  }
  for (auto& th : pool) th.join();
  if (first_err.load() != 0) {
    return Status::Fail("parallel MoE expert load failed (layer " +
                        std::to_string(layer_id) + ")");
  }
  if (lt) {
    double sum_read = 0, sum_h2d = 0, sum_sw = 0;
    for (const auto& a : phase_acc) {
      sum_read += a[0];
      sum_h2d += a[1];
      sum_sw += a[2];
    }
    std::fprintf(stderr,
                 "[q4t][moe] layer %d: experts=%d threads=%d parallel=%.0f "
                 "ms (read=%.0f h2d=%.0f swizzle=%.0f, sum=%.0f)\n",
                 layer_id, E, nthreads, nowms() - t_par0, sum_read, sum_h2d,
                 sum_sw, sum_read + sum_h2d + sum_sw);
  }

  if (stream != nullptr) {
    if (cudaStreamSynchronize(stream) != cudaSuccess) {
      return Status::Fail(std::string("stream sync failed: ") +
                          cudaGetErrorString(cudaGetLastError()));
    }
  }
  return Status();
}

}  // namespace quant
}  // namespace q4t
