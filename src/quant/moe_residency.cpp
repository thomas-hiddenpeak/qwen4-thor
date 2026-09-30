// Per-layer tiered expert residency (see moe_residency.h).
#include "q4t/quant/moe_residency.h"

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <chrono>
#include <cstring>
#include <thread>

#include "q4t/quant/swizzle.h"

namespace q4t {
namespace quant {

namespace {

// "first" fault hook: exactly one request-time stage across ALL layers
// fails. A per-layer one-shot would fire once per layer (each layer's
// first stage), so a second request would hit the next layer's armed
// hook and the recovery contract (step 2 must succeed) would break.
std::atomic<bool> g_first_fault_fired{false};

std::string ExpertName(int layer_id, int expert, const char* proj,
                       const char* suffix) {
  return "model.language_model.layers." + std::to_string(layer_id) +
         ".mlp.experts." + std::to_string(expert) + "." + proj + "." + suffix;
}

Status H2D(const void* src, void* dst, size_t bytes, cudaStream_t stream) {
  if (bytes == 0) return Status();
  if (cudaMemcpyAsync(dst, src, bytes, cudaMemcpyHostToDevice, stream) !=
      cudaSuccess) {
    return Status::Fail(std::string("residency H2D failed: ") +
                        cudaGetErrorString(cudaGetLastError()));
  }
  return Status();
}

}  // namespace

int MoEResidencyLoadThreads() {
  // Default 8: the range-read fast path issues few, large, contiguous
  // preads per expert, so more parallel workers saturate the NVMe without
  // the random-read latency pile-up the old 10-small-read path had.
  int n = 8;
  if (const char* env = std::getenv("Q4T_MOE_LOAD_THREADS")) {
    const int v = std::atoi(env);
    if (v > 0) n = v;
  }
  return std::max(1, std::min(n, MoEResidency::kMaxLoadThreads));
}

int MoEResidencyL2Slots() {
  // Default 128: the steady-state decode working set beyond the C GPU slots
  // is tens of experts per layer (measured miss rates imply ~16-44), so 128
  // keeps it warm with margin; prefill sub-chunks (distinct miss set up to
  // the dynamic capacity) also benefit. The floor is the load worker count
  // so every worker can own a distinct buffer in flight.
  int n = 128;
  const char* env = std::getenv("Q4T_MOE_L2_SLOTS");
  if (env) {
    const int v = std::atoi(env);
    if (v > 0) n = v;
  }
  return std::max(MoEResidencyLoadThreads(), std::min(n, 512));
}

size_t MoEResidencyLayerBytes(int hs, int moe_is, int C) {
  const size_t gu_sf_block = SfBufferSize(2 * moe_is, hs);
  const size_t dn_sf_block = SfBufferSize(hs, moe_is);
  size_t b = 0;
  b += static_cast<size_t>(2 * C * moe_is) * (hs / 2);  // gu_packed
  b += static_cast<size_t>(C) * gu_sf_block;
  b += static_cast<size_t>(C * hs) * (moe_is / 2);  // dn_packed
  b += static_cast<size_t>(C) * dn_sf_block;
  b += static_cast<size_t>(4 * C) * sizeof(float);  // scalar scales
  return b;
}

size_t MoEResidencyStagingBytes(int hs, int moe_is) {
  const size_t gu_w_bytes = static_cast<size_t>(moe_is) * (hs / 2);
  const size_t gu_s_bytes = static_cast<size_t>(moe_is) * (hs / 16);
  const size_t dn_w_bytes = static_cast<size_t>(hs) * (moe_is / 2);
  const size_t dn_s_bytes = static_cast<size_t>(hs) * (moe_is / 16);
  return 2 * gu_w_bytes + dn_w_bytes + 2 * gu_s_bytes + dn_s_bytes +
         2 * gu_s_bytes + SfBufferSize(2 * moe_is, hs) +
         SfBufferSize(hs, moe_is) + 4 * sizeof(float);
}

Status MoEResidency::Init(const io::WeightLoader& loader, int layer_id,
                          int E, int hs, int moe_is, int C,
                          cudaStream_t stream) {
  if (E <= 0 || hs <= 0 || moe_is <= 0 || C <= 0 || C > E) {
    return Status::Fail("invalid residency dims");
  }
  if ((2 * moe_is) % 128 != 0 || (hs % 128) != 0) {
    return Status::Fail(
        "2*moe_is and hs must be multiples of 128 (per-expert SF blocks)");
  }
  loader_ = &loader;
  layer_id_ = layer_id;
  E_ = E;
  hs_ = hs;
  moe_is_ = moe_is;
  C_ = C;
  load_threads_ = MoEResidencyLoadThreads();

  layout_.E = C;
  layout_.hs = hs;
  layout_.moe_is = moe_is;
  layout_.slot_mode = true;

  const size_t gu_sf_block = SfBufferSize(2 * moe_is, hs);
  const size_t dn_sf_block = SfBufferSize(hs, moe_is);
  const size_t scal_bytes = static_cast<size_t>(C) * sizeof(float);
  auto alloc = [&](void** p, size_t bytes) -> Status {
    if (cudaMalloc(p, bytes) != cudaSuccess) {
      return Status::Fail(std::string("residency cudaMalloc failed (") +
                          std::to_string(bytes) + " bytes)");
    }
    return Status();
  };
  Status s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.gu_packed),
                  static_cast<size_t>(2 * C * moe_is) * (hs / 2))))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.gu_sf),
                  static_cast<size_t>(C) * gu_sf_block)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.dn_packed),
                  static_cast<size_t>(C * hs) * (moe_is / 2))))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.dn_sf),
                  static_cast<size_t>(C) * dn_sf_block)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.gu_w_scale2), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.gu_input_scale),
                  scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.dn_w_scale2), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.dn_input_scale),
                  scal_bytes)))
    return s;

  layout_.gu_w_scale2_h.assign(C, 0.0f);
  layout_.gu_input_scale_h.assign(C, 0.0f);
  layout_.dn_w_scale2_h.assign(C, 0.0f);
  layout_.dn_input_scale_h.assign(C, 0.0f);

  slot_expert_.assign(C, -1);
  slot_tick_.assign(C, 0);
  slot_protected_.assign(C, 0);
  expert_slot_.assign(E, -1);
  resident_count_ = 0;
  protected_count_ = 0;

  // One-shot test-only fault hook (off unless the env var is "first" or
  // names a valid expert): the matching stage fails, then the hook
  // disarms. "first" fires on the first request-time stage (after
  // InitHot), deterministic even when the armed expert is hot.
  fail_expert_ = -1;
  fail_first_ = false;
  fail_armed_ = false;
  const char* fail_env = std::getenv("Q4T_RESIDENCY_FAIL_EXPERT");
  if (fail_env) {
    if (std::strcmp(fail_env, "first") == 0) {
      fail_first_ = true;
      fail_armed_ = true;
    } else {
      const int v = std::atoi(fail_env);
      if (v >= 0 && v < E_) {
        fail_expert_ = v;
        fail_armed_ = true;
      }
    }
  }
  // Optional per-miss pipeline timing (diagnostic; default off).
  const char* tim_env = std::getenv("Q4T_RESIDENCY_TIMING");
  timing_.enabled.store(tim_env != nullptr && std::atoi(tim_env) != 0,
                        std::memory_order_relaxed);

  // Pinned L2 pool (see header): one block, L buffers. A buffer is never
  // rewritten until the H2D that last read it completed (per-buffer event),
  // and victim selection skips in-flight buffers.
  staging_bytes_ = MoEResidencyStagingBytes(hs, moe_is);
  l2_slots_ = MoEResidencyL2Slots();
  l2_block_ = nullptr;
  l2_buf_.assign(l2_slots_, nullptr);
  l2_expert_.assign(l2_slots_, -1);
  expert_l2buf_.assign(E_, -1);
  l2_tick_.assign(l2_slots_, 0);
  l2_in_flight_.assign(l2_slots_, false);
  l2_claimed_.assign(l2_slots_, false);
  l2_event_.assign(l2_slots_, nullptr);
  l2_mu_ = new (std::nothrow) std::mutex();
  if (!l2_mu_) {
    return Status::Fail("residency L2 mutex alloc failed");
  }
  if (cudaHostAlloc(reinterpret_cast<void**>(&l2_block_),
                    static_cast<size_t>(l2_slots_) * staging_bytes_,
                    cudaHostAllocDefault) != cudaSuccess) {
    delete l2_mu_;
    l2_mu_ = nullptr;
    return Status::Fail("residency L2 pinned alloc failed");
  }
  for (int b = 0; b < l2_slots_; ++b) {
    l2_buf_[b] = l2_block_ + static_cast<size_t>(b) * staging_bytes_;
    if (cudaEventCreateWithFlags(&l2_event_[b],
                                 cudaEventDisableTiming) != cudaSuccess) {
      cudaFreeHost(l2_block_);
      l2_block_ = nullptr;
      delete l2_mu_;
      l2_mu_ = nullptr;
      return Status::Fail("residency L2 event create failed");
    }
  }
  // Persistent load workers are created last so no Init failure path
  // leaks a running pool (Free only stops/joins when inited_).
  workers_.clear();
  for (int t = 0; t < load_threads_; ++t) {
    LoadWorker* w = new (std::nothrow) LoadWorker();
    if (!w) {
      for (auto* x : workers_) delete x;
      workers_.clear();
      return Status::Fail("residency load worker alloc failed");
    }
    workers_.push_back(w);
    w->th = std::thread([this, w] { LoadWorkerLoop(w); });
  }
  inited_ = true;
  (void)stream;
  return Status();
}

