#include "q4t/server/structured_admission.h"

#include <atomic>
#include <future>
#include <limits>
#include <stdexcept>
#include <thread>

#include "q4t/server/abort_reason.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_control.h"
#include "q4t/test.h"

Q4T_TEST(structured_admission_is_independent_and_exclusive) {
  std::atomic<int> active{0};
  q4t::server::StructuredAdmission admission;
  {
    q4t::server::StructuredRequestLease first(admission, active);
    Q4T_CHECK(first.TryAcquire());
    auto competing = std::async(std::launch::async, [&] {
      q4t::server::StructuredRequestLease second(admission, active);
      return second.Acquire([] { return false; });
    });
    Q4T_CHECK(competing.get() ==
              q4t::server::StructuredRequestLease::Result::kBusy);
    Q4T_CHECK(active.load() == 1);
  }
  Q4T_CHECK(active.load() == 0);
  q4t::server::StructuredRequestLease replacement(admission, active);
  Q4T_CHECK(replacement.TryAcquire());
  return true;
}

Q4T_TEST(structured_admission_retains_cancelled_or_throwing_owner) {
  std::atomic<int> active{0};
  q4t::server::StructuredAdmission admission;
  try {
    q4t::server::StructuredRequestLease lease(admission, active);
    Q4T_CHECK(lease.TryAcquire());
    q4t::server::RequestControl request(
        "id", "key",
        std::chrono::steady_clock::now() + std::chrono::seconds(1));
    request.Cancel(q4t::server::RequestControl::State::kShutdown);
    Q4T_CHECK(request.Cancelled());
    q4t::server::StructuredRequestLease blocked(admission, active);
    Q4T_CHECK(!blocked.TryAcquire());
    throw std::runtime_error("compile failed");
  } catch (const std::runtime_error&) {
    Q4T_CHECK(active.load() == 0);
  }
  return true;
}

Q4T_TEST(structured_handoff_retains_owner_and_releases_waiter) {
  using namespace q4t::server;
  using namespace std::chrono_literals;
  StructuredAdmission admission;
  std::atomic<int> active{0};
  auto owner = std::make_unique<StructuredRequestLease>(admission, active);
  Q4T_CHECK(owner->TryAcquire());
  owner->BeginHandoff();
  owner->BeginHandoff();  // Idempotent, never an early release.
  std::atomic<bool> entered{false};
  RequestControl control("next", "", std::chrono::steady_clock::now() + 2s);
  auto next = std::async(std::launch::async, [&] {
    StructuredRequestLease waiter(admission, active);
    return waiter.Acquire([&] {
      entered.store(true);
      return control.Cancelled();
    });
  });
  ScopeExit release([&] { owner.reset(); });
  const auto end = std::chrono::steady_clock::now() + 1s;
  while (!entered.load() && std::chrono::steady_clock::now() < end)
    std::this_thread::yield();
  Q4T_CHECK(entered.load());
  Q4T_CHECK(next.wait_for(0s) == std::future_status::timeout);
  Q4T_CHECK(active.load() == 1);
  release.Run();
  Q4T_CHECK(next.wait_for(1s) == std::future_status::ready);
  Q4T_CHECK(next.get() == StructuredRequestLease::Result::kAcquired);
  Q4T_CHECK(active.load() == 0);
  return true;
}

Q4T_TEST(structured_handoff_cancellation_keeps_original_owner) {
  using namespace q4t::server;
  using namespace std::chrono_literals;
  StructuredAdmission admission;
  std::atomic<int> active{0};
  StructuredRequestLease owner(admission, active);
  Q4T_CHECK(owner.TryAcquire());
  owner.BeginHandoff();
  for (const auto reason : {RequestControl::State::kExplicit,
                            RequestControl::State::kDisconnect,
                            RequestControl::State::kShutdown,
                            RequestControl::State::kDeadline}) {
    std::atomic<bool> entered{false};
    RequestControl control("next", "", std::chrono::steady_clock::now() + 2s);
    auto next = std::async(std::launch::async, [&] {
      StructuredRequestLease waiter(admission, active);
      return waiter.Acquire([&] {
        entered.store(true);
        return control.Cancelled();
      });
    });
    ScopeExit cancel([&] { control.Cancel(); });
    const auto end = std::chrono::steady_clock::now() + 1s;
    while (!entered.load() && std::chrono::steady_clock::now() < end)
      std::this_thread::yield();
    Q4T_CHECK(entered.load());
    Q4T_CHECK(next.wait_for(0s) == std::future_status::timeout);
    control.Cancel(reason);
    Q4T_CHECK(next.wait_for(1s) == std::future_status::ready);
    Q4T_CHECK(next.get() == StructuredRequestLease::Result::kCancelled);
    Q4T_CHECK(active.load() == 1);
  }
  RequestControl expired("expired", "", std::chrono::steady_clock::now());
  StructuredRequestLease timed_out(admission, active);
  Q4T_CHECK(timed_out.Acquire([&] { return expired.Cancelled(); }) ==
            StructuredRequestLease::Result::kCancelled);
  Q4T_CHECK(std::string_view(expired.Reason()) == "deadline_exceeded");
  return true;
}

Q4T_TEST(structured_vocabulary_budget_no_overflow_or_partial_charge) {
  q4t::server::VocabularyStorageBudget budget;
  Q4T_CHECK(budget.Charge(32));
  Q4T_CHECK(!budget.Charge(std::numeric_limits<size_t>::max()));
  Q4T_CHECK(budget.Used() == 32);
  Q4T_CHECK(budget.Charge(q4t::server::kDecodedVocabularyStorageLimit - 32));
  Q4T_CHECK(!budget.Charge(1));
  Q4T_CHECK(budget.Used() == q4t::server::kDecodedVocabularyStorageLimit);
  return true;
}

Q4T_TEST(observability_abort_labels_are_bounded) {
  using q4t::server::AbortReasonIndex;
  using q4t::server::kAbortReasons;
  for (size_t i = 0; i < kAbortReasons.size(); ++i)
    Q4T_CHECK(AbortReasonIndex(kAbortReasons[i]) == i);
  for (const auto* input : {"user-controlled-label", "request-123", ""})
    Q4T_CHECK(kAbortReasons[AbortReasonIndex(input)] == "other");
  return true;
}
