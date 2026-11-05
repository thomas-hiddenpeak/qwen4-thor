#include "q4t/model/layer_subtiming.h"
#include "q4t/test.h"

using q4t::model::LayerSubTiming;
using q4t::model::QsaSubTimingGuard;

namespace {
void CompleteLayer(LayerSubTiming& timer, int layer) {
  for (int op = 12; op <= 16; ++op) {
    timer.Mark(layer, op, 0, nullptr);
    timer.Mark(layer, op, 1, nullptr);
  }
}
}  // namespace

Q4T_TEST(qsa_timing_requires_explicit_noncheckpoint_mode) {
  using q4t::model::QsaSubTimingRequested;
  Q4T_CHECK(QsaSubTimingRequested(true, 8192, false, "1"));
  Q4T_CHECK(!QsaSubTimingRequested(false, 8192, false, "1"));
  Q4T_CHECK(!QsaSubTimingRequested(true, 8, false, "1"));
  Q4T_CHECK(!QsaSubTimingRequested(true, 16, true, "1"));
  const char* invalid[] = {nullptr, "0", "true", "", "01"};
  for (const char* value : invalid)
    Q4T_CHECK(!QsaSubTimingRequested(true, 8192, false, value));
  return true;
}

Q4T_TEST(qsa_timing_disabled_preserves_event_set) {
  fake_cuda::Reset();
  LayerSubTiming timer;
  timer.Mark(0, 12, 0, nullptr);
  Q4T_CHECK(fake_cuda::records == 0);
  timer.Begin(2);
  Q4T_CHECK(fake_cuda::creates == 48);
  timer.Mark(0, 12, 0, nullptr);
  Q4T_CHECK(fake_cuda::records == 0);
  timer.Mark(0, 10, 0, nullptr);
  timer.Mark(0, 10, 1, nullptr);
  timer.Report(8192);
  Q4T_CHECK(fake_cuda::live == 0);
  Q4T_CHECK(fake_cuda::unrecorded_elapsed == 0);
  return true;
}

Q4T_TEST(qsa_timing_complete_subset_of_layers) {
  fake_cuda::Reset();
  LayerSubTiming timer;
  timer.Begin(4, 1);
  CompleteLayer(timer, 3);
  timer.Report(8192);
  Q4T_CHECK(timer.qsa_valid);
  Q4T_CHECK(fake_cuda::live == 0);
  Q4T_CHECK(fake_cuda::unrecorded_elapsed == 0);
  return true;
}

Q4T_TEST(qsa_timing_rejects_missing_boundary_and_layer) {
  for (bool missing_layer : {false, true}) {
    fake_cuda::Reset();
    LayerSubTiming timer;
    timer.Begin(2, missing_layer ? 2 : 1);
    if (missing_layer) {
      CompleteLayer(timer, 0);
    } else {
      for (int op = 12; op <= 16; ++op) timer.Mark(0, op, 1, nullptr);
    }
    timer.Report(8192);
    Q4T_CHECK(!timer.qsa_valid);
    Q4T_CHECK(fake_cuda::live == 0);
    Q4T_CHECK(fake_cuda::unrecorded_elapsed == 0);
  }
  return true;
}

Q4T_TEST(qsa_timing_rejects_event_failures) {
  for (int fault = 0; fault < 3; ++fault) {
    fake_cuda::Reset();
    LayerSubTiming timer;
    timer.Begin(1, 1);
    fake_cuda::fail_record = fault == 0;
    CompleteLayer(timer, 0);
    fake_cuda::fail_sync = fault == 1;
    fake_cuda::fail_elapsed = fault == 2;
    timer.Report(8192);
    Q4T_CHECK(!timer.qsa_valid);
    Q4T_CHECK(fake_cuda::live == 0);
  }
  return true;
}

Q4T_TEST(qsa_timing_partial_allocation_is_not_valid) {
  fake_cuda::Reset();
  fake_cuda::fail_create = 7;
  LayerSubTiming timer;
  timer.Begin(1, 1);
  Q4T_CHECK(!timer.active && !timer.qsa_valid);
  Q4T_CHECK(fake_cuda::live == 0);
  return true;
}

Q4T_TEST(qsa_timing_abort_does_not_leak_into_next_forward) {
  fake_cuda::Reset();
  auto& timer = LayerSubTiming::Instance();
  {
    QsaSubTimingGuard guard{true};
    timer.Begin(1, 1);
    timer.Mark(0, 12, 0, nullptr);
  }
  Q4T_CHECK(!timer.active && !timer.qsa_valid);
  Q4T_CHECK(fake_cuda::live == 0);
  timer.Begin(1, 1);
  CompleteLayer(timer, 0);
  timer.Report(8192);
  Q4T_CHECK(timer.qsa_valid);
  Q4T_CHECK(fake_cuda::live == 0);
  return true;
}

int main() { return q4t::test::RunAllTests(); }