// Pick the LRU L2 buffer that is not in flight and not claimed by the
// current stage chunk. First pass also skips buffers whose expert is still
// needed by the current plan (evicting it would force a same-plan NVMe
// re-read); if every free buffer holds a needed expert, fall back to the
// plain LRU (the re-read is correct, just slower); if every buffer is
// in flight or claimed, wait for the LRU in-flight H2D to complete.
int MoEResidency::PickL2Victim(const std::vector<uint8_t>* needed_mark)
    const {
  const bool has_mark = needed_mark != nullptr && !needed_mark->empty();
  int v = -1;
  uint64_t best = UINT64_MAX;
  for (int b = 0; b < l2_slots_; ++b) {
    if (l2_in_flight_[b]) {
      // Lazy release: the H2D that set the flag may have completed.
      if (cudaEventQuery(l2_event_[b]) != cudaSuccess) continue;
      l2_in_flight_[b] = false;
    }
    if (l2_claimed_[b]) continue;
    const int e = l2_expert_[b];
    if (e >= 0 && has_mark && (*needed_mark)[e]) continue;
    if (l2_tick_[b] < best) {
      best = l2_tick_[b];
      v = b;
    }
  }
  if (v < 0) {
    // Fallback: evict even a still-needed expert (the same plan re-reads it
    // from NVMe in a later chunk; correct, just slower). Never an in-flight
    // or claimed buffer.
    best = UINT64_MAX;
    for (int b = 0; b < l2_slots_; ++b) {
      if (l2_in_flight_[b] || l2_claimed_[b]) continue;
      if (l2_tick_[b] < best) {
        best = l2_tick_[b];
        v = b;
      }
    }
  }
  if (v < 0) {
    // Every buffer is in flight or claimed: wait for the LRU in-flight
    // buffer's H2D to complete. Claimed buffers commit before the next
    // stage chunk, and l2_slots_ >= load_threads_, so at least one
    // in-flight buffer is always releasable here.
    best = UINT64_MAX;
    for (int b = 0; b < l2_slots_; ++b) {
      if (!l2_in_flight_[b]) continue;
      if (l2_tick_[b] < best) {
        best = l2_tick_[b];
        v = b;
      }
    }
    if (v >= 0 && cudaEventSynchronize(l2_event_[v]) == cudaSuccess) {
      l2_in_flight_[v] = false;
    } else {
      v = -1;
    }
  }
  return v;
}

