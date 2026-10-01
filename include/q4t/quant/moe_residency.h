// Per-layer tiered expert residency for the NVFP4 routed-expert MoE.
//
// A layer keeps C device slots (C << E) instead of all E experts. Each slot
// holds one expert's packed weights + swizzled scale blocks + FP32 scales,
// laid out exactly like one expert slice of MoEWeightLayout, so the existing
// grouped-GEMM paths consume slot IDs unchanged when the layout is in slot
// mode. Missing experts are loaded on demand from the checkpoint (NVMe read
// + host swizzle + H2D); victims are chosen by LRU among the slots not
// needed by the current forward.
//
// Safety contract:
//   * A slot is only overwritten after every earlier GEMM that read it has
//     completed, because the H2D of the new expert is enqueued on the same
//     stream after those GEMMs (the commit, which C1 runs inside Phase 1).
//   * A forward only reads a slot after its load H2D, because the GEMMs are
//     enqueued after the H2D on the same stream.
//   * The L2 pool (below) is a per-layer pinned CPU cache of expert
//     payloads. Phase 1 (NVMe read + swizzle) writes an L2 buffer while the
//     previous H2D of that buffer may still be in flight; reusing a buffer
//     therefore waits (cudaEventSynchronize) for the H2D that last used it.
//     Victim selection never picks an in-flight buffer.
//
// Load pipeline (see Resolve / PlanResolve / LoadPhase1 / LoadPhase2):
//   Plan    : host-only; maps the needed set to slots, picks victims for the
//             misses (LRU, never a protected hot slot, never a slot still
//             needed by this call); records the needed set (plan.needed_mark)
//             so L2 victim selection prefers buffers whose expert is not
//             still needed by the same plan.
//   Phase 1 : parallel host work (thread pool): for each miss, an L2 hit
//             reuses the cached payload; an L2 miss NVMe-reads + scale-merges
//             + swizzles into an evicted L2 buffer. C1 pipeline: as soon as a
//             worker finishes staging, it commits that expert itself (H2D on
//             the model stream, stream-ordered after any earlier GEMM and
//             before any later GEMM), so each H2D overlaps the remaining
//             NVMe reads of the chunk. Blocking until the whole chunk is
//             staged and committed.
//   Phase 2 : no-op since the C1 pipeline (commits happen in Phase 1); kept
//             for API compatibility with the prefill pipeline in moe.cu,
//             which still alternates Phase2/Phase1 until fully committed.
// Callers that can overlap Phase 1 with GPU compute (prefill sub-chunk
// pipelines) use Plan/Phase1/Phase2 directly; the convenience Resolve()
// runs the phases back to back.
//
// L2 sizing: Q4T_MOE_L2_SLOTS (default 128, clamped to [16, 512]; the floor
// is the load worker count since the C1 pipeline) pinned buffers per layer,
// each MoEResidencyStagingBytes. The steady-state decode
// working set beyond the C GPU slots is tens of experts per layer, so the
// L2 keeps it warm and steady-state misses are H2D-from-RAM instead of
// NVMe reads.
//
// Numerical contract: loading expert e into any slot produces the exact same
// device bytes as LoadMoEWeights would for expert e (same read, same swizzle,
// same scales), so resident and on-demand experts are bit-identical to the
// all-experts-resident path.
#pragma once

#include <cuda_runtime.h>

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "q4t/io/weight_loader.h"
#include "q4t/quant/moe_weights.h"
#include "q4t/status.h"

namespace q4t {
namespace quant {

// Device bytes for one layer's C-slot layout (C x per-expert slice, the
// exact allocation MoEResidency::Init performs).
size_t MoEResidencyLayerBytes(int hs, int moe_is, int C);
// Pinned staging bytes for ONE load worker (the exact per-buffer allocation
// MoEResidency::Init performs; the layer allocates MoEResidencyLoadThreads()
// of them).
size_t MoEResidencyStagingBytes(int hs, int moe_is);
// Load worker threads from Q4T_MOE_LOAD_THREADS (default 8, clamped to
// [1, kMaxLoadThreads]). The budget and the residency must agree on this.
int MoEResidencyLoadThreads();
// Per-layer L2 CPU cache buffers from Q4T_MOE_L2_SLOTS (default 128,
// clamped to [MoEResidencyLoadThreads(), 512]). The budget must charge
// kLayers x this x MoEResidencyStagingBytes.
int MoEResidencyL2Slots();

// One layer's C-slot expert residency. Not copyable; owned by the layer.
class MoEResidency {
 public:
  // Max load worker threads; the memory budget and the residency
  // must agree on this (see MoEResidencyLoadThreads). 16 since the C1
  // pipeline (path C design 2026-10-01): more parallel NVMe readers
  // saturate the drive; the L2 floor follows the worker count.
  static constexpr int kMaxLoadThreads = 16;

