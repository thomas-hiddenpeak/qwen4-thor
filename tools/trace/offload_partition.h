// Offline-only capacity-aware partition experiment. No runtime integration.
#pragma once

#include <algorithm>
#include <bit>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <vector>

namespace q4t::trace {

struct PartitionInput {
  int experts = 512;
  int capacity = 256;
  int topk = 10;
  std::vector<uint16_t> ids;
  // Actual C++ lex sort, including equal-key ties.
  std::vector<uint32_t> order;
};

struct PartitionCounters {
  uint64_t csr_visits = 0;
  uint64_t reset_rows = 0;
  uint64_t bucket_adds = 0;
  uint64_t bucket_removes = 0;
  uint64_t bucket_summary_checks = 0;
  uint64_t bucket_word_clears = 0;
  uint64_t selections = 0;
  uint64_t work_budget = 0;
  uint64_t work_used = 0;
  // Peak vector payload for planner metadata, excluding input, output chunks,
  // vector objects, allocator overhead and validation temporaries. Not RSS.
  uint64_t metadata_payload_bytes = 0;
};

struct PartitionResult {
  std::vector<std::vector<uint32_t>> chunks;
  bool fallback = false;
  PartitionCounters counters;
};

inline void ValidatePartitionInput(const PartitionInput& input) {
  if (input.experts <= 0 || input.experts > 65536 || input.topk <= 0 ||
      input.topk > 63 || input.capacity < input.topk ||
      input.capacity > input.experts || input.ids.empty() ||
      input.ids.size() % static_cast<size_t>(input.topk) != 0) {
    throw std::invalid_argument("invalid partition dimensions");
  }
  const size_t rows = input.ids.size() / input.topk;
  if (rows > 8192 || input.order.size() != rows) {
    throw std::invalid_argument("invalid partition row count/order size");
  }
  std::vector<uint8_t> seen(rows, 0);
  std::vector<uint16_t> previous, key(input.topk);
  for (uint32_t row : input.order) {
    if (row >= rows || seen[row]++) {
      throw std::invalid_argument("token order is not a permutation");
    }
    for (int j = 0; j < input.topk; ++j) {
      key[j] = input.ids[static_cast<size_t>(row) * input.topk + j];
      if (key[j] >= input.experts) {
        throw std::invalid_argument("expert ID out of range");
      }
    }
    std::sort(key.begin(), key.end());
    if (std::adjacent_find(key.begin(), key.end()) != key.end()) {
      throw std::invalid_argument("duplicate expert in top-k row");
    }
    if (!previous.empty() && key < previous) {
      throw std::invalid_argument("token order is not lexicographic");
    }
    previous = key;
  }
}

inline uint64_t FrozenWorkBudget(const PartitionInput& input) {
  return 32 * static_cast<uint64_t>(input.ids.size());
}

inline std::vector<std::vector<uint32_t>> OriginalPartition(
    const PartitionInput& input) {
  ValidatePartitionInput(input);
  std::vector<std::vector<uint32_t>> chunks;
  std::vector<uint32_t> current;
  std::vector<uint8_t> needed(input.experts, 0);
  int distinct = 0;
  for (uint32_t row : input.order) {
    const size_t base = static_cast<size_t>(row) * input.topk;
    int fresh = 0;
    for (int j = 0; j < input.topk; ++j) {
      fresh += !needed[input.ids[base + j]];
    }
    if (distinct + fresh > input.capacity) {
      chunks.push_back(std::move(current));
      current.clear();
      std::fill(needed.begin(), needed.end(), 0);
      distinct = 0;
    }
    for (int j = 0; j < input.topk; ++j) {
      const int expert = input.ids[base + j];
      if (!needed[expert]) {
        needed[expert] = 1;
        ++distinct;
      }
    }
    current.push_back(row);
  }
  if (!current.empty()) chunks.push_back(std::move(current));
  return chunks;
}

namespace partition_internal {

// One bucket per missing-expert count. Row bits are indexed by ORIGINAL lex
// rank. Two-level bitsets select the smallest rank without scanning all rows.
class ScoreBuckets {
 public:
  ScoreBuckets(size_t rows, int topk, PartitionCounters* counters)
      : row_words_((rows + 63) / 64),
        summary_words_((row_words_ + 63) / 64),
        words_((topk + 1) * row_words_, 0),
        summary_((topk + 1) * summary_words_, 0),
        counts_(topk + 1, 0), counters_(counters) {}

  void Reset() {
    std::fill(words_.begin(), words_.end(), 0);
    std::fill(summary_.begin(), summary_.end(), 0);
    std::fill(counts_.begin(), counts_.end(), 0);
    nonempty_scores_ = 0;
    counters_->bucket_word_clears += words_.size() + summary_.size();
  }

  void Add(int score, uint32_t rank) {
    const size_t word = rank / 64;
    words_[score * row_words_ + word] |= uint64_t{1} << (rank % 64);
    summary_[score * summary_words_ + word / 64] |=
        uint64_t{1} << (word % 64);
    ++counts_[score];
    nonempty_scores_ |= uint64_t{1} << score;
    ++counters_->bucket_adds;
  }

  void Remove(int score, uint32_t rank) {
    const size_t word = rank / 64;
    uint64_t& bits = words_[score * row_words_ + word];
    bits &= ~(uint64_t{1} << (rank % 64));
    if (bits == 0) {
      summary_[score * summary_words_ + word / 64] &=
          ~(uint64_t{1} << (word % 64));
    }
    if (--counts_[score] == 0) {
      nonempty_scores_ &= ~(uint64_t{1} << score);
    }
    ++counters_->bucket_removes;
  }

