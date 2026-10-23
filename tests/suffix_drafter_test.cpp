#include "q4t/mtp/suffix_drafter.h"

#include <array>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/test.h"

namespace {

using q4t::mtp::ClassifySuffixToken;
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
  if (result.ready && result.match_length < 32) {
    result.ready = false;
    result.reason = SuffixReason::kShortRejected;
  }
  return result;
}

std::vector<int32_t> LongCopy(int length) {
  std::vector<int32_t> history;
  for (int i = 1; i <= length; ++i) history.push_back(i);
  history.insert(history.end(), {100001, 65536, 77, 999});
  for (int i = 1; i < length; ++i) history.push_back(i);
  return history;
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
  const auto history = LongCopy(32);
  Q4T_CHECK(drafter.Init(history, 200000, 96).ok());
  const auto first = drafter.ProposeAfter(32, 100001);
  Q4T_CHECK(first.ready);
  const std::array<int32_t, 3> expected{100001, 65536, 77};
  Q4T_CHECK(first.tokens == expected);
  Q4T_CHECK(first.match_length == 32 && first.match_end == 31);
  Q4T_CHECK(std::vector<int32_t>(drafter.History().begin(),
                                 drafter.History().end()) == history);
  Q4T_CHECK(drafter.ProposeAfter(32, 100001).tokens == first.tokens);
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
  std::vector<int32_t> periodic;
  for (int i = 0; i < 11; ++i) periodic.insert(periodic.end(), {1, 2, 3});
  periodic.insert(periodic.end(), {1, 2});
  Q4T_CHECK(drafter.Init(periodic, 100, 64).ok());
  const auto result = drafter.ProposeAfter(3, 1);
  const std::array<int32_t, 3> expected{1, 2, 3};
  Q4T_CHECK(result.ready && result.tokens == expected);
  Q4T_CHECK(result.match_length == 33);
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
  Q4T_CHECK(!latest.ready && latest.reason == SuffixReason::kShortRejected);
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
  Q4T_CHECK(drafter.ProposeAfter(3, 4).reason == SuffixReason::kShortRejected);
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
  Q4T_CHECK(actual.ready == expected.ready && actual.tokens == expected.tokens);
  Q4T_CHECK(actual.reason == expected.reason);
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
  const auto first = LongCopy(32);
  Q4T_CHECK(drafter.Init(first, 200000).ok());
  Q4T_CHECK(drafter.MemoryBytes() == 2 * 1024 * 1024);
  Q4T_CHECK(drafter.MemoryBytes() == SuffixDrafter::kMaxMemoryBytes);
  Q4T_CHECK(drafter.ProposeAfter(32, 100001).ready);
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

Q4T_TEST(suffix_numeric_token_classification_byte_truth_table) {
  // A literal ASCII alphabet is independent of the implementation's ranges.
  const std::string_view allowed =
      "0123456789 \t\n\v\f\r!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";
  const std::string_view digits = "0123456789";
  for (int value = 0; value < 256; ++value) {
    const char byte = static_cast<char>(value);
    const uint8_t flags = ClassifySuffixToken(std::string_view(&byte, 1));
    const bool expected_allowed = allowed.find(byte) != std::string_view::npos;
    const bool expected_digit = digits.find(byte) != std::string_view::npos;
    Q4T_CHECK(((flags & q4t::mtp::kSuffixAllowedAscii) != 0) ==
              expected_allowed);
    Q4T_CHECK(((flags & q4t::mtp::kSuffixContainsDigit) != 0) ==
              expected_digit);
    Q4T_CHECK(flags <= 3);
  }
  Q4T_CHECK(ClassifySuffixToken("") == 0);
  Q4T_CHECK(ClassifySuffixToken(" (12.50):\r\n") == 3);
  Q4T_CHECK(ClassifySuffixToken("x1") == 2);
  Q4T_CHECK(ClassifySuffixToken("\xC2\xA0"
                                "1") == 2);
  Q4T_CHECK(ClassifySuffixToken(std::string_view("1\0", 2)) == 2);
  Q4T_CHECK(ClassifySuffixToken("<special>") == 0);
  return true;
}

Q4T_TEST(suffix_numeric_admission_is_independent_of_token_partition) {
  struct Fixture {
    std::string text;
    bool rejected;
  };
  const std::array<Fixture, 6> fixtures{{
      {", 123,", true},
      {"1 + 2", true},
      {"x 123", false},
      {" \t,.", false},
      {std::string("1\0,2", 4), false},
      {"12\xC2\xA0", false},
  }};
  const std::vector<int32_t> history{1, 2, 3, 4, 5, 6, 7, 1, 2};
  for (const auto& fixture : fixtures) {
    const std::string_view text = fixture.text;
    // Every partition into three nonempty tokens, including a split inside
    // a UTF-8 sequence. Admission concerns exact bytes, not Unicode decoding.
    for (size_t first = 1; first + 1 < text.size(); ++first) {
      for (size_t second = first + 1; second < text.size(); ++second) {
        std::array<uint8_t, 16> classes{};
        classes[1] = ClassifySuffixToken(text.substr(0, first));
        classes[2] = ClassifySuffixToken(text.substr(first, second - first));
        classes[3] = ClassifySuffixToken(text.substr(second));
        SuffixDrafter drafter;
        Q4T_CHECK(drafter.Init(history, classes.size(), 32, classes).ok());
        const auto proposal = drafter.ProposeAfter(3, 4);
        Q4T_CHECK(!proposal.ready);
        Q4T_CHECK(proposal.reason == (fixture.rejected
                                          ? SuffixReason::kNumericRejected
                                          : SuffixReason::kShortRejected));
        Q4T_CHECK(drafter.Counters().numeric_checks == 1);
        Q4T_CHECK(drafter.Counters().classified_tokens == 3);
      }
    }
  }
  return true;
}

Q4T_TEST(suffix_numeric_rejection_keeps_global_proposal_and_history) {
  std::vector<uint8_t> classes(200000);
  classes[70001] = ClassifySuffixToken(",");
  classes[70002] = ClassifySuffixToken(" ");
  classes[70003] = ClassifySuffixToken("1");
  const std::vector<int32_t> history{70001, 70002, 70003, 100001, 65536,
                                     77,    9,     70001, 70002};
  const auto expected = ScanReference(history, 70003, 100001);
  Q4T_CHECK(expected.tokens[0] == 100001);
  SuffixDrafter drafter;
  Q4T_CHECK(drafter.Init(history, classes.size(), 64, classes).ok());
  const auto actual = drafter.ProposeAfter(70003, 100001);
  Q4T_CHECK(!actual.ready && actual.reason == SuffixReason::kNumericRejected);
  Q4T_CHECK(actual.tokens == expected.tokens);
  Q4T_CHECK(actual.match_end == expected.match_end);
  Q4T_CHECK(actual.match_length == expected.match_length);
  Q4T_CHECK(std::string_view(q4t::mtp::SuffixReasonName(actual.reason)) ==
            "numeric_rejected");
  Q4T_CHECK(std::vector<int32_t>(drafter.History().begin(),
                                 drafter.History().end()) == history);
  Q4T_CHECK(drafter.Active() && drafter.Size() == history.size());
  Q4T_CHECK(drafter.Counters().numeric_checks == 1);
  Q4T_CHECK(drafter.Counters().classified_tokens == 3);
  Q4T_CHECK(drafter.Counters().numeric_rejected == 1);
  Q4T_CHECK(drafter.Counters().hits == 0);
  Q4T_CHECK(drafter.Counters().numeric_ns <= drafter.Counters().query_ns);
  Q4T_CHECK(drafter.MemoryBytes() == SuffixDrafter::kIndexBytes + 64 * 4);
  return true;
}

Q4T_TEST(suffix_numeric_uses_full_longest_match_not_only_trigram) {
  std::array<uint8_t, 100> classes{};
  classes[1] = ClassifySuffixToken(" ");
  classes[2] = ClassifySuffixToken("2");
  classes[3] = ClassifySuffixToken("5");
  classes[9] = ClassifySuffixToken("Record");
  const std::vector<int32_t> history{9, 1, 2, 3, 4, 5, 6, 8, 1,
                                     2, 3, 7, 8, 9, 9, 1, 2};
  SuffixDrafter drafter;
  Q4T_CHECK(drafter.Init(history, classes.size(), 64, classes).ok());
  const auto expected = ScanReference(history, 3, 4);
  const auto actual = drafter.ProposeAfter(3, 4);
  Q4T_CHECK(actual.reason == SuffixReason::kShortRejected);
  Q4T_CHECK(actual.match_length == 4);
  Q4T_CHECK(actual.tokens == expected.tokens);
  Q4T_CHECK(actual.match_end == expected.match_end);
  Q4T_CHECK(drafter.Counters().classified_tokens == 4);
  Q4T_CHECK(drafter.Counters().numeric_rejected == 0);
  return true;
}

Q4T_TEST(suffix_numeric_existing_gates_and_newest_tie_are_preserved) {
  std::array<uint8_t, 100> classes;
  classes.fill(ClassifySuffixToken("1"));
  SuffixDrafter drafter;
  const std::vector<int32_t> longest{9, 1, 2, 3, 4, 5, 6, 8, 1,
                                     2, 3, 7, 8, 9, 9, 1, 2};
  Q4T_CHECK(drafter.Init(longest, classes.size(), 64, classes).ok());
  Q4T_CHECK(drafter.ProposeAfter(3, 7).reason ==
            SuffixReason::kFirstTokenMismatch);
  Q4T_CHECK(drafter.Counters().numeric_checks == 0);
  Q4T_CHECK(drafter.ProposeAfter(3, 4).reason ==
            SuffixReason::kNumericRejected);
  Q4T_CHECK(drafter.Counters().numeric_checks == 1);
  Q4T_CHECK(drafter.Counters().classified_tokens == 4);

  const std::vector<int32_t> tied{9, 1, 2, 3, 4, 5, 6, 8, 1,
                                  2, 3, 4, 8, 9, 7, 1, 2};
  Q4T_CHECK(drafter.Init(tied, classes.size(), 64, classes).ok());
  const auto expected = ScanReference(tied, 3, 4);
  const auto actual = drafter.ProposeAfter(3, 4);
  Q4T_CHECK(expected.tokens[0] == 4 && expected.match_end == 10);
  Q4T_CHECK(actual.reason == SuffixReason::kNumericRejected);
  Q4T_CHECK(actual.match_end == expected.match_end);
  Q4T_CHECK(actual.tokens == expected.tokens);
  Q4T_CHECK(drafter.Counters().numeric_checks == 1);

  const std::vector<int32_t> incomplete{1, 1, 1, 1, 1, 1};
  Q4T_CHECK(drafter.Init(incomplete, classes.size(), 16, classes).ok());
  Q4T_CHECK(drafter.ProposeAfter(1, 1).reason == SuffixReason::kIncomplete);
  Q4T_CHECK(drafter.ProposeAfter(-1, 1).reason == SuffixReason::kInvalidToken);
  Q4T_CHECK(drafter.Counters().numeric_checks == 0);
  Q4T_CHECK(drafter.Counters().classified_tokens == 0);
  Q4T_CHECK(drafter.Counters().numeric_rejected == 0);
  return true;
}

Q4T_TEST(suffix_short_gate_applies_after_numeric_and_without_table) {
  const std::vector<int32_t> history{1, 2, 3, 4, 5, 6, 7, 1, 2};
  // Respectively: no digit; unknown token; mixed letter/digit token.
  const std::array<std::array<uint8_t, 3>, 3> cases{
      {{1, 1, 1}, {3, 0, 1}, {3, 2, 1}}};
  for (const auto& properties : cases) {
    std::array<uint8_t, 100> classes{};
    for (size_t i = 0; i < properties.size(); ++i) {
      classes[i + 1] = properties[i];
    }
    SuffixDrafter drafter;
    Q4T_CHECK(drafter.Init(history, classes.size(), 64, classes).ok());
    Q4T_CHECK(drafter.ProposeAfter(3, 4).reason ==
              SuffixReason::kShortRejected);
    Q4T_CHECK(drafter.Counters().numeric_checks == 1);
    Q4T_CHECK(drafter.Counters().numeric_rejected == 0);
  }
  SuffixDrafter legacy;
  Q4T_CHECK(legacy.Init(history, 100, 64).ok());
  Q4T_CHECK(legacy.ProposeAfter(3, 4).reason == SuffixReason::kShortRejected);
  Q4T_CHECK(legacy.Counters().short_rejected == 1);
  Q4T_CHECK(legacy.Counters().numeric_checks == 0);
  Q4T_CHECK(legacy.Counters().classified_tokens == 0);
  return true;
}

Q4T_TEST(suffix_numeric_table_size_and_request_reset_contract) {
  const std::vector<int32_t> history{1, 2, 3, 4, 5, 6, 7, 1, 2};
  std::vector<uint8_t> classes(100, 3);
  SuffixDrafter drafter;
  Q4T_CHECK(!drafter.Init(history, 99, 64, classes).ok());
  Q4T_CHECK(!drafter.Init(history, 101, 64, classes).ok());
  Q4T_CHECK(!drafter.Active() && drafter.MemoryBytes() == 0);
  Q4T_CHECK(drafter.Init(history, 100, 64, classes).ok());
  Q4T_CHECK(drafter.ProposeAfter(3, 4).reason ==
            SuffixReason::kNumericRejected);
  drafter.Reset();
  Q4T_CHECK(!drafter.Active() && drafter.MemoryBytes() == 0);
  Q4T_CHECK(drafter.Counters().numeric_rejected == 1);
  std::vector<uint8_t>().swap(classes);  // End the borrowed table's lifetime.
  Q4T_CHECK(drafter.ProposeAfter(3, 4).reason == SuffixReason::kInactive);
  Q4T_CHECK(drafter.Init(history, 100, 64).ok());
  Q4T_CHECK(drafter.ProposeAfter(3, 4).reason == SuffixReason::kShortRejected);
  Q4T_CHECK(drafter.Counters().numeric_checks == 0);
  Q4T_CHECK(drafter.Counters().numeric_rejected == 0);
  return true;
}

Q4T_TEST(suffix_numeric_filter_preserves_fixed_maximum_match_range) {
  std::array<uint8_t, 1000> classes;
  classes.fill(ClassifySuffixToken("1"));
  std::vector<int32_t> history;
  for (int32_t token = 1; token <= 70; ++token) history.push_back(token);
  history.insert(history.end(), {101, 102, 103, 900});
  for (int32_t token = 1; token < 70; ++token) history.push_back(token);
  SuffixDrafter drafter;
  Q4T_CHECK(drafter.Init(history, classes.size(), 256, classes).ok());
  const auto proposal = drafter.ProposeAfter(70, 101);
  Q4T_CHECK(proposal.reason == SuffixReason::kNumericRejected);
  Q4T_CHECK(proposal.match_length == 64);
  Q4T_CHECK(drafter.Counters().numeric_checks == 1);
  Q4T_CHECK(drafter.Counters().classified_tokens == 64);
  Q4T_CHECK(drafter.Counters().numeric_rejected == 1);
  return true;
}

Q4T_TEST(suffix_long_copy_admission_boundary_and_global_continuation) {
  // Include the observed quoted-sentence length, both sides of the new
  // boundary, and the lookup cap. No subset vocabulary is consulted.
  for (int length : {3, 19, 31, 32, 33, 64}) {
    const auto history = LongCopy(length);
    SuffixDrafter drafter;
    Q4T_CHECK(drafter.Init(history, 200000, 256).ok());
    const auto proposal = drafter.ProposeAfter(length, 100001);
    const auto expected = ScanReference(history, length, 100001);
    Q4T_CHECK(proposal.match_length == length);
    Q4T_CHECK(proposal.match_end == static_cast<uint32_t>(length - 1));
    Q4T_CHECK(proposal.tokens == expected.tokens);
    Q4T_CHECK(proposal.ready == (length >= 32));
    Q4T_CHECK(proposal.reason == expected.reason);
    Q4T_CHECK(drafter.Counters().short_rejected == (length < 32 ? 1 : 0));
    Q4T_CHECK(drafter.Counters().hits == (length >= 32 ? 1 : 0));
    Q4T_CHECK(std::vector<int32_t>(drafter.History().begin(),
                                   drafter.History().end()) == history);
  }
  Q4T_CHECK(std::string_view(q4t::mtp::SuffixReasonName(
                SuffixReason::kShortRejected)) == "short_rejected");
  return true;
}

Q4T_TEST(suffix_short_fallback_commits_can_become_a_long_copy) {
  auto history = LongCopy(32);
  // The current query prefix ends at token 30; the committed neural prefix
  // then makes a 32-token copy eligible without resetting the index.
  history.pop_back();
  SuffixDrafter drafter;
  Q4T_CHECK(drafter.Init(history, 200000, 96).ok());
  const auto short_copy = drafter.ProposeAfter(31, 32);
  Q4T_CHECK(short_copy.reason == SuffixReason::kShortRejected);
  const std::array<int32_t, 1> committed{31};
  Q4T_CHECK(drafter.Append(committed).ok());
  const auto long_copy = drafter.ProposeAfter(32, 100001);
  Q4T_CHECK(long_copy.ready && long_copy.match_length == 32);
  Q4T_CHECK(drafter.Counters().short_rejected == 1);
  Q4T_CHECK(drafter.Counters().hits == 1);
  drafter.Reset();
  Q4T_CHECK(drafter.Counters().short_rejected == 1);
  Q4T_CHECK(drafter.Init(history, 200000, 96).ok());
  Q4T_CHECK(drafter.Counters().short_rejected == 0);
  return true;
}

Q4T_TEST(suffix_numeric_and_d0_gates_precede_short_copy_gate) {
  const auto history = LongCopy(19);
  std::vector<uint8_t> classes(200000, ClassifySuffixToken("1"));
  SuffixDrafter drafter;
  Q4T_CHECK(drafter.Init(history, 200000, 96, classes).ok());
  Q4T_CHECK(drafter.ProposeAfter(19, 77).reason ==
            SuffixReason::kFirstTokenMismatch);
  Q4T_CHECK(drafter.Counters().numeric_checks == 0);
  Q4T_CHECK(drafter.ProposeAfter(19, 100001).reason ==
            SuffixReason::kNumericRejected);
  Q4T_CHECK(drafter.Counters().numeric_checks == 1);
  Q4T_CHECK(drafter.Counters().numeric_rejected == 1);
  Q4T_CHECK(drafter.Counters().short_rejected == 0);
  return true;
}
