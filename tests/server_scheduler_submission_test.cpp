#include "q4t/server/scheduler_submission.h"
#include "q4t/test.h"

#include <condition_variable>
#include <mutex>
#include <thread>

namespace {

// All state is protected by mutex. The CV handshakes force each ordering;
// these tests do not rely on sleeps or the relative speed of either thread.
struct Submission {
  std::mutex mutex;
  std::condition_variable scheduler_cv;
  std::condition_variable completion_cv;
  std::condition_variable phase_cv;
  bool stopping = false;
  bool queued = false;
  bool pending = false;
  bool done = false;
  bool accepted = false;
  bool returned = false;
  bool rechecked_after_stop = false;
  int* borrowed = nullptr;
  int delivered = 0;
};

}  // namespace

Q4T_TEST(scheduler_submission_rejects_after_stop) {
  Submission state;
  state.stopping = true;
  bool checked_completion = false;
  const bool accepted = q4t::server::SubmitSchedulerRequestAndWait(
      state.mutex, state.stopping, state.scheduler_cv, state.completion_cv,
      [&] {
        state.queued = true;
        state.pending = true;
      },
      [&] {
        checked_completion = true;
        return state.done;
      });
  Q4T_CHECK(!accepted && !state.queued && !state.pending);
  Q4T_CHECK(!checked_completion && !state.done);
  return true;
}

Q4T_TEST(scheduler_submission_drains_before_shutdown) {
  Submission state;
  std::thread requester([&] {
    const bool accepted = q4t::server::SubmitSchedulerRequestAndWait(
        state.mutex, state.stopping, state.scheduler_cv, state.completion_cv,
        [&] {
          state.pending = true;
          state.queued = true;
        },
        [&] { return state.done; });
    const std::lock_guard<std::mutex> lock(state.mutex);
    state.accepted = accepted;
    state.returned = true;
  });
  bool owned_before_drain = false;
  {
    std::unique_lock<std::mutex> lock(state.mutex);
    state.scheduler_cv.wait(lock, [&] { return state.queued; });
    // The accepted stack request remains owned until the scheduler drains it.
    state.stopping = true;
    owned_before_drain = state.pending && !state.returned && !state.done;
    state.queued = false;
    state.pending = false;
    state.done = true;
    state.completion_cv.notify_one();
  }
  requester.join();
  Q4T_CHECK(owned_before_drain);
  Q4T_CHECK(state.accepted && state.returned && state.done);
  Q4T_CHECK(!state.queued && !state.pending);
  return true;
}

Q4T_TEST(scheduler_submission_keeps_borrowed_buffer_until_done) {
  Submission state;
  std::thread requester([&] {
    int buffer = 7;
    const bool accepted = q4t::server::SubmitSchedulerRequestAndWait(
        state.mutex, state.stopping, state.scheduler_cv, state.completion_cv,
        [&] {
          state.borrowed = &buffer;
          state.queued = true;
        },
        [&] {
          if (state.stopping && !state.done) {
            state.rechecked_after_stop = true;
            state.phase_cv.notify_one();
          }
          return state.done;
        });
    const std::lock_guard<std::mutex> lock(state.mutex);
    state.accepted = accepted;
    state.delivered = buffer;
    state.borrowed = nullptr;
    state.returned = true;
    state.phase_cv.notify_one();
  });
  bool retained_buffer = false;
  {
    std::unique_lock<std::mutex> lock(state.mutex);
    state.scheduler_cv.wait(lock, [&] { return state.queued; });
    // Model work has taken the request out of the queue but still borrows its
    // buffer. A stop notification must cause another wait, not an early exit.
    state.queued = false;
    state.stopping = true;
    state.completion_cv.notify_one();
    state.phase_cv.wait(lock, [&] {
      return state.rechecked_after_stop || state.returned;
    });
    retained_buffer = state.rechecked_after_stop && !state.returned &&
                      state.borrowed != nullptr;
    // Keep failure paths safe too: never dereference an already released
    // request buffer if a regressed helper returned on stopping alone.
    if (retained_buffer) *state.borrowed = 42;
    state.done = true;
    state.completion_cv.notify_one();
  }
  requester.join();
  Q4T_CHECK(retained_buffer);
  Q4T_CHECK(state.accepted && state.returned && state.done);
  Q4T_CHECK(state.borrowed == nullptr && state.delivered == 42);
  return true;
}
