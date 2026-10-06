// Bounded host metadata observer. No cache decisions, CUDA, locks or I/O.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>

namespace q4t::quant {

inline int MoESupplyObserverSetting(const char* value) {
  if (!value || std::strcmp(value, "0") == 0) return 0;
  if (std::strcmp(value, "1") == 0) return 1;
  return -1;
}

#define Q4T_SUPPLY_FIELDS(X)                                                \
  X(PlansStarted, plans_started)                                           \
  X(PlansComplete, plans_complete)                                         \
  X(PlansFailed, plans_failed)                                             \
  X(PlansScopeMismatch, plans_scope_mismatch)                              \
  X(PlannedLoads, planned_loads)                                           \
  X(SourceL2, source_l2)                                                   \
  X(SourceMirror, source_mirror)                                           \
  X(SourceRead, source_read)                                               \
  X(CommittedLoads, committed_loads)                                       \
  X(ClaimErrors, claim_errors)                                             \
  X(ReadErrors, read_errors)                                               \
  X(CommitErrors, commit_errors)                                           \
  X(EntryL2Present, entry_l2_present)                                       \
  X(EntryMirrorPresent, entry_mirror_present)                               \
  X(EntryMirrorCandidate, entry_mirror_candidate)                           \
  X(EntryCandidateToMirror, entry_candidate_to_mirror)                      \
  X(EntryCandidateToL2, entry_candidate_to_l2)                              \
  X(EntryCandidateToReadDirectActive, entry_candidate_to_read_direct_active) \
  X(EntryCandidateToReadDirectPublished,                                   \
    entry_candidate_to_read_direct_published)                             \
  X(EntryCandidateToReadOther, entry_candidate_to_read_other)               \
  X(EntryCandidateUnclaimed, entry_candidate_unclaimed)                    \
  X(WritebackReservations, writeback_reservations)                         \
  X(WritebackPendingMissingTargets, writeback_pending_missing_targets)     \
  X(WritebackPendingEntryCandidateTargets,                                 \
    writeback_pending_entry_candidate_targets)                            \
  X(WritebackPublished, writeback_published)                               \
  X(WritebackAborted, writeback_aborted)                                   \
  X(WritebackSkipped, writeback_skipped)                                   \
  X(SourceMirrorOutsideEntryCandidates,                                   \
    source_mirror_outside_entry_candidates)                               \
  X(DuplicateClaims, duplicate_claims)                                    \
  X(EntryClaimedMirrors, entry_claimed_mirrors)                             \
  X(CounterOverflow, counter_overflow)                                    \
  X(SamplesConfirmedTotal, samples_confirmed_total)                        \
  X(SamplesOverwritten, samples_overwritten)

enum class SupplyCounter : size_t {
#define Q4T_SUPPLY_ENUM(name, key) k##name,
  Q4T_SUPPLY_FIELDS(Q4T_SUPPLY_ENUM)
#undef Q4T_SUPPLY_ENUM
  kCount
};

inline constexpr size_t kSupplyCounterCount =
    static_cast<size_t>(SupplyCounter::kCount);
inline constexpr std::array<const char*, kSupplyCounterCount>
    kSupplyCounterNames = {
#define Q4T_SUPPLY_NAME(name, key) #key,
        Q4T_SUPPLY_FIELDS(Q4T_SUPPLY_NAME)
#undef Q4T_SUPPLY_NAME
};
#undef Q4T_SUPPLY_FIELDS

struct SupplyCounters {
  std::array<uint64_t, kSupplyCounterCount> values{};
  uint64_t Get(SupplyCounter field) const {
    return values[static_cast<size_t>(field)];
  }
  void Add(SupplyCounter field, uint64_t value = 1) {
    auto& target = values[static_cast<size_t>(field)];
    if (value > std::numeric_limits<uint64_t>::max() - target) {
      target = std::numeric_limits<uint64_t>::max();
      values[static_cast<size_t>(SupplyCounter::kCounterOverflow)] = 1;
    } else {
      target += value;
    }
  }
};

enum class SupplySource { kNone, kL2, kMirror, kRead };
enum class SupplyCause { kNone, kActive, kPublished };
enum class SupplyPublication { kNone, kActive, kPublished, kAborted };

struct SupplyWitness {
  uint64_t witness_seq = 0;
  uint64_t plan_clock = 0;
  uint64_t reserve_seq = 0;
  uint64_t claim_seq = 0;
  uint64_t publication_seq = 0;
  int layer = -1;
  int entry_index = -1;
  int expert = -1;
  int source_task = -1;
  int overwriting_task = -1;
  int gpu_victim = -1;
  int mirror_slot = -1;
  SupplyCause cause = SupplyCause::kNone;
  SupplyPublication publication = SupplyPublication::kNone;
  // Retained witnesses are only finalized, successful direct READs.
};

// Per-entry flags below are ordinary bytes, not packed bits. A task writes
// only its own read/commit result fields. Cross-task causal fields are
// accessed exclusively inside the runtime's existing l2_mu_ sections.
struct SupplyEntry {
  int expert = -1, slot = -1, victim = -1, task = -1;
  int mirror_slot = -1;
  bool entry_l2 = false, entry_mirror = false, candidate = false;
  bool entry_claimed = false, pending = true;
  bool claim_error = false, read_error = false, commit_error = false;
  bool read_ok = false, commit_ok = false, writeback_skipped = false;
  SupplySource source = SupplySource::kNone;
  SupplyCause cause = SupplyCause::kNone;
  uint64_t claim_seq = 0;
  int active_owner = -1, destroyed_owner = -1, cause_owner = -1;
  int target_slot = -1, target_entry = -1;
  bool target_pending = false, target_candidate = false;
  uint64_t reserve_seq = 0, publication_seq = 0;
  SupplyPublication publication = SupplyPublication::kNone;
};

struct MoESupplyPlan {
  static constexpr size_t kMaxMisses = 10;
  std::array<SupplyEntry, kMaxMisses> entries{};
  std::array<int, 16> entry_l2{};
  std::array<int, 8> entry_mirror{};
  std::array<bool, 8> entry_claimed{};
  size_t count = 0;
  int layer = -1;
  uint64_t plan_clock = 0, sequence = 0, duplicate_claims = 0;
  bool finalized = false, invalid = false;

