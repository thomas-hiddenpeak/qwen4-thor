#include "q4t/server/request_progress.h"

#include <atomic>
#include <thread>
#include <vector>

#include "q4t/test.h"

namespace q4t::server {
struct ProgressTestAccess {
  // Simulate a stopped publisher, not a stopped observer. No sleeps needed.
  static void Pause(ProgressHandle handle) {
    handle.cell_->sequence_.fetch_add(1);
  }
  static void Resume(ProgressHandle handle) {
    handle.cell_->sequence_.fetch_add(1);
  }
};
}  // namespace q4t::server
namespace {
using namespace q4t::server;
}

Q4T_TEST(progress_only_actual_completion_advances_work) {
  RequestProgress progress;
  auto lease = progress.Acquire(1000, 1);
  auto work = lease.Handle();
  Q4T_CHECK(work.Begin(ProgressWork::kPrefill, 2));
  auto snapshot = progress.Read(3);
  Q4T_CHECK(snapshot.complete && snapshot.count == 1);
  Q4T_CHECK(snapshot.rows[0].completed == 0 &&
            snapshot.rows[0].last_completed_ns == 0);
  Q4T_CHECK(work.Completed(4));
  Q4T_CHECK(work.Completed(6));  // Two actually finished chunks.
  Q4T_CHECK(work.End(7));
  snapshot = progress.Read(8);
  Q4T_CHECK(snapshot.complete && snapshot.rows[0].completed == 2);
  Q4T_CHECK(snapshot.rows[0].last_completed_ns == 6 &&
            snapshot.rows[0].work == ProgressWork::kNone);
  Q4T_CHECK(work.Begin(ProgressWork::kDecode, 9));
  Q4T_CHECK(work.End(10));  // Failed/drained operation, not successful work.
  snapshot = progress.Read(11);
  Q4T_CHECK(snapshot.rows[0].completed == 2 &&
            snapshot.rows[0].last_completed_ns == 6 &&
            snapshot.rows[0].work_id == 2);
  return true;
}

Q4T_TEST(progress_stage_wait_and_observation_are_not_completions) {
  RequestProgress progress;
  auto lease = progress.Acquire(1000, 1);
  auto work = lease.Handle();
  Q4T_CHECK(work.Stage(ProgressStage::kAdmission, 2));
  Q4T_CHECK(work.Wait(ProgressWait::kSlot, 3));
  auto snapshot = progress.Read(20);
  Q4T_CHECK(snapshot.complete && snapshot.rows[0].stage_since_ns == 2 &&
            snapshot.rows[0].wait_since_ns == 3);
  Q4T_CHECK(work.Wait(ProgressWait::kSlot, 21));
  Q4T_CHECK(work.Stage(ProgressStage::kAdmission, 22));
  snapshot = progress.Read(30);
  Q4T_CHECK(snapshot.complete && snapshot.rows[0].completed == 0 &&
            snapshot.rows[0].stage_since_ns == 2 &&
            snapshot.rows[0].wait_since_ns == 3);
  Q4T_CHECK(work.Wait(ProgressWait::kNone, 31));
  Q4T_CHECK(work.Stage(ProgressStage::kDecode, 32));
  snapshot = progress.Read(33);
  Q4T_CHECK(snapshot.rows[0].work == ProgressWork::kNone &&
            snapshot.rows[0].completed == 0);
  return true;
}

Q4T_TEST(progress_capacity_is_unknown_without_business_rejection) {
  RequestProgress progress;
  std::vector<RequestProgress::Lease> owners;
  for (size_t i = 0; i < RequestProgress::kCapacity; ++i) {
    owners.push_back(progress.Acquire(1000, 1));
    Q4T_CHECK(owners.back().Handle().valid());
  }
  auto extra = progress.Acquire(1000, 1);
  Q4T_CHECK(!extra.Handle().valid());
  auto snapshot = progress.Read(2);
  Q4T_CHECK(!snapshot.complete && snapshot.untracked == 1 &&
            snapshot.active == RequestProgress::kCapacity + 1);
  Q4T_CHECK(owners[0].Handle().Begin(ProgressWork::kDecode, 3));
  Q4T_CHECK(owners[0].Handle().Completed(4));
  Q4T_CHECK(owners[0].Handle().End(5));
  extra.Release();
  snapshot = progress.Read(6);
  Q4T_CHECK(snapshot.complete && snapshot.rows[0].completed == 1 &&
            snapshot.untracked == 0);
  owners.clear();
  Q4T_CHECK(progress.Read(7).active == 0);
  return true;
}

