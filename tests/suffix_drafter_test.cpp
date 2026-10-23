#include "q4t/mtp/suffix_drafter.h"

#include <array>
#include <cstdint>
#include <span>
#include <vector>

#include "q4t/test.h"

namespace {

using q4t::mtp::SuffixDrafter;
using q4t::mtp::SuffixReason;

// A scan over complete suffix lengths and every earlier window. It neither
// consults the index nor extends candidate matches backward. Fixtures using
// this oracle contain fewer than four occurrences of their final trigram.
q4t::mtp::SuffixProposal ScanReference(std::span<const int32_t> committed,
                                       int32_t pending, int32_t d0) {
  std::vector<int32_t> view(committed.begin(), committed.end());
  view.push_back(pending);
  q4t::mtp::SuffixProposal result;
  result.reason = SuffixReason::kNoMatch;
  for (int length = SuffixDrafter::kMinMatch;
       length <= SuffixDrafter::kMaxMatch &&
       static_cast<size_t>(length) <= view.size();
       ++length) {
    const size_t query_start = view.size() - length;
    for (size_t start = 0; start + length <= committed.size(); ++start) {
      bool equal = true;
      for (int i = 0; i < length; ++i) {
        equal = equal && view[start + i] == view[query_start + i];
      }
      if (equal) {
        result.match_length = length;
        result.match_end = static_cast<uint32_t>(start + length - 1);
      }
    }
  }
  if (result.match_length == 0) return result;
  if (view.size() - result.match_end - 1 < SuffixDrafter::kDraftCount) {
    result.reason = SuffixReason::kIncomplete;
    return result;
  }
  for (size_t i = 0; i < result.tokens.size(); ++i) {
    result.tokens[i] = view[result.match_end + i + 1];
  }
  result.ready = result.tokens[0] == d0;
  result.reason =
      result.ready ? SuffixReason::kReady : SuffixReason::kFirstTokenMismatch;
  return result;
}

// Fixture construction only: find two different trigrams that enter the
// same bounded bucket, then check the real query compares their token IDs.
size_t FixtureBucket(int32_t a, int32_t b, int32_t c) {
  const auto mix = [](uint64_t x) {
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33;
    x *= 0xc4ceb9fe1a85ec53ULL;
    return x ^ (x >> 33);
  };
  return mix(static_cast<uint64_t>(a) * 0x9E3779B97F4A7C15ULL ^
             mix(static_cast<uint64_t>(b) + 0x632BE59BD9B4E019ULL) ^
             (static_cast<uint64_t>(c) << 1)) &
         (SuffixDrafter::kBucketCount - 1);
}

}  // namespace

Q4T_TEST(suffix_pending_view_global_ids_and_query_immutability) {
  SuffixDrafter drafter;
  const std::vector<int32_t> history{1, 2, 3, 100001, 65536, 77, 9, 1, 2};
  Q4T_CHECK(drafter.Init(history, 200000, 64).ok());
  const auto first = drafter.ProposeAfter(3, 100001);
  Q4T_CHECK(first.ready);
  const std::array<int32_t, 3> expected{100001, 65536, 77};
  Q4T_CHECK(first.tokens == expected);
  Q4T_CHECK(first.match_length == 3 && first.match_end == 2);
  Q4T_CHECK(std::vector<int32_t>(drafter.History().begin(),
                                 drafter.History().end()) == history);
  Q4T_CHECK(drafter.ProposeAfter(3, 100001).tokens == first.tokens);
  Q4T_CHECK(drafter.Counters().hits == 2);
  Q4T_CHECK(drafter.Size() == history.size());
  return true;
}

Q4T_TEST(suffix_longest_match_and_newest_tie_independent_scan) {
  SuffixDrafter drafter;
  for (int32_t prefix : {9, 6}) {
    const std::vector<int32_t> history{9, 1, 2, 3, 4, 5,      6, 8, 1,
                                       2, 3, 7, 8, 9, prefix, 1, 2};
    Q4T_CHECK(drafter.Init(history, 100, 64).ok());
    for (int32_t d0 : {4, 7, 8}) {
      const auto expected = ScanReference(history, 3, d0);
      const auto actual = drafter.ProposeAfter(3, d0);
      Q4T_CHECK(actual.ready == expected.ready);
      Q4T_CHECK(actual.tokens == expected.tokens);
      Q4T_CHECK(actual.match_length == expected.match_length);
      Q4T_CHECK(actual.match_end == expected.match_end);
      Q4T_CHECK(actual.reason == expected.reason);
    }
  }
  return true;
}