  // Cumulative counters (host-side, single-threaded per layer in this
  // service; max_seq=1 contract).
  struct Stats {
    uint64_t resolve_calls = 0;  // Resolve() invocations
    uint64_t expert_lookups = 0;  // needed-expert entries examined
    uint64_t hits = 0;  // lookups already resident
    uint64_t misses = 0;  // lookups that required a load
    uint64_t loads = 0;  // experts loaded (incl. initial hot list)
    uint64_t evictions = 0;  // slots overwritten
    double load_bytes = 0.0;  // bytes copied H2D into slots (per commit)
    double nvme_read_bytes = 0.0;  // bytes actually read from checkpoint
    // Per-phase split (decode = single-token forwards, prefill = chunked).
    uint64_t decode_lookups = 0;
    uint64_t decode_misses = 0;
    uint64_t prefill_lookups = 0;
    uint64_t prefill_misses = 0;
    // L2 (CPU RAM) cache counters: misses are the NVMe reads, hits reuse a
    // cached payload (H2D only); evictions are L2 buffers overwritten.
    uint64_t l2_hits = 0;
    uint64_t l2_misses = 0;
    uint64_t l2_evictions = 0;
  };

  // Per-miss pipeline timing. Enabled only when Q4T_RESIDENCY_TIMING=1
  // (default off, zero overhead when disabled). The stage/pread/swz
  // counters are written by the persistent load workers (atomic); phase1
  // and d2h are recorded by the caller thread. All *_ns are cumulative
  // nanoseconds; the server takes per-request deltas.
  struct TimingStats {
    std::atomic<bool> enabled{false};
    std::atomic<uint64_t> stage_count{0}, stage_ns{0}, stage_max_ns{0};
    std::atomic<uint64_t> pread_count{0}, pread_ns{0}, pread_max_ns{0};
    std::atomic<uint64_t> swz_count{0}, swz_ns{0}, swz_max_ns{0};
    std::atomic<uint64_t> phase1_count{0}, phase1_ns{0}, phase1_max_ns{0};
    std::atomic<uint64_t> d2h_count{0}, d2h_ns{0}, d2h_max_ns{0};
    void Reset() {
      stage_count.store(0);
      stage_ns.store(0);
      stage_max_ns.store(0);
      pread_count.store(0);
      pread_ns.store(0);
      pread_max_ns.store(0);
      swz_count.store(0);
      swz_ns.store(0);
      swz_max_ns.store(0);
      phase1_count.store(0);
      phase1_ns.store(0);
      phase1_max_ns.store(0);
      d2h_count.store(0);
      d2h_ns.store(0);
      d2h_max_ns.store(0);
    }
  };

  // A planned set of expert loads: expert i goes into slot i. PlanResolve
  // fills it; LoadPhase1 stages the next chunk of entries into L2 buffers
  // (one per worker); LoadPhase2 copies the staged chunk to device and
  // updates the host identity/scale state. An L2 buffer may only be
  // rewritten after its H2D completed (victim selection skips in-flight and
  // claimed buffers), so callers must interleave the phases: Phase1 stages
  // at most one chunk ahead of Phase2 (see Resolve and the prefill loop in
  // moe.cu).
  struct LoadPlan {
    std::vector<int> experts;
    std::vector<int> slots;
    // LoadPhase1 fills workers[i] with the L2 buffer index that staged
    // expert i; LoadPhase2 commits expert i from that buffer.
    std::vector<int> workers;
    // [next_commit, next_stage) is staged but not yet committed;
    // next_stage == experts.size() means fully staged.
    size_t next_stage = 0;
    size_t next_commit = 0;
    // PlanResolve fills this (size E): 1 for every expert this plan loads,
    // so L2 victim selection can prefer buffers whose expert is not still
    // needed by the same plan.
    std::vector<uint8_t> needed_mark;
    // LoadPhase1 resets this to 0; a worker stores 1 on stage failure.
    std::atomic<int> first_err{0};
    // First stage error message (written by the first failing worker only;
    // LoadPhase1 reads it after waiting on every worker, so no race).
    std::string first_err_msg;
    bool empty() const { return experts.empty(); }
    bool fully_committed() const { return next_commit >= experts.size(); }
    void clear() {
      experts.clear();
      slots.clear();
      workers.clear();
      needed_mark.clear();
      next_stage = 0;
      next_commit = 0;
      first_err.store(0, std::memory_order_relaxed);
      first_err_msg.clear();
    }
  };

