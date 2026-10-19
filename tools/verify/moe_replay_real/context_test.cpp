#include "capture_context.h"
#include "q4t/test.h"

Q4T_TEST(real_capture_explicit_context) {
  q4t::diagnostic::CaptureContext context;
  std::vector<int32_t> tokens{11, 12, 13};
  Q4T_CHECK(!context.ObserveLayer(0, 1).ok());
  Q4T_CHECK(context.Begin(tokens, 2, 1, 3, 2, 248320).ok());
  tokens[0] = 99;
  Q4T_CHECK(context.tokens()[0] == 11);
  Q4T_CHECK(context.ObserveLayer(0, 1).ok());
  Q4T_CHECK(context.ObserveLayer(1, 1).ok());
  Q4T_CHECK(context.ObserveLayer(2, 1).ok());
  Q4T_CHECK(context.Captured(2).ok());
  Q4T_CHECK(!context.valid());
  Q4T_CHECK(context.Finish(true).ok());
  Q4T_CHECK(context.valid());
  for (int failure = 0; failure < 7; ++failure) {
    Q4T_CHECK(context.Begin(tokens, 2, 1, 3, 2, 248320).ok());
    Q4T_CHECK(context.ObserveLayer(0, 1).ok());
    if (failure == 0) Q4T_CHECK(!context.ObserveLayer(0, 1).ok());
    if (failure == 1) Q4T_CHECK(!context.ObserveLayer(1, 4).ok());
    if (failure == 2) Q4T_CHECK(!context.ObserveLayer(-1, 1).ok());
    if (failure == 3) Q4T_CHECK(!context.Captured(1).ok());
    if (failure == 4) Q4T_CHECK(!context.Captured(2).ok());
    if (failure == 5) {
      Q4T_CHECK(!context.Begin(tokens, 2, 1, 3, 2, 248320).ok());
    }
    // Failure 6 is a missing layer/capture even with successful execution.
    Q4T_CHECK(!context.Finish(true).ok());
    Q4T_CHECK(!context.valid());
  }
  Q4T_CHECK(!context.Begin(tokens, 1, 1, 3, 2, 248320).ok());
  Q4T_CHECK(!context.Begin(tokens, 2, 1, 3, 3, 248320).ok());
  tokens[2] = 248320;
  Q4T_CHECK(!context.Begin(tokens, 2, 1, 3, 2, 248320).ok());
  return true;
}
