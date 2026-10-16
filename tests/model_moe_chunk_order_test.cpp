// Pure host contracts. Execute only after the optimization's first HTTP gate.
#include "q4t/model/moe_chunk_order.h"
#include "q4t/test.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <random>
#include <vector>

namespace {

using q4t::model::MoEChunkOrderSelector;
using q4t::model::ParseMoEChunkOrder;

void AddChunks(MoEChunkOrderSelector* selector,
               const std::vector<std::vector<int>>& chunks, int experts) {
  selector->Init(chunks.size(), experts);
  for (size_t j = 0; j < chunks.size(); ++j) {
    for (int expert : chunks[j]) selector->AddExpert(j, expert);
  }
}

size_t Select(MoEChunkOrderSelector* selector,
              const std::vector<int>& resident) {
  return selector->SelectNext(static_cast<int>(resident.size()),
                              [&](int slot) { return resident[slot]; });
}

}  // namespace

Q4T_TEST(moe_chunk_order_strict_opt_in) {
  Q4T_CHECK(ParseMoEChunkOrder(nullptr) == 0);
  Q4T_CHECK(ParseMoEChunkOrder("0") == 0);
  Q4T_CHECK(ParseMoEChunkOrder("1") == 1);
  for (const char* invalid : {"", "01", "2", "-1", "true", " 1", "1x"}) {
    Q4T_CHECK(ParseMoEChunkOrder(invalid) == -1);
  }
  return true;
}

Q4T_TEST(moe_chunk_order_tie_and_exhaustion) {
  MoEChunkOrderSelector selector;
  AddChunks(&selector, {{0, 1}, {2, 3}, {4, 5}}, 8);
  for (size_t expected = 0; expected < 3; ++expected) {
    Q4T_CHECK(Select(&selector, {-1, -1}) == expected);
  }
  Q4T_CHECK(Select(&selector, {0, 1}) == 3);
  selector.Init(0, 8);
  Q4T_CHECK(Select(&selector, {-1}) == 0);
  return true;
}

Q4T_TEST(moe_chunk_order_reads_live_slots) {
  MoEChunkOrderSelector selector;
  AddChunks(&selector, {{0, 1}, {2, 3}, {4, 5}}, 8);
  Q4T_CHECK(Select(&selector, {4, 5}) == 2);
  // A retained initial snapshot would choose chunk 0 on a zero-overlap tie.
  Q4T_CHECK(Select(&selector, {2, 3}) == 1);
  Q4T_CHECK(Select(&selector, {0, 1}) == 0);
  return true;
}

Q4T_TEST(moe_chunk_order_distinct_membership_and_word_boundaries) {
  MoEChunkOrderSelector selector;
  AddChunks(&selector, {{63, 63, 63}, {64, 127}, {0, 129}}, 130);
  // Repeated token references to 63 count once, not as three cache hits.
  Q4T_CHECK(Select(&selector, {63, 64, 127, -1}) == 1);
  Q4T_CHECK(Select(&selector, {0, 129, -1}) == 2);
  Q4T_CHECK(Select(&selector, {63}) == 0);
  return true;
}

Q4T_TEST(moe_chunk_order_matches_frozen_offline_example) {
  MoEChunkOrderSelector selector;
  AddChunks(&selector, {{0, 1}, {2, 3}, {4, 5}}, 8);
  // Same fixed-partition example as test_offload_replay.py. A C=2 layer
  // starts with experts 4/5, then each executed chunk becomes resident.
  std::vector<int> resident{4, 5};
  const std::vector<std::vector<int>> needed{{0, 1}, {2, 3}, {4, 5}};
  for (size_t expected : {size_t{2}, size_t{0}, size_t{1}}) {
    const size_t selected = Select(&selector, resident);
    Q4T_CHECK(selected == expected);
    resident = needed[selected];
  }
  return true;
}

Q4T_TEST(moe_chunk_order_keeps_unequal_chunk_row_mapping) {
  struct Sub {
    std::vector<int> tokens;
    std::vector<int> slot_ids;
  };
  const std::vector<std::vector<int>> needed{{0, 1}, {2, 3}, {4, 5}};
  std::vector<Sub> subs{{{3, 0, 2}, {0, 1, 1, 0, 0, 1}},
                        {{4}, {0, 1}}, {{1, 5}, {1, 0, 0, 1}}};
  MoEChunkOrderSelector selector;
  AddChunks(&selector, needed, 8);
  std::vector<int> resident{4, 5};
  std::array<int, 6> visits{};
  const std::array<size_t, 3> expected_rows{2, 3, 1};
  for (size_t step = 0; step < subs.size(); ++step) {
    const size_t selected = Select(&selector, resident);
    const Sub& sub = subs[selected];
    Q4T_CHECK(sub.tokens.size() == expected_rows[step]);
    Q4T_CHECK(sub.slot_ids.size() == sub.tokens.size() * 2);
    for (int token : sub.tokens) ++visits[token];
    resident = needed[selected];
  }
  Q4T_CHECK(std::all_of(visits.begin(), visits.end(),
                        [](int count) { return count == 1; }));
  return true;
}

Q4T_TEST(moe_chunk_order_reset_does_not_keep_old_selection) {
  MoEChunkOrderSelector selector;
  AddChunks(&selector, {{1}, {2}}, 8);
  Q4T_CHECK(Select(&selector, {2}) == 1);
  AddChunks(&selector, {{7}, {0}}, 8);
  Q4T_CHECK(Select(&selector, {0}) == 1);
  Q4T_CHECK(Select(&selector, {7}) == 0);
  return true;
}

Q4T_TEST(moe_chunk_order_matches_independent_set_reference) {
  std::mt19937 random(431);
  constexpr int kExperts = 131;
  for (int trial = 0; trial < 64; ++trial) {
    const size_t count = 1 + random() % 19;
    std::vector<std::vector<int>> chunks(count);
    for (auto& experts : chunks) {
      for (int j = 0; j < 17; ++j) {
        experts.push_back(static_cast<int>(random() % kExperts));
      }
      std::sort(experts.begin(), experts.end());
      experts.erase(std::unique(experts.begin(), experts.end()), experts.end());
    }
    MoEChunkOrderSelector selector;
    AddChunks(&selector, chunks, kExperts);
    std::vector<bool> selected(count, false);
    for (size_t step = 0; step < count; ++step) {
      std::vector<int> resident(8);
      for (int& expert : resident) {
        expert = static_cast<int>(random() % (kExperts + 1)) - 1;
      }
      size_t expected = count;
      int best = -1;
      for (size_t chunk = 0; chunk < count; ++chunk) {
        if (selected[chunk]) continue;
        int overlap = 0;
        for (int expert : chunks[chunk]) {
          if (std::find(resident.begin(), resident.end(), expert) !=
              resident.end()) ++overlap;
        }
        if (overlap > best) {
          best = overlap;
          expected = chunk;
        }
      }
      Q4T_CHECK(Select(&selector, resident) == expected);
      selected[expected] = true;
    }
  }
  return true;
}