  // Allocate C slots and bind the checkpoint loader. No experts are loaded.
  // `stream` is used for the initial hot-list loads; later loads use the
  // stream passed to the load phases (stream-ordered safety, see header).
  Status Init(const io::WeightLoader& loader, int layer_id, int E, int hs,
              int moe_is, int C, cudaStream_t stream);

  // Load the static hot list into the slots (duplicates and entries >= E are
  // ignored; at most C distinct experts are loaded).
  Status InitHot(const std::vector<int>& hot_experts, cudaStream_t stream);

  // Mark the currently resident hot experts as protected: victim selection
  // never evicts them. Call after InitHot. With H protected experts the
  // dynamic capacity is C - H; callers must keep each Resolve call's
  // distinct NON-protected expert set within C - H.
  void SetHotProtected();
  bool HotProtected() const { return hot_protected_; }
  // Number of protected (hot) slots.
  int ProtectedCount() const { return protected_count_; }

  // Plan a needed-expert set to slots (host-only, no device work).
  //   needed : host array of n expert IDs (0..E-1, duplicates allowed)
  //   slot_of: host array of n ints, receives the slot index per entry
  //   decode_phase: true for single-token (decode) forwards; used only for
  //                 per-phase miss statistics.
  // Misses are collected into `plan` with their victim slots; nothing is
  // loaded or evicted yet. The caller must keep the distinct needed set
  // within the available (non-protected) capacity, as Resolve does.
  Status PlanResolve(const int32_t* needed, int n, int32_t* slot_of,
                     LoadPlan* plan, bool decode_phase = false) const;

  // Phase 1: stage the next chunk of `plan` (at most load_threads_
  // entries, entry next_stage + t by worker t into staging buffer t) in
  // parallel (NVMe read + merge + swizzle), each worker committing its
  // expert as soon as staging finishes (C1). Blocking; per-thread pinned
  // staging. No-op when the plan is fully staged.
  Status LoadPhase1(LoadPlan& plan) const;

  // Phase 2: no-op since the C1 pipeline (commits happen in Phase 1); kept
  // for API compatibility. No-op when nothing is staged.
  Status LoadPhase2(LoadPlan& plan, cudaStream_t stream,
                    bool* loaded = nullptr) const;

  // Convenience: Plan + Phase 1 (stage+commit) back to back (decode path).
  // `loaded` reports whether any load was enqueued.
  Status Resolve(const int32_t* needed, int n, int32_t* slot_of,
                 cudaStream_t stream, bool* loaded = nullptr,
                 bool decode_phase = false) const;

  // The C-slot device layout (E = C, slot_mode = true). Valid after Init.
  const MoEWeightLayout& Layout() const { return layout_; }
  int Slots() const { return C_; }
  // Expert currently in a slot (-1 = empty). Test/diagnostic accessor.
  int SlotExpert(int slot) const { return slot_expert_[slot]; }
  int Experts() const { return E_; }
  int ResidentCount() const { return resident_count_; }
  size_t DeviceBytes() const { return layout_.TotalBytes(); }
  const Stats& GetStats() const { return stats_; }
  bool TimingEnabled() const {
    return timing_->enabled.load(std::memory_order_relaxed);
  }
  const TimingStats& GetTiming() const { return *timing_; }
  // Records one router D2H + stream-sync round trip (from MoEForward).
  void RecordD2HSync(uint64_t ns) const;

  // Free all device buffers (idempotent).
  void Free();