  int BestScore() const {
    if (nonempty_scores_ == 0) {
      throw std::logic_error("empty score buckets with unassigned rows");
    }
    return std::countr_zero(nonempty_scores_);
  }

  uint32_t FirstRank(int score) {
    for (size_t i = 0; i < summary_words_; ++i) {
      ++counters_->bucket_summary_checks;
      const uint64_t bits = summary_[score * summary_words_ + i];
      if (bits == 0) continue;
      const size_t word = i * 64 + std::countr_zero(bits);
      return static_cast<uint32_t>(word * 64 + std::countr_zero(
          words_[score * row_words_ + word]));
    }
    throw std::logic_error("nonempty score has no row bits");
  }

  uint64_t PayloadBytes() const {
    return (words_.capacity() + summary_.capacity()) * sizeof(uint64_t) +
           counts_.capacity() * sizeof(uint32_t);
  }

 private:
  size_t row_words_;
  size_t summary_words_;
  std::vector<uint64_t> words_;
  std::vector<uint64_t> summary_;
  std::vector<uint32_t> counts_;
  uint64_t nonempty_scores_ = 0;
  PartitionCounters* counters_;
};

}  // namespace partition_internal

// Caller fixes the CLI budget to FrozenWorkBudget. The explicit argument lets
// independent small contracts exercise fallback without changing the policy.
// A work unit is one CSR entry visit (even if assigned) or one row-reset scan
// (even if assigned). Before exceeding the budget, discard ALL partial chunks
// and return OriginalPartition. Bucket operations are separately counted.
inline PartitionResult MinNewPartition(const PartitionInput& input,
                                       uint64_t work_budget) {
  ValidatePartitionInput(input);
  const size_t rows = input.order.size();
  PartitionResult result;
  PartitionCounters& counters = result.counters;
  counters.work_budget = work_budget;
  auto fallback = [&]() {
    result.chunks = OriginalPartition(input);
    result.fallback = true;
    return result;
  };
  auto charge = [&]() {
    if (counters.work_used == counters.work_budget) return false;
    ++counters.work_used;
    return true;
  };

  std::vector<uint32_t> offsets(input.experts + 1, 0);
  for (uint16_t expert : input.ids) ++offsets[expert + 1];
  for (int expert = 0; expert < input.experts; ++expert) {
    offsets[expert + 1] += offsets[expert];
  }
  std::vector<uint32_t> references(input.ids.size());
  {
    std::vector<uint32_t> cursor(offsets.begin(), offsets.end() - 1);
    for (uint32_t rank = 0; rank < rows; ++rank) {
      const size_t base = static_cast<size_t>(input.order[rank]) * input.topk;
      for (int j = 0; j < input.topk; ++j) {
        references[cursor[input.ids[base + j]]++] = rank;
      }
    }
    counters.metadata_payload_bytes =
        (offsets.capacity() + cursor.capacity() + references.capacity()) *
        sizeof(uint32_t);
  }
  std::vector<uint8_t> assigned(rows, 0), scores(rows, 0);
  std::vector<uint8_t> needed(input.experts, 0);
  partition_internal::ScoreBuckets buckets(rows, input.topk, &counters);
  counters.metadata_payload_bytes = std::max(
      counters.metadata_payload_bytes,
      static_cast<uint64_t>(
          (offsets.capacity() + references.capacity()) * sizeof(uint32_t) +
          assigned.capacity() + scores.capacity() + needed.capacity() +
          buckets.PayloadBytes()));

  size_t remaining = rows;
  while (remaining > 0) {
    buckets.Reset();
    std::fill(needed.begin(), needed.end(), 0);
    int distinct = 0;
    std::vector<uint32_t> current;
    for (uint32_t rank = 0; rank < rows; ++rank) {
      if (!charge()) return fallback();
      ++counters.reset_rows;
      if (!assigned[rank]) {
        scores[rank] = static_cast<uint8_t>(input.topk);
        buckets.Add(input.topk, rank);
      }
    }
    while (remaining > 0) {
      const int score = buckets.BestScore();
      if (distinct + score > input.capacity) break;
      const uint32_t rank = buckets.FirstRank(score);
      buckets.Remove(score, rank);
      assigned[rank] = 1;
      --remaining;
      ++counters.selections;
      const uint32_t row = input.order[rank];
      current.push_back(row);
      const size_t base = static_cast<size_t>(row) * input.topk;
      for (int j = 0; j < input.topk; ++j) {
        const uint16_t expert = input.ids[base + j];
        if (needed[expert]) continue;
        needed[expert] = 1;
        ++distinct;
        for (uint32_t i = offsets[expert]; i < offsets[expert + 1]; ++i) {
          if (!charge()) return fallback();
          ++counters.csr_visits;
          const uint32_t other = references[i];
          if (assigned[other]) continue;
          const int old = scores[other];
          if (old <= 0)
            throw std::logic_error("missing-expert score underflow");
          buckets.Remove(old, other);
          scores[other] = static_cast<uint8_t>(old - 1);
          buckets.Add(old - 1, other);
        }
      }
    }
    if (current.empty()) throw std::logic_error("empty min-new chunk");
    result.chunks.push_back(std::move(current));
  }
  return result;
}

}  // namespace q4t::trace
