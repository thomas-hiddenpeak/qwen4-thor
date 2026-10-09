#pragma once

#include <condition_variable>
#include <mutex>

namespace q4t::server {

// stopping, the queue and completion state share mutex. enqueue publishes the
// borrowed request only while that lock is held. Once accepted, only completed
// may release the caller's request/buffers: stopping does not drain GPU work.
// Both callbacks run under mutex; completed must not acquire it again.
template <typename Enqueue, typename Completed>
bool SubmitSchedulerRequestAndWait(
    std::mutex& mutex, const bool& stopping,
    std::condition_variable& scheduler_cv,
    std::condition_variable& completion_cv, Enqueue enqueue,
    Completed completed) {
  std::unique_lock<std::mutex> lock(mutex);
  if (stopping) return false;
  enqueue();
  scheduler_cv.notify_one();
  completion_cv.wait(lock, completed);
  return true;
}

}  // namespace q4t::server
