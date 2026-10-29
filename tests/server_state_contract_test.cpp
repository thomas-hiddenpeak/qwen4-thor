#include "q4t/model/sequence.h"
#include "q4t/server/request_control.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

#include <array>
#include <barrier>
#include <chrono>
#include <thread>

namespace {
using q4t::Status;
using q4t::model::CompleteSequenceWork;
using q4t::model::ModelSequence;
using q4t::server::RequestControl;
using q4t::server::RequestRegistry;
using Stage = ModelSequence::Stage;
const auto kNoDeadline = std::chrono::steady_clock::time_point::max();
}  // namespace

Q4T_TEST(server_exception_drains_before_ownership_release) {
  ModelSequence seq;
  Q4T_CHECK(seq.Begin(0).ok());
  const int32_t token = 42;
  Q4T_CHECK(seq.Submit({&token, 1}, Stage::kDecode, 8).ok());
  bool drained = false, released_after_drain = false;
  RequestControl request("failure", "key", kNoDeadline);
  try {
    q4t::server::ScopeExit owner([&] {
      released_after_drain = drained && !seq.HasPending();
    });
    q4t::server::OnException failure([&] {
      request.Cancel(RequestControl::State::kFailure);
      drained = true;  // Host stand-in for the required completion boundary.
      seq.FailAfterDrain();
    });
    throw std::bad_alloc();
  } catch (const std::bad_alloc&) {
  }
  Q4T_CHECK(released_after_drain && request.Failed() && !request.Finish());
  Q4T_CHECK(seq.history.empty() && seq.position == 0);
  Q4T_CHECK(seq.stage == Stage::kFailed && seq.End().ok());
  int calls = 0;
  {
    q4t::server::ScopeExit once([&] { ++calls; });
    once.Run();
    once.Run();
  }
  Q4T_CHECK(calls == 1);
  return true;
}

Q4T_TEST(server_state_nonmonotonic_slots_and_survivors) {
  std::array<ModelSequence, 4> slots;
  for (int slot = 0; slot < 4; ++slot) {
    const int32_t prompt[] = {100 + slot, 200 + slot};
    Q4T_CHECK(slots[slot].Begin(slot).ok());
    Q4T_CHECK(slots[slot].Submit(prompt, Stage::kDecode, 16).ok());
  }
  ModelSequence* first[] = {&slots[3], &slots[1], &slots[0], &slots[2]};
  Q4T_CHECK(CompleteSequenceWork(first, Status(), [] { return Status(); }).ok());
  Q4T_CHECK(slots[0].End().ok());
  ModelSequence* survivors[] = {&slots[3], &slots[1], &slots[2]};
  for (auto* seq : survivors) {
    int32_t token = 300 + seq->seq_id;
    Q4T_CHECK(seq->Submit({&token, 1}, Stage::kDecode, 16).ok());
    token = -1;  // Pending host tokens must be owned, not borrowed.
  }
  Q4T_CHECK(CompleteSequenceWork(survivors, Status(), [&] {
    for (auto* seq : survivors) {
      if (seq->position != 2 || seq->history.size() != 2)
        return Status::Fail("published before completion");
    }
    return Status();
  }).ok());
  for (int slot : {3, 1, 2}) {
    Q4T_CHECK(slots[slot].seq_id == slot && slots[slot].position == 3);
    Q4T_CHECK(slots[slot].history ==
              std::vector<int32_t>({100 + slot, 200 + slot, 300 + slot}));
  }
  // This verifies host reuse only. Device reset remains the caller's duty.
  Q4T_CHECK(slots[0].Begin(0).ok() && slots[0].history.empty());
  return true;
}

Q4T_TEST(server_state_cancel_does_not_release_pending_work) {
  RequestControl request("a", "key", kNoDeadline);
  ModelSequence seq;
  const int32_t token = 91;
  Q4T_CHECK(seq.Begin(2).ok());
  Q4T_CHECK(seq.Submit({&token, 1}, Stage::kDecode, 8).ok());
  Q4T_CHECK(request.Cancel() && request.Cancelled());
  Q4T_CHECK(seq.HasPending() && !seq.Begin(2).ok());
  ModelSequence* batch[] = {&seq};
  bool drained = false;
  // Cancellation wins the HTTP result, but already submitted GPU work can
  // still complete successfully before cleanup releases the slot.
  Q4T_CHECK(CompleteSequenceWork(batch, Status(), [&] {
    drained = seq.HasPending();
    return Status();
  }).ok());
  Q4T_CHECK(drained && !request.Finish() && !seq.HasPending());
  Q4T_CHECK(seq.position == 1 && seq.history[0] == token);
  Q4T_CHECK(seq.End().ok() && seq.Begin(2).ok());
  return true;
}

Q4T_TEST(server_state_stale_cleanup_cannot_remove_reused_identity) {
  RequestRegistry registry;
  auto old = registry.Register("same-id", "old-key", kNoDeadline);
  Q4T_CHECK(old && !registry.Register("same-id", "other", kNoDeadline));
  Q4T_CHECK(registry.Cancel("same-id", "old-key"));
  registry.Remove(old);
  auto fresh = registry.Register("same-id", "fresh-key", kNoDeadline);
  Q4T_CHECK(fresh && !fresh->Cancelled());
  registry.Remove(old);
  Q4T_CHECK(!registry.Cancel("same-id", "old-key"));
  Q4T_CHECK(!fresh->Cancelled());
  Q4T_CHECK(registry.Cancel("same-id", "fresh-key"));
  Q4T_CHECK(fresh->Cancelled());
  registry.Remove(fresh);
  return true;
}

Q4T_TEST(server_state_cancel_and_finish_have_one_winner) {
  RequestControl cancelled("a", "key", kNoDeadline);
  Q4T_CHECK(cancelled.Cancel() && !cancelled.Finish());
  RequestControl completed("b", "key", kNoDeadline);
  Q4T_CHECK(completed.Finish() && !completed.Cancel());
  // One raced attempt; correctness does not depend on which thread wins.
  RequestControl raced("c", "key", kNoDeadline);
  std::barrier start(3);
  bool cancel_won = false, finish_won = false;
  std::thread cancel([&] {
    start.arrive_and_wait();
    cancel_won = raced.Cancel();
  });
  std::thread finish([&] {
    start.arrive_and_wait();
    finish_won = raced.Finish();
  });
  start.arrive_and_wait();
  cancel.join();
  finish.join();
  Q4T_CHECK(cancel_won != finish_won);
  Q4T_CHECK(raced.Cancelled() == cancel_won);
  return true;
}

Q4T_TEST(server_state_deadline_and_shutdown_preserve_terminal_reason) {
  RequestRegistry registry;
  auto expired = registry.Register("expired", "key",
      std::chrono::steady_clock::time_point::min());
  auto active = registry.Register("active", "key", kNoDeadline);
  auto finished = registry.Register("finished", "key", kNoDeadline);
  Q4T_CHECK(expired->Cancelled() && finished->Finish());
  registry.CancelAll();
  Q4T_CHECK(std::string(expired->Reason()) == "deadline_exceeded");
  Q4T_CHECK(std::string(active->Reason()) == "server_shutdown");
  Q4T_CHECK(!finished->Cancelled() && !finished->Cancel());
  return true;
}
