#include "q4t/vision/forward_timing_core.h"

#include <array>
#include <cstdint>
#include <limits>
#include <string>
#include <utility>

#include "q4t/test.h"

namespace {
using q4t::vision::ForwardStage;

struct EventState {
  std::array<float, 64> stamps{};
  int creates = 0;
  int records = 0;
  int destroys = 0;
  int reads = 0;
  int fail_create = 0;
  int fail_record = 0;
  int fail_read = 0;
  float now = 0;
  bool wrong_stream = false;
};

// Deliberately has no synchronization API: only the caller can complete work.
struct FakeEvents {
  using Event = int;
  using Stream = int;
  EventState* state;
  int Create(int* event) {
    *event = ++state->creates;
    return *event == state->fail_create ? 11 : 0;
  }
  int Record(int event, int stream) {
    state->wrong_stream |= stream != 7;
    if (++state->records == state->fail_record) return 12;
    state->stamps.at(event) = state->now;
    return 0;
  }
  int Elapsed(float* ms, int begin, int end) {
    if (++state->reads == state->fail_read) return 13;
    *ms = state->stamps.at(end) - state->stamps.at(begin);
    return 0;
  }
  void Destroy(int) { ++state->destroys; }
};
using Timing = q4t::vision::ForwardEventTiming<FakeEvents>;

bool RecordComplete(Timing& timing, EventState& state) {
  // One block and one item, with two separate norm intervals.
  constexpr std::array stages{
      ForwardStage::kPatch,         ForwardStage::kPositionTables,
      ForwardStage::kPositionEmbed, ForwardStage::kNorm,
      ForwardStage::kQkv,           ForwardStage::kRope,
      ForwardStage::kAttention,     ForwardStage::kProjection,
      ForwardStage::kNorm,          ForwardStage::kMlp,
      ForwardStage::kMerger};
  if (!timing.Prepare(1, 1) || !timing.Start(7)) return false;
  for (size_t i = 0; i < stages.size(); ++i) {
    state.now += 2;  // Unclassified submission gaps must survive accounting.
    if (!timing.Begin(stages[i])) return false;
    state.now += static_cast<float>(i + 1);
    if (!timing.End()) return false;
  }
  state.now += 3;
  return timing.Finish();
}

std::string Report(const Timing& timing) {
  FILE* file = std::tmpfile();
  if (!file) return "failed_tmpfile";
  timing.Report(file, "test", 1, 64, 1, 0, 0, 0);
  std::rewind(file);
  std::string text;
  char buffer[2048];
  while (std::fgets(buffer, sizeof(buffer), file)) text += buffer;
  std::fclose(file);
  return text;
}
}  // namespace

Q4T_TEST(vision_forward_timing_disabled_has_no_backend_calls) {
  EventState state;
  {
    Timing timing(false, FakeEvents{&state});
    Q4T_CHECK(RecordComplete(timing, state));
    Q4T_CHECK(timing.CollectAfterCompletion());
    Q4T_CHECK(!timing.Valid());
    Q4T_CHECK(Report(timing).empty());
  }
  Q4T_CHECK(state.creates == 0 && state.records == 0 && state.reads == 0 &&
            state.destroys == 0);
  return true;
}

Q4T_TEST(vision_forward_timing_partitions_repeated_stages_and_residual) {
  EventState state;
  {
    Timing timing(true, FakeEvents{&state});
    Q4T_CHECK(RecordComplete(timing, state));
    Q4T_CHECK(state.reads == 0);  // Never read during submission.
    Q4T_CHECK(timing.CollectAfterCompletion() && timing.Valid());
    Q4T_CHECK(timing.TotalMs() == 91 && timing.OtherMs() == 25);
    Q4T_CHECK(timing.Milliseconds(ForwardStage::kNorm) == 13);
    Q4T_CHECK(timing.Calls(ForwardStage::kNorm) == 2);
    Q4T_CHECK(timing.Milliseconds(ForwardStage::kAttention) == 7);
    Q4T_CHECK(timing.Recorded() == 24 && !state.wrong_stream);
    Q4T_CHECK(Report(timing).find("valid=1 clock=cuda_stream") !=
              std::string::npos);
  }
  Q4T_CHECK(state.destroys == state.creates && state.creates == 24);
  return true;
}