Q4T_TEST(suffix_longest_candidate_does_not_search_around_d0_gate) {
  SuffixDrafter drafter;
  const std::vector<int32_t> history{9, 1, 2, 3, 4, 5, 6, 8, 1,
                                     2, 3, 7, 8, 9, 9, 1, 2};
  Q4T_CHECK(drafter.Init(history, 100, 64).ok());
  const auto result = drafter.ProposeAfter(3, 7);
  Q4T_CHECK(!result.ready);
  Q4T_CHECK(result.reason == SuffixReason::kFirstTokenMismatch);
  Q4T_CHECK(result.match_end == 3 && result.match_length == 4);
  Q4T_CHECK(result.tokens[0] == 4);
  return true;
}

Q4T_TEST(suffix_requires_complete_three_and_allows_periodic_pending) {
  SuffixDrafter drafter;
  const std::vector<int32_t> incomplete{1, 1, 1, 1, 1, 1};
  Q4T_CHECK(drafter.Init(incomplete, 100, 16).ok());
  const auto short_result = drafter.ProposeAfter(1, 1);
  Q4T_CHECK(!short_result.ready);
  Q4T_CHECK(short_result.match_end == 5);
  Q4T_CHECK(short_result.reason == SuffixReason::kIncomplete);
  const std::vector<int32_t> periodic{1, 2, 3, 1, 2};
  Q4T_CHECK(drafter.Init(periodic, 100, 16).ok());
  const auto result = drafter.ProposeAfter(3, 1);
  const std::array<int32_t, 3> expected{1, 2, 3};
  Q4T_CHECK(result.ready && result.tokens == expected);
  Q4T_CHECK(drafter.Size() == periodic.size());
  return true;
}

Q4T_TEST(suffix_match_range_and_candidate_count_are_bounded) {
  SuffixDrafter drafter;
  std::vector<int32_t> history;
  for (int32_t token = 1; token <= 70; ++token) history.push_back(token);
  history.insert(history.end(), {101, 102, 103, 900});
  for (int32_t token = 1; token < 70; ++token) history.push_back(token);
  Q4T_CHECK(drafter.Init(history, 1000, 256).ok());
  const auto result = drafter.ProposeAfter(70, 101);
  Q4T_CHECK(result.ready && result.match_length == 64);
  Q4T_CHECK(result.candidates_examined <= 4);
  Q4T_CHECK(result.compared_tokens <= 4 * 64);
  history.clear();
  for (int32_t token = 10; token < 16; ++token) {
    history.insert(history.end(), {9, 1, 2, 3, token, 6, 7});
  }
  history.insert(history.end(), {8, 1, 2});
  Q4T_CHECK(drafter.Init(history, 1000, 256).ok());
  const auto latest = drafter.ProposeAfter(3, 15);
  Q4T_CHECK(latest.ready);
  Q4T_CHECK(latest.candidates_examined == 4);
  Q4T_CHECK(latest.tokens[0] == 15);
  return true;
}

Q4T_TEST(suffix_hash_collision_requires_exact_token_comparison) {
  std::vector<int32_t> buckets(SuffixDrafter::kBucketCount, -1);
  int32_t first = -1;
  int32_t second = -1;
  for (int32_t token = 10; token <= 65546; ++token) {
    const size_t bucket = FixtureBucket(1, 2, token);
    if (buckets[bucket] >= 0) {
      first = buckets[bucket];
      second = token;
      break;
    }
    buckets[bucket] = token;
  }
  Q4T_CHECK(first >= 10 && second > first);
  SuffixDrafter drafter;
  const std::vector<int32_t> history{1, 2, first, 5, 6, 7, 1, 2};
  Q4T_CHECK(drafter.Init(history, 100000, 32).ok());
  const auto result = drafter.ProposeAfter(second, 5);
  Q4T_CHECK(result.candidates_examined > 0);
  Q4T_CHECK(!result.ready && result.match_length < 3);
  Q4T_CHECK(result.reason == SuffixReason::kNoMatch);
  return true;
}