// Stage one expert into an L2 buffer: an L2 hit reuses the cached payload
// (no NVMe read); an L2 miss evicts an LRU victim and NVMe-reads + merges +
// swizzles into it. *buf_out receives the buffer index.
namespace {
// Nanoseconds elapsed since t0 (0 when timing is off and t0 is empty).
uint64_t NowNs(std::chrono::steady_clock::time_point t0) {
  return static_cast<uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now() - t0)
          .count());
}
}  // namespace

void MoEResidency::RecordD2HSync(uint64_t ns) const {
  if (!timing_.enabled.load(std::memory_order_relaxed)) return;
  timing_.d2h_count.fetch_add(1, std::memory_order_relaxed);
  timing_.d2h_ns.fetch_add(ns, std::memory_order_relaxed);
  AtomicMaxU64(timing_.d2h_max_ns, ns);
}

void MoEResidency::RecordStageNs(uint64_t ns) const {
  if (!timing_.enabled.load(std::memory_order_relaxed)) return;
  timing_.stage_count.fetch_add(1, std::memory_order_relaxed);
  timing_.stage_ns.fetch_add(ns, std::memory_order_relaxed);
  AtomicMaxU64(timing_.stage_max_ns, ns);
}

void MoEResidency::RecordPhase1Ns(uint64_t ns) const {
  if (!timing_.enabled.load(std::memory_order_relaxed)) return;
  timing_.phase1_count.fetch_add(1, std::memory_order_relaxed);
  timing_.phase1_ns.fetch_add(ns, std::memory_order_relaxed);
  AtomicMaxU64(timing_.phase1_max_ns, ns);
}