  int Find(int expert) const {
    for (size_t i = 0; i < count; ++i) {
      if (entries[i].expert == expert) return static_cast<int>(i);
    }
    return -1;
  }

  // Caller-owned entry after the old worker barrier, before new dispatch.
  // Frozen max_seq=1/PhaseD-off permits this copy without another lock.
  void CaptureEntry() {
    for (size_t i = 0; i < count; ++i) {
      auto& e = entries[i];
      for (int expert : entry_l2) e.entry_l2 |= expert == e.expert;
      for (size_t slot = 0; slot < entry_mirror.size(); ++slot) {
        if (entry_mirror[slot] != e.expert) continue;
        if (e.entry_mirror) invalid = true;
        e.entry_mirror = true;
        e.mirror_slot = static_cast<int>(slot);
        e.entry_claimed = entry_claimed[slot];
      }
      e.candidate = !e.entry_l2 && e.entry_mirror && !e.entry_claimed;
    }
  }

  // Call exactly where ClaimExpert selects its actual source, under lock.
  void Claim(size_t index, SupplySource source, int mirror_slot = -1,
             bool mirror_claimed = false) {
    auto& e = entries[index];
    if (!e.pending) {
      ++duplicate_claims;
      invalid = true;
      return;
    }
    e.pending = false;
    e.claim_seq = ++sequence;
    e.source = source;
    if (!e.candidate || source != SupplySource::kRead) return;
    if (e.active_owner >= 0) {
      const auto& owner = entries[e.active_owner];
      if (owner.publication == SupplyPublication::kActive &&
          owner.target_slot == mirror_slot && mirror_claimed) {
        e.cause = SupplyCause::kActive;
        e.cause_owner = e.active_owner;
      }
    }
    if (e.cause == SupplyCause::kNone && e.destroyed_owner >= 0 &&
        mirror_slot < 0) {
      const auto& owner = entries[e.destroyed_owner];
      if (owner.publication == SupplyPublication::kPublished) {
        e.cause = SupplyCause::kPublished;
        e.cause_owner = e.destroyed_owner;
      }
    }
  }

  void ClaimFailure(size_t index) {
    Claim(index, SupplySource::kNone);
    entries[index].claim_error = true;
  }

  // Immediately before the existing ring_claimed=true assignment.
  void Reserve(size_t owner_index, int slot, int previous_expert) {
    auto& owner = entries[owner_index];
    if (owner.publication != SupplyPublication::kNone) invalid = true;
    owner.target_slot = slot;
    owner.reserve_seq = ++sequence;
    owner.publication = SupplyPublication::kActive;
    owner.target_entry = Find(previous_expert);
    if (owner.target_entry < 0) return;
    auto& target = entries[owner.target_entry];
    owner.target_pending = target.pending;
    owner.target_candidate = target.pending && target.candidate;
    if (owner.target_candidate) target.active_owner = owner_index;
  }

  void Publish(size_t owner_index) {
    auto& owner = entries[owner_index];
    if (owner.publication != SupplyPublication::kActive) invalid = true;
    owner.publication = SupplyPublication::kPublished;
    owner.publication_seq = ++sequence;
    if (owner.target_candidate) {
      auto& target = entries[owner.target_entry];
      if (target.active_owner == static_cast<int>(owner_index)) {
        target.active_owner = -1;
        target.destroyed_owner = owner_index;
      }
    }
  }

  void Abort(size_t owner_index) {
    auto& owner = entries[owner_index];
    if (owner.publication != SupplyPublication::kActive) invalid = true;
    owner.publication = SupplyPublication::kAborted;
    ++sequence;
    if (owner.target_candidate) {
      auto& target = entries[owner.target_entry];
      if (target.active_owner == static_cast<int>(owner_index))
        target.active_owner = -1;
    }
  }
};

struct MoESupplyState {
  static constexpr size_t kSampleCapacity = 4;
  SupplyCounters counters;
  std::array<SupplyWitness, kSampleCapacity> samples{};
  size_t sample_count = 0;

