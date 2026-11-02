#include "q4t/server/sequence_admission.h"
#include "q4t/server/request_admission.h"
#include "q4t/test.h"

#include <array>
#include <barrier>
#include <stdexcept>
#include <thread>
#include <vector>

namespace {
using Queue = q4t::server::SequenceAdmissionQueue;
using Ticket = Queue::Ticket;
}  // namespace

Q4T_TEST(admission_request_lease_concurrent_count_never_exceeds_bound) {
  std::atomic<int> active{0}, accepted{0};
  std::barrier ready(17), release(17);
  std::vector<std::thread> threads;
  for (int i = 0; i < 16; ++i) {
    threads.emplace_back([&] {
      q4t::server::RequestAdmissionLease lease(active, 4);
      if (lease.TryAcquire()) ++accepted;
      ready.arrive_and_wait();
      release.arrive_and_wait();
    });
  }
  ready.arrive_and_wait();
  const bool bounded = active.load() == 4 && accepted.load() == 4;
  release.arrive_and_wait();
  for (auto& thread : threads) thread.join();
  Q4T_CHECK(bounded && active.load() == 0);
  return true;
}

Q4T_TEST(admission_request_lease_scope_and_failed_acquire_do_not_steal) {
  std::atomic<int> active{0};
  try {
    q4t::server::RequestAdmissionLease first(active, 1);
    Q4T_CHECK(first.TryAcquire() && first.TryAcquire() && active.load() == 1);
    {
      q4t::server::RequestAdmissionLease denied(active, 1);
      Q4T_CHECK(!denied.TryAcquire());
    }
    Q4T_CHECK(active.load() == 1);
    throw std::runtime_error("after admission");
  } catch (const std::runtime_error&) {
  }
  Q4T_CHECK(active.load() == 0);
  return true;
}

Q4T_TEST(admission_fifo_best_fit_and_incompatible_bypass) {
  Queue queue;
  Ticket large(4097), small(1024), later(1024);
  const std::array capacities{8192, 2048};
  std::array free{false, true};
  Q4T_CHECK(queue.Enqueue(large, 2) && queue.Enqueue(small, 2));
  Q4T_CHECK(queue.Select(large, capacities, 8192, free) == -1);
  Q4T_CHECK(queue.Select(small, capacities, 8192, free) == 1);
  Q4T_CHECK(queue.Select(later, capacities, 8192, free) == -1);
  free[0] = true;
  Q4T_CHECK(queue.Select(small, capacities, 8192, free) == -1);
  Q4T_CHECK(queue.Select(large, capacities, 8192, free) == 0);
  free[0] = false;  // Ownership transfer happens under the same outer mutex.
  queue.Remove(large);
  Q4T_CHECK(queue.Select(small, capacities, 8192, free) == 1);
  queue.Remove(small);
  Q4T_CHECK(queue.Size() == 0);
  return true;
}

Q4T_TEST(admission_zero_full_queue_and_idle_slot_bypass) {
  Queue queue;
  Ticket large(8192), arrival(1024);
  const std::array capacities{8192, 2048};
  const std::array free{false, true};
  Q4T_CHECK(!queue.Enqueue(large, 0) && !large.Queued());
  Q4T_CHECK(queue.Enqueue(large, 1));
  Q4T_CHECK(!queue.Enqueue(arrival, 1) && queue.Size() == 1);
  // A full queue does not reserve an incompatible idle short slot.
  Q4T_CHECK(queue.Select(arrival, capacities, 8192, free) == 1);
  Q4T_CHECK(!arrival.Queued());
  Q4T_CHECK(!queue.Enqueue(large, 2) && queue.Size() == 1);
  return true;
}

Q4T_TEST(admission_unlinks_head_middle_tail_on_all_scope_exits) {
  Queue queue;
  Ticket first(1), second(1), last(1);
  Q4T_CHECK(queue.Enqueue(first, 4) && queue.Enqueue(second, 4) &&
            queue.Enqueue(last, 4));
  queue.Remove(second);
  Q4T_CHECK(queue.Size() == 2);
  queue.Remove(first);
  Q4T_CHECK(queue.Size() == 1);
  const std::array free{true};
  Q4T_CHECK(queue.Select(last, {}, 8192, free) == 0);
  queue.Remove(last);
  Q4T_CHECK(queue.Size() == 0);
  queue.Remove(last);
  try {
    Ticket failed(1);
    Q4T_CHECK(queue.Enqueue(failed, 1));
    throw std::runtime_error("after enqueue");
  } catch (const std::runtime_error&) {
  }
  Q4T_CHECK(queue.Size() == 0);
  Q4T_CHECK(queue.Enqueue(first, 1));
  Queue other;
  Q4T_CHECK(other.Select(first, {}, 8192, free) == -1);
  other.Remove(first);
  Q4T_CHECK(queue.Size() == 1);
  return true;
}
