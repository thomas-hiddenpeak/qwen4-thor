// Pure-host source-observer contracts. No CUDA, model or trace payloads.
#include "q4t/quant/moe_supply_observer.h"

#include <initializer_list>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <utility>

namespace {

using q4t::quant::MoESupplyObserverSetting;
using q4t::quant::MoESupplyPlan;
using q4t::quant::MoESupplyState;
using q4t::quant::SupplyCause;
using q4t::quant::SupplyCounter;
using q4t::quant::SupplyPublication;
using q4t::quant::SupplySource;
using C = SupplyCounter;

void Check(bool condition) {
  if (!condition) throw std::runtime_error("contract assertion failed");
}

MoESupplyPlan Plan(size_t count, std::initializer_list<int> mirror = {},
                   std::initializer_list<int> l2 = {}) {
  MoESupplyPlan plan;
  plan.layer = 3;
  plan.plan_clock = 42;
  plan.count = count;
  plan.entry_l2.fill(-1);
  plan.entry_mirror.fill(-1);
  size_t index = 0;
  for (int expert : mirror) plan.entry_mirror[index++] = expert;
  index = 0;
  for (int expert : l2) plan.entry_l2[index++] = expert;
  for (size_t i = 0; i < count; ++i) {
    plan.entries[i].expert = static_cast<int>(i);
    plan.entries[i].slot = static_cast<int>(i);
    plan.entries[i].victim = 100 + static_cast<int>(i);
    plan.entries[i].task = static_cast<int>(i);
  }
  plan.CaptureEntry();
  return plan;
}

void CompleteSources(MoESupplyPlan& plan) {
  for (size_t i = 0; i < plan.count; ++i) {
    auto& entry = plan.entries[i];
    entry.read_ok = entry.source == SupplySource::kRead;
    entry.commit_ok = entry.source != SupplySource::kNone;
  }
}

MoESupplyState Finish(MoESupplyPlan& plan, bool success = true) {
  MoESupplyState state;
  state.Start();
  state.Finish(plan, success);
  return state;
}

MoESupplyPlan ActiveLoss() {
  auto plan = Plan(2, {1});
  plan.Claim(0, SupplySource::kRead);
  plan.Reserve(0, 0, 1);
  plan.Claim(1, SupplySource::kRead, 0, true);
  return plan;
}

void StrictEnableSetting() {
  Check(MoESupplyObserverSetting(nullptr) == 0);
  Check(MoESupplyObserverSetting("0") == 0);
  Check(MoESupplyObserverSetting("1") == 1);
  for (const char* bad : {"", "true", "01", "-1", " 1", "1 "})
    Check(MoESupplyObserverSetting(bad) == -1);
}

void ZeroMissCompletes() {
  auto plan = Plan(0);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kPlansStarted) == 1);
  Check(state.counters.Get(C::kPlansComplete) == 1);
  Check(state.counters.Get(C::kPlannedLoads) == 0);
}

void SingleMirrorCannotLoseBeforeOwnClaim() {
  auto plan = Plan(1, {0});
  plan.Claim(0, SupplySource::kMirror, 0);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kSourceMirror) == 1);
  Check(state.counters.Get(C::kEntryCandidateToMirror) == 1);
  Check(state.counters.Get(C::kSamplesConfirmedTotal) == 0);
}

void ActiveReservationBeforePublish() {
  auto plan = ActiveLoss();
  plan.Publish(0);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kPlansComplete) == 1);
  Check(state.counters.Get(C::kSourceRead) == 2);
  Check(state.counters.Get(C::kCommittedLoads) == 2);
  Check(state.counters.Get(C::kEntryCandidateToReadDirectActive) == 1);
  const auto& sample = state.samples[0];
  Check(sample.reserve_seq == 2 && sample.claim_seq == 3);
  Check(sample.publication_seq == 4 && sample.plan_clock == 42);
  Check(sample.source_task == 1 && sample.overwriting_task == 0);
  Check(sample.gpu_victim == 100 && sample.mirror_slot == 0);
  Check(sample.layer == 3 && sample.entry_index == 1 && sample.expert == 1);
  Check(sample.cause == SupplyCause::kActive);
}

