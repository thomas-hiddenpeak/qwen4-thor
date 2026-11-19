#include "q4t/server/asr_stream_state.h"

#include <algorithm>
#include <array>
#include <limits>

#include "q4t/test.h"

namespace {
using namespace q4t::server;
using Tokens = std::vector<AsrStreamToken>;
Tokens Words() {
  return {{1, "Hello"}, {2, " world"}, {3, " this"}, {4, " is"},
          {5, " a"},    {6, " test"},  {7, "."}};
}
}  // namespace

Q4T_TEST(asr_stream_latest_prefix_catches_up_without_skipping_audio_or_borrow) {
  AsrStreamSchedule state(1, 4, 20, 2);
  Q4T_CHECK(state.Append(1, 11));
  AsrStreamStep first;
  Q4T_CHECK(state.Next(1, &first, true));
  Q4T_CHECK(first.samples == 11 && first.sequence == 1 && !first.final);
  Q4T_CHECK(state.Append(1, 9) && state.Seal(1));
  AsrStreamStep rejected;
  Q4T_CHECK(!state.Next(1, &rejected, true));
  Q4T_CHECK(state.Complete(first) && state.Evaluated() == 11);
  AsrStreamStep last;
  Q4T_CHECK(state.Next(1, &last, true));
  Q4T_CHECK(last.samples == 20 && last.sequence == 2 && last.final);
  Q4T_CHECK(state.Complete(last) && state.Drained() && state.Steps() == 2);
  return true;
}

Q4T_TEST(asr_stream_latest_prefix_keeps_readiness_epoch_and_actual_step_bound) {
  AsrStreamSchedule state(1, 4, 20, 1);
  AsrStreamStep step;
  Q4T_CHECK(state.Append(1, 3) && !state.Next(1, &step, true));
  Q4T_CHECK(state.Append(1, 1) && !state.Next(2, &step, true));
  Q4T_CHECK(state.Next(1, &step, true) && state.Complete(step));
  Q4T_CHECK(state.Append(1, 10) && state.Seal(1));
  Q4T_CHECK(!state.Next(1, &step, true) && !state.Drained());
  Q4T_CHECK(state.Received() == 14 && state.Evaluated() == 4);
  return true;
}

Q4T_TEST(asr_stream_samples_partition_invariant_without_future_audio) {
  constexpr uint64_t total = 70001;
  for (const uint64_t fragment : {1ULL, 137ULL, 16001ULL, 70001ULL}) {
    AsrStreamSchedule state(9, 32000, total, 3);
    Q4T_CHECK(state.Valid());
    std::vector<uint64_t> extents;
    for (uint64_t received = 0; received < total;) {
      const auto count = std::min(fragment, total - received);
      Q4T_CHECK(state.Append(9, count));
      received += count;
      while (state.Ready()) {
        AsrStreamStep step;
        Q4T_CHECK(state.Next(9, &step));
        Q4T_CHECK(!step.final && step.samples <= received);
        extents.push_back(step.samples);
        Q4T_CHECK(state.Complete(step));
      }
    }
    Q4T_CHECK(!state.Drained() && state.Seal(9));
    AsrStreamStep tail;
    Q4T_CHECK(state.Next(9, &tail));
    Q4T_CHECK(tail.final && tail.samples == total);
    extents.push_back(tail.samples);
    Q4T_CHECK(state.Complete(tail));
    Q4T_CHECK((extents == std::vector<uint64_t>{32000, 64000, 70001}));
    Q4T_CHECK(state.Drained() && !state.Ready());
    Q4T_CHECK(!state.Next(9, &tail) && !state.Append(9, 1));
  }
  return true;
}

Q4T_TEST(asr_stream_seal_does_not_change_borrow_or_invent_final_step) {
  for (uint64_t count : {1ULL, 32000ULL}) {
    AsrStreamSchedule state(1, 32000, 32000, 1);
    Q4T_CHECK(!state.Seal(1) && state.Append(1, count));
    if (count == 1) Q4T_CHECK(state.Seal(1));
    AsrStreamStep step;
    Q4T_CHECK(state.Next(1, &step));
    Q4T_CHECK(step.samples == count && step.final == (count == 1));
    Q4T_CHECK(state.Seal(1));
    // Exact-boundary EOF arrives while the executor owns a non-final ticket.
    // Finalization reuses its actual EOS; there is no second model invocation.
    Q4T_CHECK(state.Complete(step));
    Q4T_CHECK(state.Drained() && state.Steps() == 1 && !state.Ready());
    Q4T_CHECK(!state.Complete(step) && !state.Next(1, &step));
  }
  return true;
}

