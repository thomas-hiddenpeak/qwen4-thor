#include "q4t/server/actual_usage_step.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
}

Q4T_TEST(actual_usage_step_closes_late_observers_before_the_next_choice) {
  ActualUsageCounter counter;
  counter.Enable();
  counter.Merge(ActualUsage{7, 2});
  {
    ActualUsageStep first(&counter);
    first.Observe({10, 3, 2, 1, 4});
    ScopeExit old_cleanup([&] { first.Observe({10, 3, 2, 1, 4}); });
    first.Finish();
    Q4T_CHECK(!first.Active());
    ActualUsageStep second(&counter, false);
    second.Observe({10, 4, 2, 2, 4});
    old_cleanup.Run();  // Must not enter the second producer's live step.
    first.Observe({1000, 1000});
    second.Finish();
    first.Finish();
    Q4T_CHECK(counter.Total() == (ActualUsage{17, 9, 2, 3, 4}));
    ActualUsageStep audio(&counter);
    audio.Observe({5, 33});
    second.Observe({10, 4, 2, 2, 4});
    Q4T_CHECK(counter.Total() == (ActualUsage{22, 42, 2, 3, 4}));
  }
  Q4T_CHECK(counter.Total() == (ActualUsage{22, 42, 2, 3, 4}));
  return true;
}

Q4T_TEST(actual_usage_step_unwinds_partial_work_and_allows_no_context) {
  ActualUsageCounter counter;
  counter.Enable();
  try {
    ActualUsageStep producer(&counter);
    producer.Observe({8, 2});
    producer.Observe({8, 3});
    throw 1;
  } catch (int) {
  }
  Q4T_CHECK(counter.Total() == (ActualUsage{8, 3}));
  ActualUsageStep absent(nullptr);
  Q4T_CHECK(!absent.Active());
  absent.Observe({kActualUsageLimit, 1});
  absent.Finish();
  return true;
}

Q4T_TEST(actual_usage_step_never_repairs_unknown_or_overlapping_work) {
  for (int failure = 0; failure < 3; ++failure) {
    ActualUsageCounter counter;
    counter.Enable();
    {
      ActualUsageStep producer(&counter);
      if (failure == 0) producer.Observe({1, 0, 2});
      if (failure == 1) counter.Merge(std::nullopt);
      if (failure == 2) {
        ActualUsageStep overlapping(&counter);
        overlapping.Observe({1, 1});
      }
      producer.Finish();
      producer.Observe({1, 1});
      ActualUsageStep next(&counter);
      next.Observe({2, 3});
    }
    Q4T_CHECK(!counter.Total());
  }
  return true;
}
