// Run only after this optimization's first actual HTTP E2E test.
#include "q4t/quant/moe_mirror_recycle.h"
#include "q4t/test.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <numeric>
#include <tuple>
#include <vector>

namespace {
using q4t::quant::BuildMoEMirrorGpuCoverage;
using q4t::quant::MoEMirrorGpuRecycleScope;
using q4t::quant::ParseMoEMirrorGpuRecycle;
using q4t::quant::PickMoEMirrorGpuRecycle;
}  // namespace

Q4T_TEST(mirror_recycle_strict_opt_in) {
  Q4T_CHECK(ParseMoEMirrorGpuRecycle(nullptr) == 0);
  Q4T_CHECK(ParseMoEMirrorGpuRecycle("0") == 0);
  Q4T_CHECK(ParseMoEMirrorGpuRecycle("1") == 1);
  for (const char* value : {"", "01", "2", "-1", "true", " 1", "1 ", "1\n"}) {
    Q4T_CHECK(ParseMoEMirrorGpuRecycle(value) == -1);
  }
  return true;
}

Q4T_TEST(mirror_recycle_requires_explicit_single_decode) {
  Q4T_CHECK(MoEMirrorGpuRecycleScope(true, 1, false));
  for (int tokens : {0, 1, 2, 8, 8192}) {
    for (bool request : {false, true}) {
      Q4T_CHECK(!MoEMirrorGpuRecycleScope(false, tokens, request));
      if (tokens != 1 || request)
        Q4T_CHECK(!MoEMirrorGpuRecycleScope(true, tokens, request));
    }
  }
  return true;
}

Q4T_TEST(mirror_recycle_snapshot_excludes_full_plan_victims) {
  const std::vector<int> slots{7, 2, -1, 4, 0};
  const std::vector<uint8_t> reserved{1, 0, 1, 1, 0};
  const auto covered = BuildMoEMirrorGpuCoverage(8, slots, reserved);
  Q4T_CHECK(covered == std::vector<uint8_t>({1, 0, 1, 0, 0, 0, 0, 0}));
  // Missing expert 6 is not considered resident just because it will load.
  Q4T_CHECK(!covered[6]);
  return true;
}

Q4T_TEST(mirror_recycle_snapshot_ignores_invalid_empty_members) {
  const std::vector<int> slots{-1, -7, 5, 1};
  const std::vector<uint8_t> reserved(4, 0);
  Q4T_CHECK(BuildMoEMirrorGpuCoverage(3, slots, reserved) ==
            std::vector<uint8_t>({0, 1, 0}));
  return true;
}

Q4T_TEST(mirror_recycle_snapshot_remains_immutable_during_commits) {
  std::vector<int> slots{0, 1, 2, 3};
  const std::vector<uint8_t> reserved{1, 0, 1, 0};
  const auto covered = BuildMoEMirrorGpuCoverage(6, slots, reserved);
  for (const auto& order : {std::array<int, 2>{0, 2}, {2, 0}}) {
    for (int slot : order) slots[slot] = slot == 0 ? 4 : 5;
    Q4T_CHECK(covered == std::vector<uint8_t>({0, 1, 0, 1, 0, 0}));
  }
  return true;
}

Q4T_TEST(mirror_recycle_prefers_covered_and_records_changed_target) {
  const std::vector<int> ring{0, 1, 2, 3};
  const std::vector<uint8_t> covered{0, 0, 1, 0};
  const auto choice = PickMoEMirrorGpuRecycle(
      0, ring, covered, [](int) { return true; });
  Q4T_CHECK(choice.slot == 2 && choice.fallback == 0);
  Q4T_CHECK(choice.preferred && choice.Changed());
  const auto same = PickMoEMirrorGpuRecycle(
      2, ring, covered, [](int) { return true; });
  Q4T_CHECK(same.slot == 2 && same.fallback == 2);
  Q4T_CHECK(same.preferred && !same.Changed());
  return true;
}

Q4T_TEST(mirror_recycle_wraparound_and_covered_tie_order) {
  const std::vector<int> ring{0, 1, 2, 3};
  const std::vector<uint8_t> covered{1, 1, 0, 0};
  const auto choice = PickMoEMirrorGpuRecycle(
      3, ring, covered, [](int) { return true; });
  Q4T_CHECK(choice.slot == 0 && choice.fallback == 3);
  Q4T_CHECK(choice.preferred && choice.Changed());
  return true;
}

Q4T_TEST(mirror_recycle_claimed_or_pending_cannot_be_selected) {
  const std::vector<int> ring{0, 1, 2, 3};
  const std::vector<uint8_t> covered{1, 1, 1, 1};
  const std::array<bool, 4> claimed{true, false, false, false};
  const std::array<bool, 4> ready{true, false, true, true};
  const auto choice = PickMoEMirrorGpuRecycle(
      0, ring, covered,
      [&](int slot) { return !claimed[slot] && ready[slot]; });
  Q4T_CHECK(choice.slot == 2 && choice.fallback == 2);
  Q4T_CHECK(choice.preferred && !choice.Changed());
  return true;
}