Q4T_TEST(asr_stream_tickets_epochs_and_sample_step_limits) {
  AsrStreamSchedule state(2, 4, 12, 2);
  Q4T_CHECK(state.Append(2, 4));
  AsrStreamStep step{99, 99, 99, true};
  Q4T_CHECK(!state.Next(1, &step) && step.epoch == 99);
  Q4T_CHECK(!state.Next(2, nullptr) && !state.Cancel(1));
  Q4T_CHECK(!state.Append(1, 1) && state.Received() == 4);
  Q4T_CHECK(state.Next(2, &step) && state.Append(2, 8));
  Q4T_CHECK(step.samples == 4 && !state.Ready());
  for (int field = 0; field < 4; ++field) {
    auto stale = step;
    if (field == 0) ++stale.epoch;
    if (field == 1) ++stale.sequence;
    if (field == 2) ++stale.samples;
    if (field == 3) stale.final = true;
    Q4T_CHECK(!state.Complete(stale) && state.InFlight());
  }
  Q4T_CHECK(!state.Append(2, std::numeric_limits<uint64_t>::max()));
  Q4T_CHECK(state.Received() == 12 && state.Complete(step));
  Q4T_CHECK(state.Next(2, &step) && step.samples == 8);
  Q4T_CHECK(state.Complete(step) && state.Seal(2) && state.Ready());
  // Work remains, but the finite step budget fails explicitly, not as EOF.
  Q4T_CHECK(!state.Next(2, &step) && !state.Drained());
  Q4T_CHECK(state.Evaluated() == 8 && state.Steps() == 2);
  return true;
}

Q4T_TEST(asr_stream_cancel_reclaims_only_matching_borrow_without_success) {
  AsrStreamSchedule state(3, 4, 8, 2);
  Q4T_CHECK(state.Append(3, 4));
  AsrStreamStep step;
  Q4T_CHECK(state.Next(3, &step) && state.Cancel(3));
  Q4T_CHECK(state.InFlight() && state.Received() == 4);
  Q4T_CHECK(!state.Ready() && !state.Seal(3) && !state.Append(3, 0));
  Q4T_CHECK(state.Complete(step) && !state.InFlight());
  Q4T_CHECK(state.Cancelled() && !state.Drained() && state.Evaluated() == 0);
  Q4T_CHECK(!state.Complete(step) && !state.Next(3, &step));
  return true;
}

Q4T_TEST(asr_stream_invalid_limits_never_become_live) {
  for (auto numbers : std::array<std::array<uint64_t, 4>, 8>{
           {{0, 1, 1, 1},
            {1, 0, 1, 1},
            {1, 128001, 1, 1},
            {1, 1, 0, 1},
            {1, 1, 9600001, 1},
            {1, 1, 1, 0},
            {1, 1, 1, 4097},
            {1, 1, std::numeric_limits<uint64_t>::max(), 1}}}) {
    AsrStreamSchedule state(numbers[0], numbers[1], numbers[2], numbers[3]);
    AsrStreamStep step;
    Q4T_CHECK(!state.Valid() && !state.Append(numbers[0], 1));
    Q4T_CHECK(!state.Seal(numbers[0]) && !state.Next(numbers[0], &step));
    Q4T_CHECK(!state.Cancel(numbers[0]) && !state.Drained());
  }
  for (int field = 0; field < 7; ++field) {
    AsrStreamPrefixLimits limits;
    if (field == 0) limits.tokens = 0;
    if (field == 1) limits.tokens = 4097;
    if (field == 2) limits.bytes = 0;
    if (field == 3) limits.bytes = 65537;
    if (field == 4) limits.unfixed_passes = 17;
    if (field == 5) limits.rollback_tokens = 4097;
    if (field == 6) limits.vocabulary = 0;
    AsrStreamPrefix state(1, limits);
    Q4T_CHECK(!state.Valid() && !state.Accept(1, Words(), true, true));
    Q4T_CHECK(!state.Finalize(1) && !state.Finished());
  }
  Q4T_CHECK(!AsrStreamPrefix(0).Valid());
  return true;
}

Q4T_TEST(asr_stream_warmup_and_rollback_keep_actual_immutable_prefix) {
  AsrStreamPrefix state(4);
  auto proposal = Words();
  Q4T_CHECK(state.Accept(4, proposal, true, false) && state.Prefix().empty());
  proposal[0] = {10, "Hi"};  // First two real invocations have no fixed prefix.
  Q4T_CHECK(state.Accept(4, proposal, true, false));
  Q4T_CHECK(state.Prefix().size() == 2 && state.Prefix()[0] == proposal[0]);
  proposal[2] = {11, ", this"};  // Only the unfixed suffix can be revised.
  proposal.push_back({12, " Today"});
  Q4T_CHECK(state.Accept(4, proposal, true, false));
  Q4T_CHECK(state.Prefix().size() == 3 && state.Prefix()[2].id == 11);
  proposal[0].id = 99;  // Equal visible bytes do not mean equal model input.
  Q4T_CHECK(!state.Accept(4, proposal, true, true));
  Q4T_CHECK(state.Failed() && !state.Finished() && state.Passes() == 3);
  Q4T_CHECK(state.Prefix()[0].id == 10 && !state.Finalize(4));
  return true;
}