 private:
  // Atomic max update for the timing max counters (relaxed; a lost
  // update only under-reports the max, never corrupts).
  static void AtomicMaxU64(std::atomic<uint64_t>& dst, uint64_t v) {
    uint64_t cur = dst.load(std::memory_order_relaxed);
    while (v > cur &&
           !dst.compare_exchange_weak(cur, v, std::memory_order_relaxed)) {
    }
  }
  // Timing recorders (no-ops when timing is disabled).
  void RecordStageNs(uint64_t ns) const;
  void RecordPhase1Ns(uint64_t ns) const;

  // Stage one expert into an L2 buffer: an L2 hit reuses the cached payload
  // (no NVMe read); an L2 miss evicts an LRU victim (never an in-flight
  // buffer, preferring experts not in *needed_mark) and NVMe-reads + merges
  // + swizzles into it. *buf_out receives the buffer index.
  // One persistent load worker (created in Init, joined in Free).
  // LoadPhase1 dispatches one stage task per worker and waits for
  // completion, instead of creating and destroying load_threads_
  // std::threads on every call (that create/join cost is a fixed
  // per-miss-chunk overhead on the decode hot path).
  // Per-worker counter delta. C1 pipeline: workers stage AND commit, so the
  // counters they touch (loads, evictions, L2, nvme bytes, resident count)
  // accumulate here worker-locally; LoadPhase1 merges every dispatched
  // worker's delta into stats_ after the chunk barrier (happens-before via
  // the done condition variable). Stats therefore stays caller-thread-only,
  // copyable, and SumResidencyStats unchanged.
  struct StatsDelta {
    uint64_t loads = 0;
    double load_bytes = 0.0;
    uint64_t evictions = 0;
    double nvme_read_bytes = 0.0;
    uint64_t l2_hits = 0;
    uint64_t l2_misses = 0;
    uint64_t l2_evictions = 0;
    int resident_delta = 0;
    void Reset() {
      loads = 0;
      load_bytes = 0.0;
      evictions = 0;
      nvme_read_bytes = 0.0;
      l2_hits = 0;
      l2_misses = 0;
      l2_evictions = 0;
      resident_delta = 0;
    }
    void MergeInto(Stats& s, int& resident_count) const {
      s.loads += loads;
      s.load_bytes += load_bytes;
      s.evictions += evictions;
      s.nvme_read_bytes += nvme_read_bytes;
      s.l2_hits += l2_hits;
      s.l2_misses += l2_misses;
      s.l2_evictions += l2_evictions;
      resident_count += resident_delta;
    }
  };

  struct LoadWorker {
    std::thread th;
    std::mutex mu;
    std::condition_variable need;  // worker waits for a task
    std::condition_variable done;  // caller waits for task completion
    LoadPlan* plan = nullptr;
    int idx = -1;
    bool finished = true;
    bool stop = false;
    cudaStream_t stream = nullptr;  // C1: commit stream for this task
    StatsDelta delta_;  // C1: counters staged+committed by this task
  };

  Status StageExpert(int expert, const std::vector<uint8_t>* needed_mark,
                     int* buf_out, StatsDelta* delta = nullptr,
                     bool* hit_out = nullptr) const;

  // Undo a miss-path stage bookkeeping after a stage or commit failure:
  // drop the buffer<->expert mapping (miss only; a hit buffer still holds
  // the payload) and release the claim so the buffer is pickable again.
  void ReleaseMissClaim(int expert, int buf, bool hit) const;

  // LRU victim among non-in-flight L2 buffers (-1 if none; impossible while
  // in-flight count < l2_slots_).
  int PickL2Victim(const std::vector<uint8_t>* needed_mark) const;

  // Commit one staged expert from L2 buffer `buf` into its slot
  // (H2D + host state). C1: called by the staging worker as soon as the
  // stage finishes (stream-ordered safety as in the header contract).
  Status CommitExpert(int expert, int slot, int buf, cudaStream_t stream,
                      StatsDelta* delta = nullptr) const;

  // Body of one persistent load worker (see LoadWorker).
  void LoadWorkerLoop(LoadWorker* w) const;