void ClaimAfterPublication() {
  auto plan = Plan(2, {1});
  plan.Claim(0, SupplySource::kRead);
  plan.Reserve(0, 0, 1);
  plan.Publish(0);
  plan.Claim(1, SupplySource::kRead);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kEntryCandidateToReadDirectPublished) == 1);
  Check(state.samples[0].reserve_seq == 2);
  Check(state.samples[0].publication_seq == 3);
  Check(state.samples[0].claim_seq == 4);
}

void AbortBeforeClaimRestoresOpportunity() {
  auto plan = Plan(2, {1});
  plan.Claim(0, SupplySource::kRead);
  plan.Reserve(0, 0, 1);
  plan.Abort(0);
  plan.Claim(1, SupplySource::kMirror, 0);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kEntryCandidateToMirror) == 1);
  Check(state.counters.Get(C::kWritebackReservations) == 1);
  Check(state.counters.Get(C::kWritebackAborted) == 1);
  Check(state.sample_count == 0);
}

void AbortAfterReadKeepsActualCause() {
  auto plan = ActiveLoss();
  plan.Abort(0);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kEntryCandidateToReadDirectActive) == 1);
  Check(state.samples[0].publication == SupplyPublication::kAborted);
  Check(state.samples[0].publication_seq == 0);
  Check(state.counters.Get(C::kPlansComplete) == 1);
}

void SameTaskClaimsAllBeforeCommit() {
  auto plan = Plan(2, {1});
  plan.entries[1].task = 0;
  plan.Claim(0, SupplySource::kRead);
  plan.Claim(1, SupplySource::kMirror, 0);
  // Runtime cannot reserve the claimed ring source; it skips writeback.
  plan.entries[0].writeback_skipped = true;
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kEntryCandidateToMirror) == 1);
  Check(state.counters.Get(C::kWritebackSkipped) == 1);
  Check(state.counters.Get(C::kSamplesConfirmedTotal) == 0);
}

void L2HasPriorityOverEntryMirror() {
  auto plan = Plan(2, {1}, {1});
  plan.Claim(0, SupplySource::kRead);
  plan.Reserve(0, 0, 1);
  plan.Publish(0);
  plan.Claim(1, SupplySource::kL2);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kEntryL2Present) == 1);
  Check(state.counters.Get(C::kEntryMirrorPresent) == 1);
  Check(state.counters.Get(C::kEntryMirrorCandidate) == 0);
  Check(state.counters.Get(C::kSourceL2) == 1);
  Check(state.counters.Get(C::kWritebackPendingMissingTargets) == 1);
  Check(state.counters.Get(C::kWritebackPendingEntryCandidateTargets) == 0);
}

void UnknownEntryLossStaysSeparate() {
  auto plan = Plan(2, {1});
  plan.Claim(0, SupplySource::kRead);
  plan.Claim(1, SupplySource::kRead);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kEntryCandidateToReadOther) == 1);
  Check(state.counters.Get(C::kSamplesConfirmedTotal) == 0);
}

void ClaimFailureRetainsPartialWork() {
  auto plan = Plan(2, {1});
  plan.Claim(0, SupplySource::kRead);
  plan.ClaimFailure(1);
  CompleteSources(plan);
  auto state = Finish(plan, false);
  Check(state.counters.Get(C::kPlansFailed) == 1);
  Check(state.counters.Get(C::kClaimErrors) == 1);
  Check(state.counters.Get(C::kSourceRead) == 1);
  Check(state.counters.Get(C::kCommittedLoads) == 1);
  Check(state.counters.Get(C::kEntryCandidateUnclaimed) == 1);
}

void ReadFailureCannotConfirmLoss() {
  auto plan = ActiveLoss();
  plan.Publish(0);
  CompleteSources(plan);
  plan.entries[1].read_ok = false;
  plan.entries[1].read_error = true;
  plan.entries[1].commit_ok = false;
  auto state = Finish(plan, false);
  Check(state.counters.Get(C::kSourceRead) == 2);
  Check(state.counters.Get(C::kReadErrors) == 1);
  Check(state.counters.Get(C::kCommittedLoads) == 1);
  Check(state.counters.Get(C::kEntryCandidateUnclaimed) == 1);
  Check(state.sample_count == 0);
}

