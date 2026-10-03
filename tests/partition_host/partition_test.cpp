// Execute only after the runtime optimization's first HTTP E2E gate.
#include "q4t/model/moe_partition.h"
#include "q4t/test.h"

#include <limits>
#include <numeric>
#include <type_traits>

#include "../../tools/trace/offload_partition.h"

namespace {

using q4t::model::MoEPartitionSupported;
using q4t::model::ParseMoEPartitionMode;
using q4t::model::PlanMoEPartition;
using Chunks = std::vector<std::vector<int32_t>>;

const std::vector<int32_t> kIds{0, 1, 0, 2, 0, 3, 1, 2};
const std::vector<int32_t> kOrder{0, 1, 2, 3};

template <typename Function>
bool RejectsInvalid(Function function) {
  try {
    function();
  } catch (const std::invalid_argument&) {
    return true;
  }
  return false;
}

bool HasCompleteRows(const Chunks& chunks, size_t count) {
  std::vector<int> seen(count, 0);
  for (const auto& chunk : chunks) {
    if (chunk.empty()) return false;
    for (int32_t row : chunk) {
      if (row < 0 || static_cast<size_t>(row) >= count || ++seen[row] != 1) {
        return false;
      }
    }
  }
  return std::all_of(seen.begin(), seen.end(), [](int n) { return n == 1; });
}

}  // namespace

Q4T_TEST(moe_partition_strict_opt_in) {
  Q4T_CHECK(ParseMoEPartitionMode(nullptr) == 0);
  Q4T_CHECK(ParseMoEPartitionMode("0") == 0);
  Q4T_CHECK(ParseMoEPartitionMode("1") == 1);
  for (const char* value : {"", "01", "2", "-1", "true", " 1", "1 "}) {
    Q4T_CHECK(ParseMoEPartitionMode(value) == -1);
  }
  return true;
}

Q4T_TEST(moe_partition_supported_domain) {
  Q4T_CHECK(MoEPartitionSupported(2, 512, 256, 10, false));
  Q4T_CHECK(MoEPartitionSupported(8192, 65536, 63, 63, false));
  for (int rows : {-1, 0, 1, 8193, std::numeric_limits<int>::max()}) {
    Q4T_CHECK(!MoEPartitionSupported(rows, 512, 256, 10, false));
  }
  Q4T_CHECK(!MoEPartitionSupported(2, 0, 1, 1, false));
  Q4T_CHECK(!MoEPartitionSupported(2, 65537, 256, 10, false));
  Q4T_CHECK(!MoEPartitionSupported(2, 512, 9, 10, false));
  Q4T_CHECK(!MoEPartitionSupported(2, 512, 513, 10, false));
  Q4T_CHECK(!MoEPartitionSupported(2, 512, 256, 0, false));
  Q4T_CHECK(!MoEPartitionSupported(2, 512, 256, 64, false));
  Q4T_CHECK(!MoEPartitionSupported(2, 512, 256, 10, true));
  return true;
}

Q4T_TEST(moe_partition_runtime_golden_and_shared_alias) {
  static_assert(std::is_same_v<q4t::trace::PartitionInput,
                              q4t::model::PartitionInput>);
  static_assert(std::is_same_v<q4t::trace::PartitionResult,
                              q4t::model::PartitionResult>);
  const auto planned = PlanMoEPartition(4, 3, 2, false, kIds, kOrder);
  const Chunks expected{{0, 1, 3}, {2}};
  Q4T_CHECK(planned.chunks == expected);
  Q4T_CHECK(!planned.fallback);
  Q4T_CHECK(planned.counters.work_budget == 32 * kIds.size());
  Q4T_CHECK(planned.counters.work_used == planned.counters.csr_visits +
                                           planned.counters.reset_rows);
  return true;
}

Q4T_TEST(moe_partition_complete_router_rows_and_scatter) {
  const std::vector<int32_t> ids{0, 3, 2, 0, 1, 0, 2, 1};
  const std::vector<int32_t> order{2, 1, 0, 3};
  const std::vector<int32_t> weights{101, 102, 201, 202, 301, 302, 401, 402};
  const auto saved_ids = ids;
  const auto saved_order = order;
  const auto planned = PlanMoEPartition(4, 3, 2, false, ids, order);
  const Chunks expected{{2, 1, 3}, {0}};
  Q4T_CHECK(planned.chunks == expected);
  Q4T_CHECK(HasCompleteRows(planned.chunks, order.size()));
  std::vector<int32_t> scattered(ids.size(), -1);
  for (const auto& chunk : planned.chunks) {
    for (int32_t row : chunk) {
      // The runtime uses the same row index for expert IDs and weights.
      // Distinct sentinels detect top-k order swaps and duplicate scattering.
      for (int j = 0; j < 2; ++j) {
        const size_t index = static_cast<size_t>(row) * 2 + j;
        scattered[index] = ids[index] * 1000 + weights[index];
      }
    }
  }
  for (size_t i = 0; i < ids.size(); ++i) {
    Q4T_CHECK(scattered[i] == ids[i] * 1000 + weights[i]);
  }
  Q4T_CHECK(ids == saved_ids && order == saved_order);
  return true;
}

Q4T_TEST(moe_partition_equal_keys_keep_supplied_lex_rank) {
  const std::vector<int32_t> ids{1, 0, 0, 1, 1, 0};
  const std::vector<int32_t> order{2, 0, 1};
  const auto planned = PlanMoEPartition(2, 2, 2, false, ids, order);
  Q4T_CHECK(planned.chunks == Chunks{order});
  return true;
}