Status MoEResidency::StageExpert(int expert,
                                 const std::vector<uint8_t>* needed_mark,
                                 int* buf_out) const {
  if (expert < 0 || expert >= E_) {
    return Status::Fail("expert out of range");
  }
  const bool tim = timing_.enabled.load(std::memory_order_relaxed);
  const auto t_stage0 =
      tim ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};
  // One-shot test-only fault hook: fail the first stage of the armed expert
  // before any L2/slot state is touched, then disarm. The plan-local slot
  // reservation is dropped with the plan, so a failed load leaves the slot
  // bookkeeping consistent (the next load of the expert is a clean miss).
  {
    std::lock_guard<std::mutex> lk(*l2_mu_);
    if (fail_armed_ && init_done_) {
      bool fire = false;
      if (fail_first_) {
        // Global one-shot: the first request-time stage in the whole
        // model fails, no matter which layer owns it.
        fire = !g_first_fault_fired.exchange(true,
                                             std::memory_order_acq_rel);
      } else {
        fire = expert == fail_expert_;
      }
      if (fire) {
        fail_armed_ = false;
        return Status::Fail("residency fault injection: expert " +
                            std::to_string(expert) + " (test hook)");
      }
    }
  }
  // Per-projection byte sizes; down/gate/up are equal by construction
  // (hs*moe_is/2 weights, hs*moe_is/16 SF).
  const size_t w_bytes = static_cast<size_t>(hs_) * (moe_is_ / 2);
  const size_t s_bytes = static_cast<size_t>(hs_) * (moe_is_ / 16);
  const size_t gu_sf_block = layout_.gu_sf_block();
  const size_t dn_sf_block = layout_.dn_sf_block();

  int b;
  bool hit = false;
  {
    std::lock_guard<std::mutex> lk(*l2_mu_);
    b = expert_l2buf_[expert];
    if (b >= 0) {
      // L2 hit: the buffer already holds this expert's payload; no read.
      // Claim it so a same-chunk miss cannot evict it before our commit.
      l2_tick_[b] = ++l2_recency_;
      l2_claimed_[b] = true;
      ++stats_.l2_hits;
      hit = true;
    } else {
      b = PickL2Victim(needed_mark);
      if (b < 0) {
        return Status::Fail("no residency L2 buffer available");
      }
      const int old = l2_expert_[b];
      if (old >= 0) {
        expert_l2buf_[old] = -1;
        ++stats_.l2_evictions;
      }
      l2_expert_[b] = expert;
      expert_l2buf_[expert] = b;
      l2_tick_[b] = ++l2_recency_;
      ++stats_.l2_misses;
    }
  }
  *buf_out = b;
  if (hit) {
    // The payload is already staged in the buffer; no read, merge, or
    // swizzle. A concurrent in-flight H2D only reads the buffer, and our
    // commit H2D is stream-ordered after it, so no wait is needed.
    if (tim) RecordStageNs(NowNs(t_stage0));
    return Status();
  }
  // A miss always picks a non-in-flight buffer, but keep the defensive wait
  // in case the flag went stale under us.
  if (l2_in_flight_[b]) {
    if (cudaEventSynchronize(l2_event_[b]) != cudaSuccess) {
      return Status::Fail("residency L2 wait failed");
    }
    l2_in_flight_[b] = false;
  }
  // L2 buffer is in CHECKPOINT file order so the range fast path can read
  // straight into it: weights [dn|ga|up] (w_bytes each), then SF
  // [dn|ga|up] packed at s_bytes each, the gate+up SF merge scratch, the
  // swizzled SF blocks, and the scalar scales. Total bytes equal
  // MoEResidencyStagingBytes (3*w + 5*s + sf blocks + 16); the SF region
  // must stay packed or the tail overflows the pinned buffer.
  uint8_t* p = l2_buf_[b];
  uint8_t* w_dn = p;
  uint8_t* w_ga = p + w_bytes;
  uint8_t* w_up = p + 2 * w_bytes;
  uint8_t* s_dn = p + 3 * w_bytes;
  uint8_t* s_ga = s_dn + s_bytes;
  uint8_t* s_up = s_ga + s_bytes;
  uint8_t* gu_s_merged = s_up + s_bytes;
  uint8_t* gu_sw = gu_s_merged + 2 * s_bytes;
  uint8_t* dn_sw = gu_sw + gu_sf_block;
  float* scal = reinterpret_cast<float*>(dn_sw + dn_sf_block);
  if (reinterpret_cast<uint8_t*>(scal) + 4 * sizeof(float) >
      p + staging_bytes_) {
    return Status::Fail("residency staging layout exceeds buffer");
  }

  Status s;
  const auto t_read0 =
      tim ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};
  // Fast path: the checkpoint keeps each expert's down/gate/up weights
  // contiguous (verified below per expert), so one pread fetches all three
  // (likewise the SF blocks and the six scalar scales). Fewer, larger,
  // contiguous reads are much faster on NVMe than ten small random reads.
  const std::string dn_w_name =
      ExpertName(layer_id_, expert, "down_proj", "weight");
  const io::TensorInfo* dn_w = loader_->FindTensor(dn_w_name);
  const io::TensorInfo* ga_w = loader_->FindTensor(
      ExpertName(layer_id_, expert, "gate_proj", "weight"));
  const io::TensorInfo* up_w = loader_->FindTensor(
      ExpertName(layer_id_, expert, "up_proj", "weight"));
  const io::TensorInfo* dn_s = loader_->FindTensor(
      ExpertName(layer_id_, expert, "down_proj", "weight_scale"));
  const io::TensorInfo* ga_s = loader_->FindTensor(
      ExpertName(layer_id_, expert, "gate_proj", "weight_scale"));
  const io::TensorInfo* up_s = loader_->FindTensor(
      ExpertName(layer_id_, expert, "up_proj", "weight_scale"));
  const bool w_ok = dn_w && ga_w && up_w &&
                    dn_w->byte_size() == w_bytes &&
                    ga_w->byte_size() == w_bytes &&
                    up_w->byte_size() == w_bytes &&
                    dn_w->data_end == ga_w->data_start &&
                    ga_w->data_end == up_w->data_start;
  const bool s_ok = dn_s && ga_s && up_s &&
                    dn_s->byte_size() == s_bytes &&
                    ga_s->byte_size() == s_bytes &&
                    up_s->byte_size() == s_bytes &&
                    dn_s->data_end == ga_s->data_start &&
                    ga_s->data_end == up_s->data_start;
  // Six scalar scales, file order: dn.input_scale, dn.ws2, ga.input_scale,
  // ga.ws2, up.input_scale, up.ws2 (each 4 B, contiguous when sc_ok).
  const char* sc_names[6] = {
      "down_proj.input_scale", "down_proj.weight_scale_2",
      "gate_proj.input_scale", "gate_proj.weight_scale_2",
      "up_proj.input_scale", "up_proj.weight_scale_2"};
  const io::TensorInfo* sc[6] = {nullptr};
  bool sc_ok = true;
  for (int i = 0; i < 6; ++i) {
    const std::string n = "model.language_model.layers." +
                          std::to_string(layer_id_) + ".mlp.experts." +
                          std::to_string(expert) + "." + sc_names[i];
    sc[i] = loader_->FindTensor(n);
    sc_ok = sc_ok && sc[i] != nullptr && sc[i]->byte_size() == sizeof(float);
  }
  if (sc_ok) {
    for (int i = 1; i < 6; ++i) {
      sc_ok = sc_ok && sc[i]->data_start == sc[0]->data_start + i * sizeof(float);
    }
  }
  if (w_ok && s_ok) {
    if (!(s = loader_->ReadRange(dn_w_name, dn_w->data_start, 3 * w_bytes,
                                 w_dn))) {
      return s;
    }
    stats_.nvme_read_bytes +=
        static_cast<double>(3 * w_bytes + 3 * s_bytes + 6 * sizeof(float));
    const std::string dn_s_name =
        ExpertName(layer_id_, expert, "down_proj", "weight_scale");
    if (!(s = loader_->ReadRange(dn_s_name, dn_s->data_start, 3 * s_bytes,
                                 s_dn))) {
      return s;
    }
    if (sc_ok) {
      float file_scal[6];
      if (!(s = loader_->ReadRange(
                "model.language_model.layers." +
                    std::to_string(layer_id_) + ".mlp.experts." +
                    std::to_string(expert) + ".down_proj.input_scale",
                sc[0]->data_start, 6 * sizeof(float), file_scal))) {
        return s;
      }
      scal[0] = file_scal[3];  // gate.weight_scale_2
      scal[1] = file_scal[2];  // gate.input_scale
      scal[2] = file_scal[1];  // down.weight_scale_2
      scal[3] = file_scal[0];  // down.input_scale
    } else {
      const char* sc4[4] = {"gate_proj.weight_scale_2",
                            "gate_proj.input_scale",
                            "down_proj.weight_scale_2",
                            "down_proj.input_scale"};
      for (int i = 0; i < 4; ++i) {
        const std::string n = "model.language_model.layers." +
                              std::to_string(layer_id_) + ".mlp.experts." +
                              std::to_string(expert) + "." + sc4[i];
        if (!(s = loader_->ReadTensor(n, &scal[i]))) return s;
      }
    }
  } else {
    // Legacy per-tensor path (bit-identical bytes, arbitrary layout).
    std::string n;
    n = ExpertName(layer_id_, expert, "down_proj", "weight");
    if (!(s = loader_->ReadTensor(n, w_dn))) return s;
    stats_.nvme_read_bytes +=
        static_cast<double>(3 * w_bytes + 3 * s_bytes + 4 * sizeof(float));
    n = ExpertName(layer_id_, expert, "gate_proj", "weight");
    if (!(s = loader_->ReadTensor(n, w_ga))) return s;
    n = ExpertName(layer_id_, expert, "up_proj", "weight");
    if (!(s = loader_->ReadTensor(n, w_up))) return s;
    n = ExpertName(layer_id_, expert, "down_proj", "weight_scale");
    if (!(s = loader_->ReadTensor(n, s_dn))) return s;
    n = ExpertName(layer_id_, expert, "gate_proj", "weight_scale");
    if (!(s = loader_->ReadTensor(n, s_ga))) return s;
    n = ExpertName(layer_id_, expert, "up_proj", "weight_scale");
    if (!(s = loader_->ReadTensor(n, s_up))) return s;
    const char* sc4[4] = {"gate_proj.weight_scale_2",
                          "gate_proj.input_scale",
                          "down_proj.weight_scale_2",
                          "down_proj.input_scale"};
    for (int i = 0; i < 4; ++i) {
      const std::string n = "model.language_model.layers." +
                            std::to_string(layer_id_) + ".mlp.experts." +
                            std::to_string(expert) + "." + sc4[i];
      if (!(s = loader_->ReadTensor(n, &scal[i]))) return s;
    }
  }

  if (tim) {
    const uint64_t rns = NowNs(t_read0);
    timing_.pread_count.fetch_add(1, std::memory_order_relaxed);
    timing_.pread_ns.fetch_add(rns, std::memory_order_relaxed);
    AtomicMaxU64(timing_.pread_max_ns, rns);
  }
  const auto t_swz0 =
      tim ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};
  // Merge gate+up scales then swizzle both blocks (identical to the
  // LoadMoEWeights per-expert path, so device bytes match bit-for-bit).
  std::memcpy(gu_s_merged, s_ga, s_bytes);
  std::memcpy(gu_s_merged + s_bytes, s_up, s_bytes);
  SwizzleSfInto(gu_s_merged, 2 * moe_is_, hs_, gu_sw);
  SwizzleSfInto(s_dn, hs_, moe_is_, dn_sw);
  if (tim) {
    const uint64_t sns = NowNs(t_swz0);
    timing_.swz_count.fetch_add(1, std::memory_order_relaxed);
    timing_.swz_ns.fetch_add(sns, std::memory_order_relaxed);
    AtomicMaxU64(timing_.swz_max_ns, sns);
    RecordStageNs(NowNs(t_stage0));
  }
  return Status();
}

