// Offline partition contracts; prepared only, execute under the root gate.
#include "offload_partition.h"

#include <functional>
#include <iostream>
#include <numeric>
#include <random>
#include <string>

namespace {

using q4t::trace::FrozenWorkBudget;
using q4t::trace::MinNewPartition;
using q4t::trace::OriginalPartition;
using q4t::trace::PartitionInput;
using q4t::trace::ValidatePartitionInput;
using Chunks = std::vector<std::vector<uint32_t>>;

void Check(bool value, const char* message) {
  if (!value) throw std::runtime_error(message);
}

PartitionInput Example() {
  return {4, 3, 2, {0, 1, 0, 2, 0, 3, 1, 2}, {0, 1, 2, 3}};
}

void PartitionContract(const PartitionInput& input, const Chunks& chunks) {
  std::vector<int> seen(input.order.size(), 0);
  for (const auto& chunk : chunks) {
    Check(!chunk.empty(), "empty output chunk");
    std::vector<bool> needed(input.experts, false);
    for (uint32_t row : chunk) {
      Check(row < seen.size() && ++seen[row] == 1, "invalid/duplicate row");
      for (int j = 0; j < input.topk; ++j) {
        needed[input.ids[static_cast<size_t>(row) * input.topk + j]] = true;
      }
    }
    Check(std::count(needed.begin(), needed.end(), true) <= input.capacity,
          "capacity exceeded");
  }
  Check(std::all_of(seen.begin(), seen.end(), [](int n) { return n == 1; }),
        "a token row was omitted");
}

// Independent slow reference: recompute each remaining row's new-expert set
// directly on every selection. It has no CSR, score buckets or incremental
// updates, and is restricted to small synthetic inputs.
Chunks Reference(const PartitionInput& input) {
  std::vector<bool> assigned(input.order.size(), false);
  size_t remaining = input.order.size();
  Chunks result;
  while (remaining) {
    std::vector<bool> needed(input.experts, false);
    int distinct = 0;
    std::vector<uint32_t> chunk;
    while (remaining) {
      size_t best = input.order.size();
      int score = input.topk + 1;
      for (size_t rank = 0; rank < input.order.size(); ++rank) {
        if (assigned[rank]) continue;
        int fresh = 0;
        const size_t base = static_cast<size_t>(input.order[rank]) * input.topk;
        for (int j = 0; j < input.topk; ++j) {
          fresh += !needed[input.ids[base + j]];
        }
        if (fresh < score) {
          best = rank;
          score = fresh;
        }
      }
      if (distinct + score > input.capacity) break;
      assigned[best] = true;
      --remaining;
      const uint32_t row = input.order[best];
      chunk.push_back(row);
      const size_t base = static_cast<size_t>(row) * input.topk;
      for (int j = 0; j < input.topk; ++j) {
        const int expert = input.ids[base + j];
        if (!needed[expert]) {
          needed[expert] = true;
          ++distinct;
        }
      }
    }
    Check(!chunk.empty(), "reference made no progress");
    result.push_back(std::move(chunk));
  }
  return result;
}

void GoldenPartition() {
  const PartitionInput input = Example();
  const auto result = MinNewPartition(input, FrozenWorkBudget(input));
  Check(!result.fallback, "small golden unexpectedly fell back");
  Check(OriginalPartition(input) == Chunks{{0, 1}, {2}, {3}},
        "original changed");
  Check(result.chunks == Chunks{{0, 1, 3}, {2}}, "min-new golden mismatch");
  PartitionContract(input, result.chunks);
}

void BudgetWholeForwardFallback() {
  const auto input = Example();
  const auto complete =
      MinNewPartition(input, std::numeric_limits<uint64_t>::max());
  const uint64_t exact = complete.counters.work_used;
  Check(exact > 1, "invalid work count");
  for (uint64_t budget : {uint64_t{0}, uint64_t{1}, exact - 1}) {
    const auto result = MinNewPartition(input, budget);
    Check(result.fallback, "budget exhaustion was hidden");
    Check(result.chunks == OriginalPartition(input),
          "partial candidate retained");
    Check(result.counters.work_used == budget,
          "work exceeded/stopped before cap");
    Check(result.counters.work_used == result.counters.csr_visits +
              result.counters.reset_rows, "work accounting mismatch");
  }
  const auto exact_result = MinNewPartition(input, exact);
  Check(!exact_result.fallback && exact_result.chunks == complete.chunks,
        "exact budget rejected a finished partition");
}

void LexRankTie() {
  PartitionInput input{4, 3, 2, {0, 3, 0, 2, 1, 0, 1, 2}, {2, 1, 0, 3}};
  const auto result = MinNewPartition(input, FrozenWorkBudget(input));
  Check(result.chunks == Chunks{{2, 1, 3}, {0}}, "tie used original row ID");
  input = {2, 2, 2, {1, 0, 0, 1}, {1, 0}};
  Check(MinNewPartition(input, FrozenWorkBudget(input)).chunks ==
            Chunks{{1, 0}},
        "equal-key C++ order was replaced with a stable row-ID sort");
}

void CountWorkByHand() {
  const PartitionInput input{6, 2, 2, {0, 1, 2, 3, 4, 5}, {0, 1, 2}};
  const auto result = MinNewPartition(input, FrozenWorkBudget(input));
  const auto& c = result.counters;
  Check(!result.fallback && result.chunks == Chunks{{0}, {1}, {2}},
        "disjoint-row chunks incorrect");
  Check(c.reset_rows == 9 && c.csr_visits == 6 && c.work_used == 15,
        "assigned CSR/reset rows must still count toward work");
  Check(c.bucket_adds == 6 && c.bucket_removes == 3 && c.selections == 3,
        "bucket counters incorrect");
}

void BitsetSecondLevel() {
  PartitionInput input;
  input.experts = input.capacity = input.topk = 2;
  constexpr uint32_t rows = 4101;
  for (uint32_t row = 0; row < rows; ++row) {
    input.ids.insert(input.ids.end(), {0, 1});
    input.order.push_back(rows - row - 1);
  }
  const auto result = MinNewPartition(input, FrozenWorkBudget(input));
  Check(!result.fallback && result.chunks == Chunks{input.order},
        "score-bucket summary boundary lost/reordered rows");
  PartitionContract(input, result.chunks);
}

void MetadataBound() {
  PartitionInput input;
  constexpr uint32_t rows = 8192;
  for (uint32_t row = 0; row < rows; ++row) {
    for (uint16_t expert = 0; expert < 10; ++expert)
      input.ids.push_back(expert);
    input.order.push_back(row);
  }
  const auto result = MinNewPartition(input, FrozenWorkBudget(input));
  Check(FrozenWorkBudget(input) == 2621440, "frozen budget differs");
  Check(!result.fallback && result.chunks.size() == 1, "uniform routes split");
  Check(result.counters.metadata_payload_bytes < 512 * 1024,
        "metadata payload no longer below half MiB at frozen dimensions");
  Check(result.counters.work_used == 8192 + 81920,
        "uniform-route work mismatch");
}

void RejectInvalid() {
  std::vector<PartitionInput> bad;
  auto input = Example();
  input.order = {0, 0, 2, 3};
  bad.push_back(input);
  input = Example();
  input.order = {1, 0, 2, 3};
  bad.push_back(input);
  input = Example();
  input.ids[1] = 0;
  bad.push_back(input);
  input = Example();
  input.ids[0] = 4;
  bad.push_back(input);
  input = Example();
  input.capacity = 1;
  bad.push_back(input);
  input = Example();
  input.topk = 0;
  bad.push_back(input);
  input = Example();
  input.ids.pop_back();
  bad.push_back(input);
  input = Example();
  input.order.pop_back();
  bad.push_back(input);
  for (const auto& value : bad) {
    bool rejected = false;
    try {
      ValidatePartitionInput(value);
    } catch (const std::invalid_argument&) {
      rejected = true;
    }
    Check(rejected, "invalid input accepted");
  }
}

void IndependentRandomReference() {
  std::mt19937 random(671);
  for (int trial = 0; trial < 200; ++trial) {
    PartitionInput input;
    input.experts = 17;
    input.topk = 3;
    input.capacity = 7;
    const uint32_t rows = 8 + random() % 38;
    std::vector<int> experts(input.experts);
    std::iota(experts.begin(), experts.end(), 0);
    for (uint32_t row = 0; row < rows; ++row) {
      std::shuffle(experts.begin(), experts.end(), random);
      for (int j = 0; j < input.topk; ++j) {
        input.ids.push_back(static_cast<uint16_t>(experts[j]));
      }
      input.order.push_back(row);
    }
    const auto key = [&](uint32_t row) {
      std::vector<uint16_t> values(input.ids.begin() + row * input.topk,
                                    input.ids.begin() + (row + 1) * input.topk);
      std::sort(values.begin(), values.end());
      return values;
    };
    std::sort(input.order.begin(), input.order.end(),
              [&](uint32_t a, uint32_t b) { return key(a) < key(b); });
    const auto a = MinNewPartition(input, FrozenWorkBudget(input));
    const auto b = MinNewPartition(input, FrozenWorkBudget(input));
    Check(!a.fallback && a.chunks == Reference(input), "reference mismatch");
    Check(a.chunks == b.chunks && a.counters.work_used == b.counters.work_used,
          "nondeterministic partition/work");
    Check(a.counters.work_used == a.counters.reset_rows + a.counters.csr_visits,
          "work accounting mismatch");
    PartitionContract(input, a.chunks);
  }
}

}  // namespace

int main() {
  const std::vector<std::pair<const char*, std::function<void()>>> cases{
      {"golden_partition", GoldenPartition},
      {"whole_forward_budget_fallback", BudgetWholeForwardFallback},
      {"original_cpp_lex_rank_tie", LexRankTie},
      {"hand_counted_work", CountWorkByHand},
      {"bitset_second_level", BitsetSecondLevel},
      {"frozen_metadata_bound", MetadataBound},
      {"invalid_input_rejected", RejectInvalid},
      {"independent_random_reference", IndependentRandomReference}};
  int failed = 0;
  for (const auto& [name, test] : cases) {
    try {
      test();
      std::cout << "[PASS] " << name << '\n';
    } catch (const std::exception& error) {
      ++failed;
      std::cout << "[FAIL] " << name << ": " << error.what() << '\n';
    }
  }
  std::cout << cases.size() << " tests, " << failed << " failed\n";
  return failed ? 1 : 0;
}