Q4T_TEST(moe_partition_rejects_expert_narrowing) {
  for (int32_t invalid : {-1, 4, 65536, std::numeric_limits<int32_t>::max()}) {
    auto ids = kIds;
    ids[0] = invalid;
    Q4T_CHECK(RejectsInvalid([&] {
      PlanMoEPartition(4, 3, 2, false, ids, kOrder);
    }));
  }
  return true;
}

Q4T_TEST(moe_partition_rejects_token_narrowing) {
  for (int32_t invalid : {-1, 4, std::numeric_limits<int32_t>::max()}) {
    auto order = kOrder;
    order[0] = invalid;
    Q4T_CHECK(RejectsInvalid([&] {
      PlanMoEPartition(4, 3, 2, false, kIds, order);
    }));
  }
  return true;
}

Q4T_TEST(moe_partition_rejects_duplicate_and_unsorted_order) {
  for (const auto& order : {std::vector<int32_t>{0, 0, 2, 3},
                            std::vector<int32_t>{1, 0, 2, 3}}) {
    Q4T_CHECK(RejectsInvalid([&] {
      PlanMoEPartition(4, 3, 2, false, kIds, order);
    }));
  }
  return true;
}

Q4T_TEST(moe_partition_rejects_duplicate_expert_per_row) {
  auto ids = kIds;
  ids[1] = ids[0];
  Q4T_CHECK(RejectsInvalid([&] {
    PlanMoEPartition(4, 3, 2, false, ids, kOrder);
  }));
  return true;
}

Q4T_TEST(moe_partition_rejects_partial_or_missing_rows) {
  auto ids = kIds;
  ids.pop_back();
  Q4T_CHECK(RejectsInvalid([&] {
    PlanMoEPartition(4, 3, 2, false, ids, kOrder);
  }));
  auto order = kOrder;
  order.pop_back();
  Q4T_CHECK(RejectsInvalid([&] {
    PlanMoEPartition(4, 3, 2, false, kIds, order);
  }));
  Q4T_CHECK(RejectsInvalid([&] {
    PlanMoEPartition(4, 3, 2, false, {}, {});
  }));
  return true;
}

Q4T_TEST(moe_partition_rejects_unsupported_direct_calls) {
  Q4T_CHECK(RejectsInvalid([&] {
    PlanMoEPartition(4, 3, 2, true, kIds, kOrder);
  }));
  Q4T_CHECK(RejectsInvalid([&] {
    PlanMoEPartition(4, 3, 0, false, kIds, kOrder);
  }));
  Q4T_CHECK(RejectsInvalid([&] {
    PlanMoEPartition(4, 1, 2, false, kIds, kOrder);
  }));
  const std::vector<int32_t> one_row{0}, one_id{0};
  Q4T_CHECK(RejectsInvalid([&] {
    PlanMoEPartition(1, 1, 1, false, one_id, one_row);
  }));
  const std::vector<int32_t> too_many(8193, 0);
  Q4T_CHECK(RejectsInvalid([&] {
    PlanMoEPartition(1, 1, 1, false, too_many, too_many);
  }));
  return true;
}

Q4T_TEST(moe_partition_max_expert_and_topk_no_truncation) {
  std::vector<int32_t> ids;
  for (int row = 0; row < 2; ++row) {
    for (int j = 0; j < 63; ++j) ids.push_back(65535 - j);
  }
  const std::vector<int32_t> order{1, 0};
  const auto planned = PlanMoEPartition(65536, 63, 63, false, ids, order);
  Q4T_CHECK(!planned.fallback);
  Q4T_CHECK(planned.chunks == Chunks{order});
  Q4T_CHECK(planned.counters.work_budget == 32 * 2 * 63);
  return true;
}

Q4T_TEST(moe_partition_max_rows_preserved) {
  std::vector<int32_t> ids(8192, 0), order(8192);
  std::iota(order.rbegin(), order.rend(), 0);
  const auto planned = PlanMoEPartition(1, 1, 1, false, ids, order);
  Q4T_CHECK(!planned.fallback);
  Q4T_CHECK(planned.chunks == Chunks{order});
  Q4T_CHECK(HasCompleteRows(planned.chunks, order.size()));
  return true;
}

Q4T_TEST(moe_partition_fixed_budget_whole_forward_fallback) {
  // Disjoint singleton expert sets force enough reset scans to exhaust the
  // fixed production budget. No test-only budget parameter is exposed.
  std::vector<int32_t> ids(129), order(129);
  std::iota(ids.begin(), ids.end(), 0);
  std::iota(order.begin(), order.end(), 0);
  const auto planned = PlanMoEPartition(129, 1, 1, false, ids, order);
  Q4T_CHECK(planned.fallback);
  Q4T_CHECK(planned.counters.work_budget == 32 * ids.size());
  Q4T_CHECK(planned.counters.work_used == planned.counters.work_budget);
  Q4T_CHECK(planned.chunks.size() == order.size());
  for (size_t row = 0; row < order.size(); ++row) {
    Q4T_CHECK(planned.chunks[row] == std::vector<int32_t>{order[row]});
  }
  Q4T_CHECK(HasCompleteRows(planned.chunks, order.size()));
  return true;
}