// Commit one staged expert into its slot: H2D from its L2 buffer
// (stream-ordered after any earlier GEMM that read the evicted slot, before
// any GEMM that reads the new expert) plus host identity/scale bookkeeping.
Status MoEResidency::CommitExpert(int expert, int slot, int buf,
                                  cudaStream_t stream) const {
  if (expert < 0 || expert >= E_) {
    return Status::Fail("expert out of range");
  }
  if (slot < 0 || slot >= C_) {
    return Status::Fail("slot out of range");
  }
  const size_t w_bytes = static_cast<size_t>(hs_) * (moe_is_ / 2);
  const size_t s_bytes = static_cast<size_t>(hs_) * (moe_is_ / 16);
  const size_t gu_sf_block = layout_.gu_sf_block();
  const size_t dn_sf_block = layout_.dn_sf_block();

  // L2 buffer is in checkpoint file order (see StageExpert):
  // [w_dn|w_ga|w_up|s_dn|s_ga|s_up|gu_s_merged|gu_sw|dn_sw|scal] with the
  // SF region packed at s_bytes each (total = MoEResidencyStagingBytes).
  uint8_t* p = l2_buf_[buf];
  uint8_t* w_dn = p;
  uint8_t* w_ga = p + w_bytes;
  uint8_t* w_up = p + 2 * w_bytes;
  uint8_t* gu_sw = p + 3 * w_bytes + 5 * s_bytes;
  uint8_t* dn_sw = gu_sw + gu_sf_block;
  const float* scal = reinterpret_cast<const float*>(dn_sw + dn_sf_block);
  if (reinterpret_cast<const uint8_t*>(scal) + 4 * sizeof(float) >
      p + staging_bytes_) {
    return Status::Fail("residency staging layout exceeds buffer");
  }

  Status s;
  // H2D into the slot.
  uint8_t* gu_dst =
      layout_.gu_packed + static_cast<size_t>(slot) * moe_is_ * hs_;
  if (!(s = H2D(w_ga, gu_dst, w_bytes, stream))) return s;
  if (!(s = H2D(w_up, gu_dst + moe_is_ * (hs_ / 2), w_bytes, stream)))
    return s;
  uint8_t* dn_dst =
      layout_.dn_packed + static_cast<size_t>(slot) * hs_ * (moe_is_ / 2);
  if (!(s = H2D(w_dn, dn_dst, w_bytes, stream))) return s;
  if (!(s = H2D(gu_sw, layout_.gu_sf + static_cast<size_t>(slot) * gu_sf_block,
                gu_sf_block, stream)))
    return s;
  if (!(s = H2D(dn_sw, layout_.dn_sf + static_cast<size_t>(slot) * dn_sf_block,
                dn_sf_block, stream)))
    return s;
  if (!(s = H2D(&scal[0], layout_.gu_w_scale2 + slot, sizeof(float), stream)))
    return s;
  if (!(s = H2D(&scal[1], layout_.gu_input_scale + slot, sizeof(float),
                stream)))
    return s;
  if (!(s = H2D(&scal[2], layout_.dn_w_scale2 + slot, sizeof(float), stream)))
    return s;
  if (!(s = H2D(&scal[3], layout_.dn_input_scale + slot, sizeof(float),
                stream)))
    return s;

  // Mark this L2 buffer in flight until its last H2D completes (victim
  // selection skips it; a same-expert L2 hit may still use it read-only).
  if (cudaEventRecord(l2_event_[buf], stream) != cudaSuccess) {
    return Status::Fail("residency L2 event record failed");
  }
  {
    std::lock_guard<std::mutex> lk(*l2_mu_);
    l2_in_flight_[buf] = true;
    l2_claimed_[buf] = false;
  }

  // Host identity + scales (the GEMM wrappers read the host scale vectors).
  layout_.gu_w_scale2_h[slot] = scal[0];
  layout_.gu_input_scale_h[slot] = scal[1];
  layout_.dn_w_scale2_h[slot] = scal[2];
  layout_.dn_input_scale_h[slot] = scal[3];
  if (slot_expert_[slot] < 0) ++resident_count_;
  if (slot_expert_[slot] >= 0) expert_slot_[slot_expert_[slot]] = -1;
  slot_expert_[slot] = expert;
  expert_slot_[expert] = slot;
  ++stats_.loads;
  stats_.load_bytes +=
      static_cast<double>(3 * w_bytes + 3 * s_bytes +
                          4 * sizeof(float));
  return Status();
}