Q4T_TEST(vision_forward_timing_capacity_fails_before_allocation) {
  for (const auto& [items, depth] : std::array<std::pair<size_t, int>, 4>{
           {{0, 27}, {1, -1}, {SIZE_MAX, 27}, {1, INT32_MAX}}}) {
    EventState state;
    Timing timing(true, FakeEvents{&state});
    Q4T_CHECK(!timing.Prepare(items, depth));
    Q4T_CHECK(state.creates == 0 && !timing.Valid());
  }
  return true;
}

Q4T_TEST(vision_forward_timing_partial_creation_releases_only_owned_events) {
  EventState state;
  state.fail_create = 3;
  {
    Timing timing(true, FakeEvents{&state});
    Q4T_CHECK(!timing.Prepare(1, 1));
    Q4T_CHECK(timing.ErrorCode() == 11);
    Q4T_CHECK(!timing.Start(7));
    Q4T_CHECK(Report(timing).find("valid=0 error=create error_code=11") !=
              std::string::npos);
  }
  Q4T_CHECK(state.destroys == 2 && state.records == 0);
  return true;
}

Q4T_TEST(vision_forward_timing_early_return_never_reads_unfinished_events) {
  EventState state;
  {
    Timing timing(true, FakeEvents{&state});
    Q4T_CHECK(timing.Prepare(1, 1) && timing.Start(7));
    Q4T_CHECK(timing.Begin(ForwardStage::kPatch));
    Q4T_CHECK(!timing.Finish());
    Q4T_CHECK(!timing.CollectAfterCompletion() && !timing.Valid());
    Q4T_CHECK(Report(timing).find("valid=0 error=incomplete") !=
              std::string::npos);
  }
  Q4T_CHECK(state.reads == 0 && state.destroys == 24);
  return true;
}

Q4T_TEST(vision_forward_timing_record_and_read_errors_remain_invalid) {
  for (bool during_record : {true, false}) {
    EventState state;
    if (during_record)
      state.fail_record = 3;
    else
      state.fail_read = 3;
    {
      Timing timing(true, FakeEvents{&state});
      Q4T_CHECK(RecordComplete(timing, state) == !during_record);
      Q4T_CHECK(!timing.CollectAfterCompletion() && !timing.Valid());
      Q4T_CHECK(timing.ErrorCode() == (during_record ? 12 : 13));
      Q4T_CHECK(Report(timing).find("valid=0") != std::string::npos);
    }
    Q4T_CHECK(state.destroys == 24);
  }
  return true;
}

Q4T_TEST(vision_forward_timing_rejects_overlap_reuse_and_missing_intervals) {
  for (int fault = 0; fault < 3; ++fault) {
    EventState state;
    Timing timing(true, FakeEvents{&state});
    Q4T_CHECK(timing.Prepare(1, 1) && timing.Start(7));
    Q4T_CHECK(timing.Begin(ForwardStage::kPatch));
    if (fault == 0) Q4T_CHECK(!timing.Begin(ForwardStage::kNorm));
    if (fault == 1) Q4T_CHECK(!timing.Prepare(1, 1));
    if (fault == 2) {
      Q4T_CHECK(timing.End());
      Q4T_CHECK(!timing.Finish());
    }
    Q4T_CHECK(!timing.CollectAfterCompletion() && state.reads == 0);
  }
  return true;
}

Q4T_TEST(vision_forward_timing_rejects_nonfinite_or_reversed_timestamps) {
  for (float bad : {std::numeric_limits<float>::quiet_NaN(),
                    std::numeric_limits<float>::infinity(), -1.0f}) {
    EventState state;
    Timing timing(true, FakeEvents{&state});
    Q4T_CHECK(RecordComplete(timing, state));
    state.stamps[4] = bad;
    Q4T_CHECK(!timing.CollectAfterCompletion() && !timing.Valid());
    Q4T_CHECK(Report(timing).find("valid=0") != std::string::npos);
  }
  return true;
}
