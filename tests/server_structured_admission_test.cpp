#include "q4t/server/structured_admission.h"

#include <atomic>
#include <future>
#include <limits>
#include <stdexcept>

#include "q4t/server/abort_reason.h"
#include "q4t/server/request_control.h"
#include "q4t/test.h"

Q4T_TEST(structured_admission_is_independent_and_exclusive) {
  std::atomic<int> active{0};
  {
    q4t::server::StructuredRequestLease first(active);
    Q4T_CHECK(first.TryAcquire());
    auto competing = std::async(std::launch::async, [&] {
      q4t::server::StructuredRequestLease second(active);
      return second.TryAcquire();
    });
    Q4T_CHECK(!competing.get());
    Q4T_CHECK(active.load() == 1);
  }
  Q4T_CHECK(active.load() == 0);
  q4t::server::StructuredRequestLease replacement(active);
  Q4T_CHECK(replacement.TryAcquire());
  return true;
}

Q4T_TEST(structured_admission_retains_cancelled_or_throwing_owner) {
  std::atomic<int> active{0};
  try {
    q4t::server::StructuredRequestLease lease(active);
    Q4T_CHECK(lease.TryAcquire());
    q4t::server::RequestControl request(
        "id", "key",
        std::chrono::steady_clock::now() + std::chrono::seconds(1));
    request.Cancel(q4t::server::RequestControl::State::kShutdown);
    Q4T_CHECK(request.Cancelled());
    q4t::server::StructuredRequestLease blocked(active);
    Q4T_CHECK(!blocked.TryAcquire());
    throw std::runtime_error("compile failed");
  } catch (const std::runtime_error&) {
    Q4T_CHECK(active.load() == 0);
  }
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