Status MoEResidency::InitHot(const std::vector<int>& hot_experts,
                             cudaStream_t stream) {
  if (!inited_) return Status::Fail("residency not initialized");
  LoadPlan plan;
  int slot = 0;
  for (int e : hot_experts) {
    if (e < 0 || e >= E_) continue;
    if (expert_slot_[e] >= 0) continue;  // duplicate in the list
    if (slot >= C_) break;
    plan.experts.push_back(e);
    plan.slots.push_back(slot);
    ++slot;
  }
  Status s = LoadPhase1(plan);
  if (!s.ok()) return s;
  while (!plan.fully_committed()) {
    s = LoadPhase2(plan, stream);
    if (!s.ok()) return s;
    s = LoadPhase1(plan);
    if (!s.ok()) return s;
  }
  for (int i = 0; i < static_cast<int>(plan.experts.size()); ++i) {
    slot_tick_[plan.slots[i]] = ++tick_;
  }
  init_done_ = true;
  return Status();
}

void MoEResidency::SetHotProtected() {
  if (!inited_ || hot_protected_) return;
  for (int s = 0; s < C_; ++s) {
    const int e = slot_expert_[s];
    if (e >= 0 && expert_slot_[e] == s) {
      slot_protected_[s] = 1;
      ++protected_count_;
    }
  }
  hot_protected_ = true;
}

