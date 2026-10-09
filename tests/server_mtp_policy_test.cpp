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

Q4T_TEST(mtp_generation_step_preserves_output_and_context_tail) {
  using q4t::server::MtpGenerationStepFits;
  // A 4-token step consumes four main inputs. Ordinary decode leaves its
  // final allowed output unconsumed, so only the 5/5 cell permits this step.
  for (int output_remaining = 1; output_remaining <= 5; ++output_remaining) {
    for (int context_remaining = 1; context_remaining <= 5;
         ++context_remaining) {
      Q4T_CHECK(MtpGenerationStepFits(16 - context_remaining, 16, 8192, 3,
                                     output_remaining) ==
                (output_remaining == 5 && context_remaining == 5));
    }
  }
  return true;
}

Q4T_TEST(mtp_generation_step_rejects_invalid_or_overflowing_ranges) {
  using q4t::server::MtpGenerationStepFits;
  constexpr int limit = std::numeric_limits<int>::max();
  Q4T_CHECK(!MtpGenerationStepFits(-1, 16, 8192, 3, 5));
  Q4T_CHECK(!MtpGenerationStepFits(0, 16, 8192, 3, 0));
  Q4T_CHECK(!MtpGenerationStepFits(0, 16, 8192, 3, -1));
  Q4T_CHECK(!MtpGenerationStepFits(0, 16, 3, 3, 5));
  Q4T_CHECK(!MtpGenerationStepFits(0, 16, 8192, 0, 5));
  Q4T_CHECK(!MtpGenerationStepFits(0, 16, 8192, -1, 5));
  Q4T_CHECK(!MtpGenerationStepFits(limit - 4, limit, 8192, 3, limit));
  Q4T_CHECK(MtpGenerationStepFits(limit - 5, limit, 8192, 3, limit));
  Q4T_CHECK(!MtpGenerationStepFits(1, limit, limit, limit, limit));
  return true;
}

Q4T_TEST(mtp_sequential_step_compatibility_name) {
  using q4t::server::MtpSequentialStepFits;
  Q4T_CHECK(MtpSequentialStepFits(11, 16, 8192, 3, 5));
  Q4T_CHECK(!MtpSequentialStepFits(12, 16, 8192, 3, 5));
  Q4T_CHECK(!MtpSequentialStepFits(11, 16, 8192, 3, 4));
  return true;
}