Q4T_TEST(progress_slot_reuse_rejects_stale_publisher_without_aba) {
  RequestProgress progress;
  auto first = progress.Acquire(100, 1);
  auto stale = first.Handle();
  Q4T_CHECK(stale.Begin(ProgressWork::kPrefill, 2));
  Q4T_CHECK(stale.Completed(3));
  first.Release();
  auto second = progress.Acquire(200, 4);
  Q4T_CHECK(stale.ticket() != second.Handle().ticket());
  Q4T_CHECK(!stale.Completed(5));
  Q4T_CHECK(!stale.Stage(ProgressStage::kCleanup, 5));
  const auto snapshot = progress.Read(6);
  Q4T_CHECK(snapshot.complete && snapshot.count == 1);
  Q4T_CHECK(snapshot.rows[0].deadline_ns == 200 &&
            snapshot.rows[0].completed == 0 &&
            snapshot.rows[0].stage == ProgressStage::kInput);
  return true;
}

Q4T_TEST(progress_owner_release_and_move_keep_lifetimes) {
  RequestProgress progress;
  ProgressHandle old;
  {
    auto first = progress.Acquire(100, 1);
    old = first.Handle();
    RequestProgress::Lease moved(std::move(first));
    first.Release();
    Q4T_CHECK(progress.Read(2).count == 1);
    auto second = progress.Acquire(100, 3);
    moved = std::move(second);
    Q4T_CHECK(progress.Read(4).count == 1);
    Q4T_CHECK(!old.Wait(ProgressWait::kScheduler, 5));
  }
  const auto snapshot = progress.Read(6);
  Q4T_CHECK(snapshot.complete && snapshot.count == 0 && snapshot.active == 0);
  Q4T_CHECK(!old.Begin(ProgressWork::kDecode, 7));
  return true;
}

Q4T_TEST(progress_concurrent_capture_never_accepts_mixed_row) {
  RequestProgress progress;
  auto lease = progress.Acquire(1000000, 1);
  auto work = lease.Handle();
  std::atomic<bool> done{false}, okay{true};
  std::thread writer([&] {
    for (uint64_t i = 1; i <= 5000; ++i) {
      if (!work.Begin(ProgressWork::kDecode, i * 3) ||
          !work.Completed(i * 3 + 1) || !work.End(i * 3 + 2))
        okay.store(false);
    }
    done.store(true);
  });
  while (!done.load()) {
    const auto snapshot = progress.Read(1000000);
    if (!snapshot.complete) continue;
    const auto& row = snapshot.rows[0];
    if (snapshot.count != 1 || row.work_id > 5000 ||
        row.work_since_ns != row.work_id * 3 ||
        (row.completed && row.last_completed_ns != row.completed * 3 + 1) ||
        (row.completed > row.work_id) ||
        (row.work_id - row.completed > 1)) okay.store(false);
  }
  writer.join();
  const auto snapshot = progress.Read(1000000);
  Q4T_CHECK(okay.load() && snapshot.complete &&
            snapshot.rows[0].completed == 5000);
  return true;
}

Q4T_TEST(progress_paused_writer_contention_and_clock_are_unknown) {
  RequestProgress progress;
  {
    auto lease = progress.Acquire(1000, 1);
    auto work = lease.Handle();
    ProgressTestAccess::Pause(work);
    Q4T_CHECK(!progress.Read(2).complete);
    Q4T_CHECK(!work.Wait(ProgressWait::kWrite, 3));
    ProgressTestAccess::Resume(work);
    Q4T_CHECK(!progress.Read(4).complete);  // Lost update remains explicit.
  }
  for (const auto bad_time : {uint64_t{0}, uint64_t{9}}) {
    auto lease = progress.Acquire(1000, 10);
    Q4T_CHECK(!lease.Handle().Stage(ProgressStage::kOutput, bad_time));
    Q4T_CHECK(!progress.Read(20).complete);
  }
  {
    auto lease = progress.Acquire(1000, 10);
    Q4T_CHECK(!progress.Read(9).complete);
    Q4T_CHECK(progress.Read(11).complete);
  }
  Q4T_CHECK(!progress.Read(0).complete);
  Q4T_CHECK(progress.Read(21).complete);
  return true;
}

Q4T_TEST(progress_other_work_cannot_hide_old_unfinished_operation) {
  RequestProgress progress;
  auto old = progress.Acquire(10, 1);
  auto busy = progress.Acquire(1000, 2);
  Q4T_CHECK(old.Handle().Begin(ProgressWork::kPrefill, 3));
  for (uint64_t i = 4; i < 100; i += 3) {
    Q4T_CHECK(busy.Handle().Begin(ProgressWork::kDecode, i));
    Q4T_CHECK(busy.Handle().Completed(i + 1));
    Q4T_CHECK(busy.Handle().End(i + 2));
  }
  const auto snapshot = progress.Read(200);
  Q4T_CHECK(snapshot.complete && snapshot.count == 2);
  Q4T_CHECK(snapshot.rows[0].deadline_ns == 10 &&
            snapshot.rows[0].work_since_ns == 3 &&
            snapshot.rows[0].work == ProgressWork::kPrefill &&
            snapshot.rows[0].completed == 0 &&
            snapshot.rows[1].completed == 32);
  return true;
}