Status MoEResidency::PlanResolve(const int32_t* needed, int n,
                                 int32_t* slot_of, LoadPlan* plan,
                                 bool decode_phase) const {
  if (!inited_) return Status::Fail("residency not initialized");
  if (n <= 0) return Status();
  if (n > (1 << 20)) {
    return Status::Fail("residency resolve set too large");
  }
  plan->clear();
  ++tick_;
  ++stats_.resolve_calls;

  // Mark the needed set so victim selection never evicts an expert that this
  // call still has to load (avoids a load-evict-reload cycle). Kept on the
  // plan so LoadPhase1's L2 victim selection sees the same marks.
  plan->needed_mark.assign(E_, 0);
  std::vector<uint8_t>& needed_mark = plan->needed_mark;
  for (int i = 0; i < n; ++i) {
    const int e = needed[i];
    if (e < 0 || e >= E_) {
      return Status::Fail("needed expert out of range");
    }
    needed_mark[e] = 1;
  }

  std::vector<uint8_t> reserved(C_, 0);
  // In-call dedup: the needed list repeats experts (one entry per token
  // top-k slot). A miss reserves a slot but does not commit it, so without
  // this map every repeat of the same expert would miss again and reserve a
  // second slot, exhausting capacity even when the DISTINCT set fits.
  std::vector<int32_t> planned(E_, -1);
  for (int i = 0; i < n; ++i) {
    const int e = needed[i];
    ++stats_.expert_lookups;
    if (decode_phase) {
      ++stats_.decode_lookups;
    } else {
      ++stats_.prefill_lookups;
    }
    int slot = expert_slot_[e];
    if (slot < 0) slot = planned[e];
    if (slot >= 0) {
      slot_tick_[slot] = tick_;
      ++stats_.hits;
      slot_of[i] = slot;
      continue;
    }
    // Miss: pick a victim. Prefer an empty slot; else the LRU slot that is
    // not protected, not needed by this call, and not reserved by an earlier
    // miss in this call. The caller keeps the distinct needed set within the
    // available (non-protected) capacity, so this always succeeds.
    ++stats_.misses;
    if (decode_phase) {
      ++stats_.decode_misses;
    } else {
      ++stats_.prefill_misses;
    }
    int v = -1;
    for (int s = 0; s < C_; ++s) {
      if (slot_expert_[s] < 0 && !slot_protected_[s] && !reserved[s]) {
        v = s;
        break;
      }
    }
    if (v < 0) {
      uint64_t best = UINT64_MAX;
      for (int s = 0; s < C_; ++s) {
        if (slot_protected_[s] || reserved[s]) continue;
        const int se = slot_expert_[s];
        if (se >= 0 && needed_mark[se]) continue;
        if (slot_tick_[s] < best) {
          best = slot_tick_[s];
          v = s;
        }
      }
    }
    if (v < 0) {
      int distinct = 0;
      for (int e = 0; e < E_; ++e)
        if (needed_mark[e]) ++distinct;
      int resident = 0;
      for (int e = 0; e < E_; ++e)
        if (needed_mark[e] && expert_slot_[e] >= 0) ++resident;
      int reserved_n = 0;
      for (int s = 0; s < C_; ++s)
        if (reserved[s]) ++reserved_n;
      std::fprintf(stderr,
                   "[q4t][residency][diag] layer=%d C=%d protected=%d "
                   "distinct=%d resident=%d misses_so_far=%d reserved=%d "
                   "n=%d decode=%d\n",
                   layer_id_, C_, protected_count_, distinct, resident,
                   static_cast<int>(stats_.misses), reserved_n, n,
                   decode_phase);
      return Status::Fail("no residency slot available (needed set exceeds "
                          "dynamic capacity)");
    }
    reserved[v] = 1;
    planned[e] = v;
    plan->experts.push_back(e);
    plan->slots.push_back(v);
    slot_of[i] = v;
  }
  return Status();
}

Status MoEResidency::LoadPhase1(LoadPlan& plan) const {
  if (!inited_) return Status::Fail("residency not initialized");
  const int n = static_cast<int>(plan.experts.size());
  const int off = static_cast<int>(plan.next_stage);
  if (off >= n) return Status();  // fully staged
  const bool tim = timing_.enabled.load(std::memory_order_relaxed);
  const auto t_p1 =
      tim ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};
  if (off == 0) plan.workers.assign(n, 0);
  // One chunk per call: worker t stages entry off + t into an L2 buffer
  // (hit or miss). Buffers are never shared between workers because victim
  // selection is serialized and skips in-flight and claimed buffers. Reusing
  // a buffer for the next chunk is safe because StageExpert waits on the
  // event recorded by the commit that last H2D'd from it.
  const int cnt = std::min(load_threads_, n - off);
  plan.first_err.store(0, std::memory_order_relaxed);
  for (int t = 0; t < cnt; ++t) {
    LoadWorker* w = workers_[t];
    {
      std::lock_guard<std::mutex> lk(w->mu);
      w->plan = &plan;
      w->idx = off + t;
      w->finished = false;
    }
    w->need.notify_one();
  }
  for (int t = 0; t < cnt; ++t) {
    LoadWorker* w = workers_[t];
    std::unique_lock<std::mutex> lk(w->mu);
    w->done.wait(lk, [&] { return w->finished; });
  }
  if (plan.first_err.load(std::memory_order_relaxed) != 0) {
    if (tim) RecordPhase1Ns(NowNs(t_p1));
    return Status::Fail("residency parallel stage failed: " +
                        plan.first_err_msg);
  }
  if (tim) RecordPhase1Ns(NowNs(t_p1));
  plan.next_stage = off + cnt;
  return Status();
}