  const io::WeightLoader* loader_ = nullptr;  // borrowed from Model::weight_loader; valid for the model's lifetime
  int layer_id_ = 0;
  int E_ = 0;
  int hs_ = 0;
  int moe_is_ = 0;
  int C_ = 0;

  mutable MoEWeightLayout layout_;  // E = C, slot_mode = true

  // Host state.
  mutable std::vector<int> slot_expert_;  // [C] expert id in slot, -1 = empty
  mutable std::vector<int> expert_slot_;  // [E] slot of expert, -1 = not resident
  mutable std::vector<uint64_t> slot_tick_;  // [C] LRU recency (higher = newer)
  mutable uint64_t tick_ = 0;
  mutable int resident_count_ = 0;

  // Protected hot slots (never evicted).
  mutable std::vector<uint8_t> slot_protected_;  // [C]
  bool hot_protected_ = false;
  int protected_count_ = 0;

  // Per-slot FP32 scales are kept in layout_ (gu_w_scale2_h etc., sized C).

  // Pinned L2 pool (see header safety contract). One cudaHostAlloc block
  // per layer, sub-allocated into l2_slots_ buffers of staging_bytes_ each.
  // Raw cudaHostAlloc pointer: the C++ allocator's pages are not valid
  // cudaFreeHost targets.
  int load_threads_ = 1;
  // C1: stream the workers commit on. Set from Init's stream; the service
  // runs single-stream (Q4T_MOE_STREAMS=1, enforced at model load), so it
  // is the same stream every load phase and GEMM uses.
  cudaStream_t commit_stream_ = nullptr;
  int l2_slots_ = 0;
  // C2: single device block holding all four per-slot scale vectors
  // ([gu_w_scale2 | gu_input_scale | dn_w_scale2 | dn_input_scale], C
  // floats each) so CommitExpert H2Ds all four scales in one copy. The
  // layout_ scale pointers point into it; Free() releases the block once.
  uint8_t* scal_block_ = nullptr;
  mutable uint8_t* l2_block_ = nullptr;
  mutable std::vector<uint8_t*> l2_buf_;  // [L]
  mutable std::vector<int> l2_expert_;  // [L] expert in buffer, -1 = empty
  mutable std::vector<int> expert_l2buf_;  // [E] buffer of expert, -1 = absent
  // One-shot test-only fault hook (Q4T_RESIDENCY_FAIL_EXPERT):
  // "first" -> the first request-time stage in the whole model (any
  // layer/expert, after every layer's InitHot) fails exactly once
  // (global one-shot; see g_first_fault_fired); <int> -> the first
  // stage of that expert fails. The failure message contains
  // "residency fault injection" and the hook disarms. Off unless the
  // env var is "first" or a valid expert id. The hook only fires after
  // init_done_ so an armed hot expert cannot kill startup.
  mutable int fail_expert_ = -1;
  mutable bool fail_first_ = false;
  mutable bool fail_armed_ = false;
  mutable bool init_done_ = false;

  mutable std::vector<uint64_t> l2_tick_;  // [L] LRU recency
  // L2 recency clock, separate from tick_ (the slot LRU clock): L2
  // staging must not advance slot ticks or same-call slot commits
  // would outrun in-call hits and break LRU victim order.
  mutable uint64_t l2_recency_ = 0;
  mutable std::vector<bool> l2_in_flight_;  // [L] H2D pending
  mutable std::vector<bool> l2_claimed_;  // [L] staged by the current chunk, not yet committed
  mutable std::vector<cudaEvent_t> l2_event_;  // [L]
  // Heap-allocated so the class keeps its (unused) implicit copyability;
  // DecoderLayer is default-constructed via vector::resize in model.cu.
  mutable std::mutex* l2_mu_ = nullptr;
  size_t staging_bytes_ = 0;

  // Persistent load workers (see LoadWorker above).
  mutable std::vector<LoadWorker*> workers_;

  mutable Stats stats_;
  // Heap-allocated (like l2_mu_) so the class keeps its implicit
  // movability: TimingStats holds std::atomics, which are non-copyable
  // and non-movable; DecoderLayer is default-constructed via
  // vector::resize in model.cu, which instantiates the move ctor.
  mutable std::unique_ptr<TimingStats> timing_{new TimingStats()};
  bool inited_ = false;
};

}  // namespace quant
}  // namespace q4t