void CommitFailureCannotConfirmLoss() {
  auto plan = ActiveLoss();
  plan.Publish(0);
  CompleteSources(plan);
  plan.entries[1].commit_ok = false;
  plan.entries[1].commit_error = true;
  auto state = Finish(plan, false);
  Check(state.counters.Get(C::kCommitErrors) == 1);
  Check(state.counters.Get(C::kEntryCandidateUnclaimed) == 1);
  Check(state.counters.Get(C::kSamplesConfirmedTotal) == 0);
}

void MergedReadFailureRetainsEveryAffectedEntry() {
  auto plan = Plan(2);
  plan.entries[1].task = 0;
  plan.Claim(0, SupplySource::kRead);
  plan.Claim(1, SupplySource::kRead);
  // A failed ReadRun marks the entire attempted sub-run, not one expert.
  plan.entries[0].read_error = true;
  plan.entries[1].read_error = true;
  auto state = Finish(plan, false);
  Check(state.counters.Get(C::kReadErrors) == 2);
  Check(state.counters.Get(C::kSourceRead) == 2);
  Check(state.counters.Get(C::kCommittedLoads) == 0);
  Check(state.counters.Get(C::kPlansFailed) == 1);
}

void UnfinishedReservationCannotConfirmLoss() {
  auto plan = ActiveLoss();
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kPlansFailed) == 1);
  Check(state.counters.Get(C::kWritebackReservations) == 1);
  Check(state.counters.Get(C::kWritebackPublished) == 0);
  Check(state.counters.Get(C::kWritebackAborted) == 0);
  Check(state.sample_count == 0);
}

void DuplicateClaimIsNotDoubleCounted() {
  auto plan = Plan(1);
  plan.Claim(0, SupplySource::kRead);
  plan.Claim(0, SupplySource::kRead);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kSourceRead) == 1);
  Check(state.counters.Get(C::kDuplicateClaims) == 1);
  Check(state.counters.Get(C::kPlansScopeMismatch) == 1);
  Check(state.counters.Get(C::kPlansFailed) == 1);
}

void IllegalSameTaskWitnessFailsClosed() {
  auto plan = ActiveLoss();
  plan.entries[1].task = 0;
  plan.Publish(0);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kPlansFailed) == 1);
  Check(state.counters.Get(C::kSamplesConfirmedTotal) == 0);
}

void TwoDistinctLossesCloseCandidatePartition() {
  auto plan = Plan(3, {1, 2});
  plan.Claim(0, SupplySource::kRead);
  plan.Reserve(0, 0, 1);
  plan.Claim(1, SupplySource::kRead, 0, true);
  plan.Publish(0);
  plan.Reserve(1, 1, 2);
  plan.Claim(2, SupplySource::kRead, 1, true);
  plan.Abort(1);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kEntryMirrorCandidate) == 2);
  Check(state.counters.Get(C::kEntryCandidateToReadDirectActive) == 2);
  Check(state.counters.Get(C::kSamplesConfirmedTotal) == 2);
  Check(state.counters.Get(C::kWritebackReservations) == 2);
  Check(state.counters.Get(C::kWritebackPublished) == 1);
  Check(state.counters.Get(C::kWritebackAborted) == 1);
}

void BoundedRecentSamplesKeepExactCounts() {
  MoESupplyState state;
  for (int i = 0; i < 6; ++i) {
    auto plan = ActiveLoss();
    plan.plan_clock = 100 + i;
    plan.Publish(0);
    CompleteSources(plan);
    state.Start();
    state.Finish(plan, true);
  }
  Check(state.counters.Get(C::kSamplesConfirmedTotal) == 6);
  Check(state.counters.Get(C::kSamplesOverwritten) == 2);
  Check(state.sample_count == 4);
  for (size_t i = 0; i < 4; ++i) {
    Check(state.samples[i].witness_seq == i + 3);
    Check(state.samples[i].plan_clock == i + 102);
  }
}

void FinalizationOccursOnce() {
  auto plan = Plan(0);
  auto state = Finish(plan);
  state.Finish(plan, true);
  Check(state.counters.Get(C::kPlansComplete) == 1);
  Check(state.counters.Get(C::kPlansScopeMismatch) == 1);
}