// Persistent load worker: waits for a stage task, stages one expert, and
// signals the caller. The task (plan pointer + entry index) is fully
// specified at dispatch; LoadPhase1 does not return until every dispatched
// worker has finished, so the plan outlives the task.
void MoEResidency::LoadWorkerLoop(LoadWorker* w) const {
  while (true) {
    LoadPlan* plan;
    int idx;
    {
      std::unique_lock<std::mutex> lk(w->mu);
      w->need.wait(lk, [&] { return w->stop || w->idx >= 0; });
      if (w->stop) break;
      plan = w->plan;
      idx = w->idx;
      w->idx = -1;
    }
    int buf = -1;
    Status s = StageExpert(plan->experts[idx],
                           plan->needed_mark.empty() ? nullptr
                                                    : &plan->needed_mark,
                           &buf);
    if (s.ok()) {
      plan->workers[idx] = buf;
    } else {
      if (plan->first_err.exchange(1, std::memory_order_acq_rel) == 0) {
        // Only the first failing worker writes; LoadPhase1 reads after
        // waiting on every worker's done (happens-before via the cv).
        plan->first_err_msg = s.message();
      }
    }
    {
      std::lock_guard<std::mutex> lk(w->mu);
      w->finished = true;
    }
    w->done.notify_one();
  }
}

Status MoEResidency::LoadPhase2(LoadPlan& plan, cudaStream_t stream,
                                bool* loaded) const {
  if (!inited_) return Status::Fail("residency not initialized");
  if (plan.next_commit >= plan.next_stage) return Status();  // nothing staged
  if (plan.workers.size() != plan.experts.size()) {
    return Status::Fail("residency plan missing worker map");
  }
  for (size_t i = plan.next_commit; i < plan.next_stage; ++i) {
    const int e = plan.experts[i];
    const int v = plan.slots[i];
    // Only an overwrite of an occupied slot is an eviction; loading an empty
    // slot (initial fill) is not.
    if (slot_expert_[v] >= 0) ++stats_.evictions;
    Status s = CommitExpert(e, v, plan.workers[i], stream);
    if (!s.ok()) return s;
    slot_tick_[v] = tick_;
    if (loaded) *loaded = true;
  }
  plan.next_commit = plan.next_stage;
  return Status();
}

Status MoEResidency::Resolve(const int32_t* needed, int n, int32_t* slot_of,
                             cudaStream_t stream, bool* loaded,
                             bool decode_phase) const {
  LoadPlan plan;
  Status s = PlanResolve(needed, n, slot_of, &plan, decode_phase);
  if (!s.ok()) return s;
  if (loaded) *loaded = false;
  s = LoadPhase1(plan);
  if (!s.ok()) return s;
  while (!plan.fully_committed()) {
    s = LoadPhase2(plan, stream, loaded);
    if (!s.ok()) return s;
    s = LoadPhase1(plan);
    if (!s.ok()) return s;
  }
  return Status();
}

void MoEResidency::Free() {
  if (!inited_) return;
  for (auto* w : workers_) {
    {
      std::lock_guard<std::mutex> lk(w->mu);
      w->stop = true;
    }
    w->need.notify_one();
  }
  for (auto* w : workers_) {
    if (w->th.joinable()) w->th.join();
    delete w;
  }
  workers_.clear();
  auto free_if = [](void** p) {
    if (*p) {
      cudaFree(*p);
      *p = nullptr;
    }
  };
  free_if(reinterpret_cast<void**>(&layout_.gu_packed));
  free_if(reinterpret_cast<void**>(&layout_.gu_sf));
  free_if(reinterpret_cast<void**>(&layout_.dn_packed));
  free_if(reinterpret_cast<void**>(&layout_.dn_sf));
  free_if(reinterpret_cast<void**>(&layout_.gu_w_scale2));
  free_if(reinterpret_cast<void**>(&layout_.gu_input_scale));
  free_if(reinterpret_cast<void**>(&layout_.dn_w_scale2));
  free_if(reinterpret_cast<void**>(&layout_.dn_input_scale));
  for (int b = 0; b < l2_slots_; ++b) {
    if (l2_event_[b]) {
      cudaEventDestroy(l2_event_[b]);
      l2_event_[b] = nullptr;
    }
  }
  if (l2_block_) {
    cudaFreeHost(l2_block_);
    l2_block_ = nullptr;
  }
  if (l2_mu_) {
    delete l2_mu_;
    l2_mu_ = nullptr;
  }
  l2_buf_.clear();
  l2_expert_.clear();
  expert_l2buf_.clear();
  l2_tick_.clear();
  l2_in_flight_.clear();
  l2_claimed_.clear();
  l2_event_.clear();
  inited_ = false;
}

}  // namespace quant
}  // namespace q4t
