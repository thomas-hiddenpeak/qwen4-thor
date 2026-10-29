#pragma once

#include <condition_variable>
#include <mutex>
#include <vector>

namespace q4t::server {

// Only the scheduler calls this at a selection boundary with its queue lock
// held and no borrowed execution still in flight. Completed results may be
// consumed outside the lock: retire their membership without rewriting them.
template <typename Request, typename Cancelled, typename Complete>
void RetireCancelledSchedulerRequests(std::vector<Request*>& active,
                                      Cancelled cancelled, Complete complete) {
  for (auto it = active.begin(); it != active.end();) {
    Request* request = *it;
    if (!cancelled(request)) {
      ++it;
      continue;
    }
    request->retired = true;
    if (request->pending) complete(request);
    it = active.erase(it);
  }
}

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