  void Start() { counters.Add(SupplyCounter::kPlansStarted); }
  void ScopeFailure() { counters.Add(SupplyCounter::kPlansScopeMismatch); }

  // Caller only, after all tasks finish. Failed plans retain source intent
  // and errors, but never add confirmed direct loss counters or witnesses.
  void Finish(MoESupplyPlan& plan, bool runtime_success) {
    using C = SupplyCounter;
    if (plan.finalized) {
      ScopeFailure();
      return;
    }
    plan.finalized = true;
    bool complete = runtime_success && !plan.invalid;
    for (size_t i = 0; i < plan.count; ++i) {
      const auto& e = plan.entries[i];
      complete &= !e.pending && !e.claim_error && !e.read_error &&
                  !e.commit_error && e.commit_ok &&
                  e.source != SupplySource::kNone &&
                  (e.source != SupplySource::kRead || e.read_ok) &&
                  e.publication != SupplyPublication::kActive;
      if (e.cause != SupplyCause::kNone) {
        const auto& owner = plan.entries[e.cause_owner];
        complete &= owner.task != e.task && owner.task >= 0 && e.task >= 0;
      }
    }
    counters.Add(complete ? C::kPlansComplete : C::kPlansFailed);
    if (plan.invalid) ScopeFailure();
    counters.Add(C::kPlannedLoads, plan.count);
    counters.Add(C::kDuplicateClaims, plan.duplicate_claims);
    for (size_t i = 0; i < plan.count; ++i) {
      const auto& e = plan.entries[i];
      counters.Add(C::kClaimErrors, e.claim_error);
      counters.Add(C::kReadErrors, e.read_error);
      counters.Add(C::kCommitErrors, e.commit_error);
      counters.Add(C::kCommittedLoads, e.commit_ok);
      counters.Add(C::kSourceL2, e.source == SupplySource::kL2);
      counters.Add(C::kSourceMirror, e.source == SupplySource::kMirror);
      counters.Add(C::kSourceRead, e.source == SupplySource::kRead);
      counters.Add(C::kEntryL2Present, e.entry_l2);
      counters.Add(C::kEntryMirrorPresent, e.entry_mirror);
      counters.Add(C::kEntryMirrorCandidate, e.candidate);
      counters.Add(C::kEntryClaimedMirrors, e.entry_claimed);
      counters.Add(C::kSourceMirrorOutsideEntryCandidates,
                   e.source == SupplySource::kMirror && !e.candidate);
      counters.Add(C::kWritebackReservations, e.reserve_seq != 0);
      counters.Add(C::kWritebackPendingMissingTargets, e.target_pending);
      counters.Add(C::kWritebackPendingEntryCandidateTargets,
                   e.target_candidate);
      counters.Add(C::kWritebackPublished,
                   e.publication == SupplyPublication::kPublished);
      counters.Add(C::kWritebackAborted,
                   e.publication == SupplyPublication::kAborted);
      counters.Add(C::kWritebackSkipped, e.writeback_skipped);
      if (!e.candidate) continue;
      if (e.source == SupplySource::kMirror) {
        counters.Add(C::kEntryCandidateToMirror);
      } else if (e.source == SupplySource::kL2) {
        counters.Add(C::kEntryCandidateToL2);
      } else if (e.source != SupplySource::kRead || !complete) {
        counters.Add(C::kEntryCandidateUnclaimed);
      } else if (e.cause == SupplyCause::kNone) {
        counters.Add(C::kEntryCandidateToReadOther);
      } else {
        counters.Add(e.cause == SupplyCause::kActive
                         ? C::kEntryCandidateToReadDirectActive
                         : C::kEntryCandidateToReadDirectPublished);
        const auto& owner = plan.entries[e.cause_owner];
        counters.Add(C::kSamplesConfirmedTotal);
        SupplyWitness witness;
        witness.witness_seq = counters.Get(C::kSamplesConfirmedTotal);
        witness.plan_clock = plan.plan_clock;
        witness.reserve_seq = owner.reserve_seq;
        witness.claim_seq = e.claim_seq;
        witness.publication_seq = owner.publication_seq;
        witness.layer = plan.layer;
        witness.entry_index = i;
        witness.expert = e.expert;
        witness.source_task = e.task;
        witness.overwriting_task = owner.task;
        witness.gpu_victim = owner.victim;
        witness.mirror_slot = owner.target_slot;
        witness.cause = e.cause;
        witness.publication = owner.publication;
        if (sample_count == kSampleCapacity) {
          for (size_t j = 1; j < sample_count; ++j) samples[j - 1] = samples[j];
          counters.Add(C::kSamplesOverwritten);
        } else {
          ++sample_count;
        }
        samples[sample_count - 1] = witness;
      }
    }
  }
};

static_assert(sizeof(MoESupplyPlan) <= 4096);
static_assert(sizeof(MoESupplyState) <= 4096);

}  // namespace q4t::quant
