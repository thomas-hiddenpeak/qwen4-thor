#include "q4t/server/asr_stream_input.h"

#include <array>
#include <future>
#include <limits>
#include <thread>
#include <vector>

#include "q4t/server/realtime_asr_policy.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
}  // namespace

Q4T_TEST(asr_stream_input_append_during_borrow_never_overwrites_prefix) {
  AsrStreamInput input(1, 12, 4);
  const std::array<float, 4> first{.25, -.5, 1.2, -1.1};
  Q4T_CHECK(input.Valid() && input.StorageBytes() == 48);
  Q4T_CHECK(input.Append(1, first));
  const auto borrowed = input.Read(Clock::now() + 1s);
  Q4T_CHECK(borrowed.kind == AsrStreamReadKind::kReady &&
            borrowed.samples.size() == 4);
  auto producer = std::async(std::launch::async, [&] {
    return bool(
               input.Append(1, std::array<float, 8>{2, 3, 4, 5, 6, 7, 8, 9})) &&
           bool(input.Seal(1));
  });
  // Independent memory locations: the reader keeps its exact prefix while
  // the producer fills the rest of the preallocated capacity.
  for (int iteration = 0; iteration < 1000; ++iteration)
    for (size_t i = 0; i < first.size(); ++i)
      Q4T_CHECK(borrowed.samples[i] == first[i]);
  Q4T_CHECK(producer.get() && input.Complete(borrowed.step));
  const auto second = input.Read(Clock::now() + 1s);
  Q4T_CHECK(second.kind == AsrStreamReadKind::kReady &&
            second.samples.size() == 8);
  Q4T_CHECK(second.samples.data() == borrowed.samples.data() &&
            second.samples[7] == 5);
  Q4T_CHECK(input.Complete(second.step));
  const auto final = input.Read(Clock::now() + 1s);
  Q4T_CHECK(final.kind == AsrStreamReadKind::kReady && final.step.final &&
            final.samples.size() == 12 && final.samples[11] == 9);
  Q4T_CHECK(input.Complete(final.step) && input.Drained());
  Q4T_CHECK(input.Read(Clock::now() + 1s).kind == AsrStreamReadKind::kEnd);
  return true;
}

Q4T_TEST(asr_stream_input_wait_wakes_for_real_samples_and_cancel) {
  for (bool cancel : {false, true}) {
    AsrStreamInput input(2, 8, 4);
    std::promise<void> entered;
    bool announced = false;
    auto worker = std::async(std::launch::async, [&] {
      return input.Read(Clock::now() + 1s, [&] {
        if (!announced) {
          entered.set_value();
          announced = true;
        }
        return false;
      });
    });
    entered.get_future().wait();
    if (cancel)
      Q4T_CHECK(input.Cancel(2));
    else
      Q4T_CHECK(input.Append(2, std::array<float, 4>{1, 2, 3, 4}));
    const auto read = worker.get();
    Q4T_CHECK(read.kind == (cancel ? AsrStreamReadKind::kCancelled
                                   : AsrStreamReadKind::kReady));
    Q4T_CHECK(!input.Drained());
    if (!cancel) Q4T_CHECK(input.Complete(read.step));
  }
  return true;
}

Q4T_TEST(asr_stream_input_cancel_keeps_borrow_and_never_reports_eof) {
  AsrStreamInput input(3, 8, 4);
  Q4T_CHECK(input.Append(3, std::array<float, 4>{.1, .2, .3, .4}));
  const auto read = input.Read(Clock::now() + 1s);
  Q4T_CHECK(read.kind == AsrStreamReadKind::kReady && input.Cancel(3));
  Q4T_CHECK(read.samples[3] == .4f && input.StorageBytes() == 32);
  Q4T_CHECK(!input.Append(3, std::array<float, 1>{0}));
  Q4T_CHECK(input.Complete(read.step) && !input.Drained());
  Q4T_CHECK(input.Read(Clock::now() + 1s).kind ==
            AsrStreamReadKind::kCancelled);
  Q4T_CHECK(!input.Complete(read.step));
  return true;
}

Q4T_TEST(
    asr_stream_input_deadline_bad_samples_and_step_exhaustion_are_not_eof) {
  AsrStreamInput input(4, 8, 4, 1);
  Q4T_CHECK(!input.Append(
      4, std::array<float, 1>{std::numeric_limits<float>::infinity()}));
  Q4T_CHECK(!input.Append(3, std::array<float, 1>{1}) && input.Received() == 0);
  Q4T_CHECK(input.Read(Clock::now()).kind == AsrStreamReadKind::kDeadline);
  Q4T_CHECK(input.Read(Clock::now() + 1s, [] { return true; }).kind ==
            AsrStreamReadKind::kCancelled);
  Q4T_CHECK(input.Append(4, std::array<float, 5>{1, 2, 3, 4, 5}));
  Q4T_CHECK(!input.Append(4, std::array<float, 4>{6, 7, 8, 9}));
  Q4T_CHECK(input.Received() == 5 && input.Seal(4));
  const auto first = input.Read(Clock::now() + 1s);
  Q4T_CHECK(first.kind == AsrStreamReadKind::kReady && !first.step.final &&
            input.Complete(first.step));
  Q4T_CHECK(input.Read(Clock::now() + 1s).kind == AsrStreamReadKind::kInvalid);
  Q4T_CHECK(!input.Drained());
  AsrStreamInput invalid(0, 8, 4);
  Q4T_CHECK(!invalid.Valid() && invalid.StorageBytes() == 0 &&
            invalid.Read(Clock::now() + 1s).kind ==
                AsrStreamReadKind::kInvalid);
  return true;
}

Q4T_TEST(realtime_asr_delay_selects_actual_sample_context_and_revision_budget) {
  const std::array<std::string_view, 5> names{"minimal", "low", "medium",
                                              "high", "xhigh"};
  const std::array<size_t, 5> counts{8000, 16000, 32000, 64000, 128000};
  const std::array<size_t, 5> rollback{2, 3, 5, 7, 9};
  for (size_t i = 0; i < names.size(); ++i) {
    const auto policy = RealtimeAsrDelayPolicy(names[i]);
    Q4T_CHECK(policy.Valid() && policy.chunk_samples == counts[i] &&
              policy.prefix.rollback_tokens == rollback[i] &&
              policy.prefix.unfixed_passes == 2);
    AsrStreamInput input(1, 160000, policy.chunk_samples);
    const std::vector<float> samples(counts[i], .25f);
    Q4T_CHECK(input.Append(1, samples));
    const auto read = input.Read(Clock::now() + 1s);
    Q4T_CHECK(read.kind == AsrStreamReadKind::kReady &&
              read.samples.size() == counts[i]);
    Q4T_CHECK(input.Seal(1) && input.Complete(read.step));
    Q4T_CHECK(input.Read(Clock::now() + 1s).kind == AsrStreamReadKind::kEnd);
  }
  for (auto invalid : {"", "auto", "MINIMAL", "minimal "})
    Q4T_CHECK(!RealtimeAsrDelayPolicy(invalid).Valid());
  return true;
}