void ShapeSinglePrefillIsSeparatedByBoundaryDeltas() {
  MoESupplyState state;
  auto singleton_prefill = ActiveLoss();
  singleton_prefill.Publish(0);
  CompleteSources(singleton_prefill);
  state.Start();
  state.Finish(singleton_prefill, true);
  const auto at_decode_entry = state.counters;
  auto decode = Plan(1, {0});
  decode.Claim(0, SupplySource::kMirror, 0);
  CompleteSources(decode);
  state.Start();
  state.Finish(decode, true);
  auto all_hit_decode = Plan(0);
  state.Start();
  state.Finish(all_hit_decode, true);
  Check(state.counters.Get(C::kPlansComplete) -
            at_decode_entry.Get(C::kPlansComplete) == 2);
  Check(state.counters.Get(C::kSamplesConfirmedTotal) -
            at_decode_entry.Get(C::kSamplesConfirmedTotal) == 0);
  Check(state.counters.Get(C::kSourceMirror) -
            at_decode_entry.Get(C::kSourceMirror) == 1);
}

void CounterOverflowIsExplicitAndDoesNotWrap() {
  q4t::quant::SupplyCounters counters;
  counters.values[static_cast<size_t>(C::kSourceRead)] =
      std::numeric_limits<uint64_t>::max();
  counters.Add(C::kSourceRead);
  Check(counters.Get(C::kSourceRead) == std::numeric_limits<uint64_t>::max());
  Check(counters.Get(C::kCounterOverflow) == 1);
  counters.Add(C::kSourceL2, 3);
  Check(counters.Get(C::kSourceL2) == 3);
}

void ClaimedEntryIsNotInitialCandidate() {
  auto plan = Plan(1, {0});
  // A fresh fixture captures the actual claimed flag before any worker.
  plan.entries[0] = {};
  plan.entries[0].expert = 0;
  plan.entries[0].task = 0;
  plan.entry_claimed[0] = true;
  plan.CaptureEntry();
  plan.Claim(0, SupplySource::kRead, 0, true);
  CompleteSources(plan);
  auto state = Finish(plan);
  Check(state.counters.Get(C::kEntryClaimedMirrors) == 1);
  Check(state.counters.Get(C::kEntryMirrorCandidate) == 0);
  Check(state.counters.Get(C::kSamplesConfirmedTotal) == 0);
}

}  // namespace

int main() {
  const std::pair<const char*, void (*)()> contracts[] = {
      {"strict_enable", StrictEnableSetting},
      {"zero_miss", ZeroMissCompletes},
      {"single_mirror", SingleMirrorCannotLoseBeforeOwnClaim},
      {"reserve_before_publish", ActiveReservationBeforePublish},
      {"published_loss", ClaimAfterPublication},
      {"abort_before_claim", AbortBeforeClaimRestoresOpportunity},
      {"abort_after_read", AbortAfterReadKeepsActualCause},
      {"same_task_claim_all", SameTaskClaimsAllBeforeCommit},
      {"l2_precedence", L2HasPriorityOverEntryMirror},
      {"other_loss", UnknownEntryLossStaysSeparate},
      {"claim_failure", ClaimFailureRetainsPartialWork},
      {"read_failure", ReadFailureCannotConfirmLoss},
      {"commit_failure", CommitFailureCannotConfirmLoss},
      {"merged_read_failure", MergedReadFailureRetainsEveryAffectedEntry},
      {"unfinished_reservation", UnfinishedReservationCannotConfirmLoss},
      {"duplicate_claim", DuplicateClaimIsNotDoubleCounted},
      {"same_task_impossible_cause", IllegalSameTaskWitnessFailsClosed},
      {"two_losses", TwoDistinctLossesCloseCandidatePartition},
      {"bounded_samples", BoundedRecentSamplesKeepExactCounts},
      {"single_finalization", FinalizationOccursOnce},
      {"phase_boundary_delta", ShapeSinglePrefillIsSeparatedByBoundaryDeltas},
      {"counter_overflow", CounterOverflowIsExplicitAndDoesNotWrap},
      {"claimed_entry", ClaimedEntryIsNotInitialCandidate},
  };
  size_t passed = 0;
  for (const auto& [name, test] : contracts) {
    try {
      test();
      ++passed;
      std::cout << "PASS " << name << '\n';
    } catch (const std::exception& error) {
      std::cerr << "FAIL " << name << ": " << error.what() << '\n';
      return 1;
    }
  }
  std::cout << passed << " contracts passed\n";
  return 0;
}