Q4T_TEST(mirror_recycle_no_covered_keeps_first_eligible_fallback) {
  const std::vector<int> ring{0, 1, 2, 3};
  const std::vector<uint8_t> covered(4, 0);
  const auto choice = PickMoEMirrorGpuRecycle(
      3, ring, covered, [](int slot) { return slot != 3 && slot != 0; });
  Q4T_CHECK(choice.slot == 1 && choice.fallback == 1);
  Q4T_CHECK(!choice.preferred && !choice.Changed());
  return true;
}

Q4T_TEST(mirror_recycle_empty_and_out_of_range_are_not_covered) {
  const std::vector<int> ring{-1, 8, -3, 1};
  const std::vector<uint8_t> covered{0, 1};
  const auto choice = PickMoEMirrorGpuRecycle(
      0, ring, covered, [](int) { return true; });
  Q4T_CHECK(choice.slot == 3 && choice.fallback == 0);
  Q4T_CHECK(choice.preferred && choice.Changed());
  return true;
}

Q4T_TEST(mirror_recycle_no_slot_preserves_skip) {
  const std::vector<int> ring{0, 1};
  const std::vector<uint8_t> covered{1, 1};
  const auto choice = PickMoEMirrorGpuRecycle(
      0, ring, covered, [](int) { return false; });
  Q4T_CHECK(choice.slot == -1 && choice.fallback == -1);
  Q4T_CHECK(!choice.preferred && !choice.Changed());
  int calls = 0;
  const auto empty = PickMoEMirrorGpuRecycle(
      0, std::span<const int>{}, covered, [&](int) { ++calls; return true; });
  Q4T_CHECK(empty.slot == -1 && calls == 0);
  return true;
}

Q4T_TEST(mirror_recycle_each_eligibility_query_at_most_once) {
  const std::vector<int> ring{0, 1, 2, 3};
  const std::vector<uint8_t> covered{0, 0, 1, 0};
  std::array<int, 4> calls{};
  const auto choice = PickMoEMirrorGpuRecycle(
      0, ring, covered, [&](int slot) { ++calls[slot]; return true; });
  Q4T_CHECK(choice.slot == 2);
  Q4T_CHECK((calls == std::array<int, 4>{1, 1, 1, 0}));
  return true;
}

Q4T_TEST(mirror_recycle_reservations_exclude_other_workers) {
  const std::vector<int> ring{0, 1, 2};
  const std::vector<uint8_t> covered{0, 1, 1};
  std::array<bool, 3> claimed{};
  const auto eligible = [&](int slot) { return !claimed[slot]; };
  const auto first = PickMoEMirrorGpuRecycle(0, ring, covered, eligible);
  claimed[first.slot] = true;  // existing mutex protects reserve and choice
  const auto second = PickMoEMirrorGpuRecycle(0, ring, covered, eligible);
  Q4T_CHECK(first.slot == 1 && second.slot == 2);
  Q4T_CHECK(second.preferred && second.Changed());
  return true;
}

Q4T_TEST(mirror_recycle_exhaustive_small_ring_rank_oracle) {
  // Independent ordering oracle: eligible members sort by uncovered first
  // component, then circular distance. Test every coverage/eligibility subset.
  for (int count = 1; count <= 4; ++count) {
    std::vector<int> ring(count);
    std::iota(ring.begin(), ring.end(), 0);
    for (int coverage = 0; coverage < (1 << count); ++coverage) {
      std::vector<uint8_t> covered(count);
      for (int i = 0; i < count; ++i) covered[i] = (coverage >> i) & 1;
      for (int available = 0; available < (1 << count); ++available) {
        for (int cursor = 0; cursor < count; ++cursor) {
          std::vector<int> ranked;
          for (int slot = 0; slot < count; ++slot)
            if ((available >> slot) & 1) ranked.push_back(slot);
          const auto distance = [&](int slot) {
            return (slot + count - cursor) % count;
          };
          const auto legacy = ranked.empty() ? -1 : *std::min_element(
              ranked.begin(), ranked.end(), [&](int a, int b) {
                return distance(a) < distance(b);
              });
          std::sort(ranked.begin(), ranked.end(), [&](int a, int b) {
            return std::tuple(!covered[a], distance(a)) <
                   std::tuple(!covered[b], distance(b));
          });
          const auto choice = PickMoEMirrorGpuRecycle(
              cursor, ring, covered,
              [&](int slot) { return ((available >> slot) & 1) != 0; });
          const int expected = ranked.empty() ? -1 : ranked.front();
          Q4T_CHECK(choice.slot == expected && choice.fallback == legacy);
          Q4T_CHECK(choice.preferred == (expected >= 0 && covered[expected]));
          Q4T_CHECK(choice.Changed() ==
                    (expected >= 0 && covered[expected] && expected != legacy));
        }
      }
    }
  }
  return true;
}
