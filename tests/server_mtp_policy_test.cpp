#include "q4t/server/mtp_policy.h"
#include "q4t/test.h"

#include <limits>

Q4T_TEST(mtp_step_preserves_context_tail) {
  using q4t::server::MtpStepFits;
  Q4T_CHECK(MtpStepFits(12, 16, 8192, 3));
  for (int position : {13, 14, 15, 16}) {
    Q4T_CHECK(!MtpStepFits(position, 16, 8192, 3));
  }
  Q4T_CHECK(!MtpStepFits(0, 16, 3, 3));
  Q4T_CHECK(MtpStepFits(0, 16, 4, 3));
  return true;
}

Q4T_TEST(mtp_step_rejects_invalid_or_overflowing_ranges) {
  using q4t::server::MtpStepFits;
  constexpr int limit = std::numeric_limits<int>::max();
  Q4T_CHECK(!MtpStepFits(-1, 16, 8192, 3));
  Q4T_CHECK(!MtpStepFits(0, 0, 8192, 3));
  Q4T_CHECK(!MtpStepFits(0, 16, 8192, 0));
  Q4T_CHECK(!MtpStepFits(0, 16, 8192, -1));
  Q4T_CHECK(!MtpStepFits(limit - 1, limit, 8192, 3));
  Q4T_CHECK(MtpStepFits(limit - 4, limit, 8192, 3));
  Q4T_CHECK(!MtpStepFits(1, limit, limit, limit));
  return true;
}