Q4T_TEST(asr_stream_prefix_never_splits_or_repairs_utf8) {
  for (const std::string scalar :
       {"\xc2\xa2", "\xe4\xbd\xa0", "\xf0\x9f\x8e\x99", "\xef\xbf\xbd"}) {
    for (size_t split = 1; split < scalar.size(); ++split) {
      AsrStreamPrefixLimits limits;
      limits.unfixed_passes = 0;
      limits.rollback_tokens = 1;
      AsrStreamPrefix state(5, limits);
      Tokens proposal{
          {1, "A"}, {2, scalar.substr(0, split)}, {3, scalar.substr(split)}};
      Q4T_CHECK(state.Accept(5, proposal, true, false));
      Q4T_CHECK(state.Prefix().size() == 1);
      proposal.push_back({4, " tail"});
      Q4T_CHECK(state.Accept(5, proposal, true, false));
      Q4T_CHECK(state.Prefix().size() == 3);
      Q4T_CHECK(state.Prefix()[1].bytes + state.Prefix()[2].bytes == scalar);
    }
  }
  return true;
}

Q4T_TEST(asr_stream_malformed_suffix_withheld_until_real_finalization) {
  for (const std::string bytes :
       {"\xff", "\xc0\xaf", "\xe4\xbd", "\xed\xa0\x80"}) {
    AsrStreamPrefixLimits limits;
    limits.unfixed_passes = limits.rollback_tokens = 0;
    AsrStreamPrefix state(6, limits);
    Tokens proposal{{1, "valid"}, {2, bytes}, {3, " tail"}};
    Q4T_CHECK(state.Accept(6, proposal, true, true));
    Q4T_CHECK(state.Finished() && state.Prefix().size() == 1);
    // Final output goes through the ordinary transcript UTF8 mapper. The
    // prefix state preserves original tokens/bytes instead of forging repair.
    Q4T_CHECK(state.Hypothesis()[1].bytes == bytes);
    Q4T_CHECK(!state.Accept(6, proposal, true, false));
  }
  return true;
}

Q4T_TEST(asr_stream_invalid_hypotheses_fail_sticky_preserve_last_result) {
  for (int fault = 0; fault < 7; ++fault) {
    AsrStreamPrefixLimits limits;
    limits.tokens = 8;
    limits.bytes = 32;
    limits.rollback_tokens = 2;
    limits.unfixed_passes = 0;
    AsrStreamPrefix state(7, limits);
    auto proposal = Words();
    Q4T_CHECK(state.Accept(7, proposal, true, false));
    const auto previous = proposal;
    if (fault == 0) proposal[5].id = -1;
    if (fault == 1) proposal[5].id = limits.vocabulary;
    if (fault == 2) proposal.resize(4);
    if (fault == 3) proposal[0].bytes = "different";
    if (fault == 4) proposal[5].bytes = std::string(33, 'x');
    if (fault == 5) proposal.resize(9, {9, "x"});
    Q4T_CHECK(!state.Accept(7, proposal, fault != 6, true));
    Q4T_CHECK(state.Failed() && !state.Finished() && state.Passes() == 1);
    Q4T_CHECK(std::ranges::equal(state.Hypothesis(), previous));
    Q4T_CHECK(!state.Accept(7, previous, true, true) && !state.Finalize(7));
  }
  return true;
}

Q4T_TEST(asr_stream_stale_owner_and_eof_cannot_fake_first_result) {
  AsrStreamPrefix state(8);
  Q4T_CHECK(!state.Finalize(8) && !state.Finished());
  Q4T_CHECK(!state.Accept(7, Words(), true, true) && !state.Failed());
  Q4T_CHECK(!state.Cancel(7));
  Q4T_CHECK(state.Accept(8, Words(), true, false));
  Q4T_CHECK(!state.Finalize(7) && !state.Finished());
  Q4T_CHECK(state.Finalize(8) && state.Finished() && state.Passes() == 1);
  Q4T_CHECK(state.Finalize(8) && !state.Accept(8, Words(), true, true));
  return true;
}

Q4T_TEST(asr_stream_cancel_retains_token_storage_and_suppresses_success) {
  for (bool final : {false, true}) {
    AsrStreamPrefix state(9);
    Q4T_CHECK(state.Accept(9, Words(), true, final));
    const auto borrowed = state.Hypothesis();
    Q4T_CHECK(state.Cancel(9) && state.Cancelled() && !state.Finished());
    Q4T_CHECK(borrowed.data() == state.Hypothesis().data());
    Q4T_CHECK(borrowed[0].bytes == "Hello" && borrowed.size() == 7);
    Q4T_CHECK(!state.Accept(9, Words(), true, true) && !state.Finalize(9));
  }
  return true;
}
