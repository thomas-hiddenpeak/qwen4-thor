#include "q4t/service/guardian_quota.h"
#include "q4t/test.h"

Q4T_TEST(guardian_quota_restore_receipt_never_applies_to_other_generation) {
  q4t::service::QuotaGate gate(true);
  gate.Granted(7, 100);
  Q4T_CHECK(!gate.Restored(6, 5));
  Q4T_CHECK(!gate.Restored(7, 5));
  Q4T_CHECK(!gate.Restored(8, 7));
  Q4T_CHECK(!gate.Open(101));
  Q4T_CHECK(gate.Restored(7, 6));
  Q4T_CHECK(gate.Open(101));
  gate.Retired(200);
  gate.Granted(8, 300);
  Q4T_CHECK(!gate.Restored(7, 6));
  Q4T_CHECK(!gate.Open(301));
  Q4T_CHECK(gate.Open(60'000'000'200));
  return true;
}

Q4T_TEST(guardian_usage_handoff_cannot_extend_original_cleanup_deadline) {
  q4t::service::QuotaHandoffGrace grace;
  Q4T_CHECK(!grace.Waiting(100));
  grace.Start(300, 100, 1000);
  Q4T_CHECK(grace.Waiting(1099));
  grace.Start(1000, 100, 1000);  // Polling must not restart the deadline.
  Q4T_CHECK(!grace.Waiting(1100));
  q4t::service::QuotaHandoffGrace capped, expired;
  capped.Start(100, 100, 30'000'000'000);
  Q4T_CHECK(capped.Waiting(6'000'000'099));
  Q4T_CHECK(!capped.Waiting(6'000'000'100));
  expired.Start(500, 100, 300);
  Q4T_CHECK(!expired.Waiting(500));
  return true;
}