Q4T_TEST(suffix_appends_only_supplied_committed_prefix) {
  SuffixDrafter drafter;
  std::vector<int32_t> committed{1, 2, 3, 4, 5, 6, 9, 1, 2};
  Q4T_CHECK(drafter.Init(committed, 100, 64).ok());
  Q4T_CHECK(drafter.ProposeAfter(3, 4).ready);
  // Only pending 3 and accepted draft 4 committed. The other proposal rows
  // are not submitted to Append. Querying or rejecting a row cannot index it.
  const std::array<int32_t, 2> accepted_inputs{3, 4};
  Q4T_CHECK(drafter.Append(accepted_inputs).ok());
  committed.insert(committed.end(), accepted_inputs.begin(),
                   accepted_inputs.end());
  Q4T_CHECK(drafter.Size() == committed.size());
  Q4T_CHECK(std::vector<int32_t>(drafter.History().begin(),
                                 drafter.History().end()) == committed);
  const auto expected = ScanReference(committed, 5, 6);
  const auto actual = drafter.ProposeAfter(5, 6);
  Q4T_CHECK(actual.ready && actual.tokens == expected.tokens);
  Q4T_CHECK(drafter.Counters().appended_tokens == 2);
  return true;
}

Q4T_TEST(suffix_capacity_exhaustion_is_transactional_neural_fallback) {
  SuffixDrafter drafter;
  const std::array<int32_t, 2> initial{1, 2};
  Q4T_CHECK(drafter.Init(initial, 100, 4).ok());
  const std::array<int32_t, 2> accepted{3, 4};
  Q4T_CHECK(drafter.Append(accepted).ok());
  const std::array<int32_t, 2> over_budget{5, 6};
  Q4T_CHECK(drafter.Append(over_budget).ok());
  Q4T_CHECK(!drafter.Active() && drafter.Size() == 4);
  Q4T_CHECK(drafter.DisabledReason() == SuffixReason::kCapacityExhausted);
  Q4T_CHECK(drafter.Counters().capacity_exhaustions == 1);
  Q4T_CHECK(drafter.ProposeAfter(5, 6).reason ==
            SuffixReason::kCapacityExhausted);
  Q4T_CHECK(drafter.Append(over_budget).ok());
  Q4T_CHECK(drafter.Size() == 4);
  Q4T_CHECK(drafter.Counters().capacity_exhaustions == 1);
  return true;
}

Q4T_TEST(suffix_invalid_inputs_do_not_partially_index_or_allocate) {
  SuffixDrafter drafter;
  const std::array<int32_t, 2> valid{1, 2};
  const std::array<int32_t, 2> invalid{3, 100};
  Q4T_CHECK(!drafter.Init(valid, 0, 16).ok());
  Q4T_CHECK(!drafter.Init(valid, 100, 0).ok());
  Q4T_CHECK(!drafter.Init(valid, 100, SuffixDrafter::kMaxCapacity + 1).ok());
  Q4T_CHECK(!drafter.Init(invalid, 100, 16).ok());
  Q4T_CHECK(drafter.MemoryBytes() == 0 && !drafter.Active());
  Q4T_CHECK(drafter.Init(valid, 100, 16).ok());
  Q4T_CHECK(drafter.ProposeAfter(-1, 2).reason == SuffixReason::kInvalidToken);
  Q4T_CHECK(drafter.ProposeAfter(1, 100).reason == SuffixReason::kInvalidToken);
  Q4T_CHECK(drafter.Size() == 2 && drafter.Active());
  Q4T_CHECK(!drafter.Append(invalid).ok());
  Q4T_CHECK(drafter.Size() == 2 && !drafter.Active());
  return true;
}

Q4T_TEST(suffix_reset_releases_storage_and_request_history) {
  SuffixDrafter drafter;
  const std::vector<int32_t> first{1, 2, 3, 4, 5, 6, 1, 2};
  Q4T_CHECK(drafter.Init(first, 100).ok());
  Q4T_CHECK(drafter.MemoryBytes() == 2 * 1024 * 1024);
  Q4T_CHECK(drafter.MemoryBytes() == SuffixDrafter::kMaxMemoryBytes);
  Q4T_CHECK(drafter.ProposeAfter(3, 4).ready);
  drafter.Reset();
  Q4T_CHECK(!drafter.Active() && drafter.MemoryBytes() == 0);
  Q4T_CHECK(drafter.Size() == 0 && drafter.History().empty());
  Q4T_CHECK(drafter.Counters().hits == 1);
  Q4T_CHECK(drafter.Counters().peak_memory_bytes ==
            SuffixDrafter::kMaxMemoryBytes);
  const std::array<int32_t, 3> second{9, 1, 2};
  Q4T_CHECK(drafter.Init(second, 100, 8).ok());
  Q4T_CHECK(!drafter.ProposeAfter(3, 4).ready);
  Q4T_CHECK(drafter.Counters().hits == 0);
  Q4T_CHECK(drafter.MemoryBytes() == SuffixDrafter::kIndexBytes + 32);
  return true;
}
