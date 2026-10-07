// Source-faithful selected MoE schedule; no model, timing or GPU execution.
// stdin: LE uint32 experts, capacity, topk, rows, policy; uint16 router IDs.
// policy 0: legacy. policy 1: production min-new when rows > 1, else legacy.
// The caller applies request-length/global policy before invoking this tool.
#include "research/moe_partition.h"

#include <array>
#include <bit>
#include <iostream>
#include <numeric>

namespace {

using Chunks = std::vector<std::vector<int32_t>>;

template <typename T>
bool Read(T* data, size_t count) {
  return static_cast<bool>(std::cin.read(
      reinterpret_cast<char*>(data),
      static_cast<std::streamsize>(count * sizeof(T))));
}

template <typename T>
void WriteArray(const std::vector<T>& values) {
  std::cout << '[';
  for (size_t i = 0; i < values.size(); ++i) {
    if (i != 0) std::cout << ',';
    std::cout << values[i];
  }
  std::cout << ']';
}

void ValidateChunks(const Chunks& chunks, const std::vector<int32_t>& ids,
                    int experts, int capacity, int topk, size_t rows) {
  if (chunks.empty()) throw std::logic_error("empty schedule");
  std::vector<uint8_t> seen(rows, 0);
  for (const auto& chunk : chunks) {
    if (chunk.empty()) throw std::logic_error("empty chunk");
    std::vector<uint8_t> needed(experts, 0);
    int distinct = 0;
    for (int32_t row : chunk) {
      if (row < 0 || static_cast<size_t>(row) >= rows || seen[row]++) {
        throw std::logic_error("invalid chunk row permutation");
      }
      const size_t base = static_cast<size_t>(row) * topk;
      for (int j = 0; j < topk; ++j) {
        const int expert = ids[base + j];
        if (!needed[expert]) {
          needed[expert] = 1;
          ++distinct;
        }
      }
    }
    if (distinct > capacity) throw std::logic_error("chunk exceeds capacity");
  }
  if (std::find(seen.begin(), seen.end(), uint8_t{0}) != seen.end()) {
    throw std::logic_error("schedule omitted a row");
  }
}

}  // namespace

int main(int argc, char**) {
  static_assert(std::endian::native == std::endian::little);
  if (argc != 1) return 2;
  std::array<uint32_t, 5> header{};
  if (!Read(header.data(), header.size()) || header[0] == 0 ||
      header[0] > 65536 || header[1] == 0 || header[1] > header[0] ||
      header[2] == 0 || header[2] > 16 || header[2] > header[1] ||
      header[3] == 0 || header[3] > 8192 || header[4] > 1) {
    return 2;
  }
  const int experts = static_cast<int>(header[0]);
  const int capacity = static_cast<int>(header[1]);
  const int topk = static_cast<int>(header[2]);
  const size_t rows = header[3];
  const bool requested = header[4] == 1;
  std::vector<uint16_t> packed(rows * topk);
  if (!Read(packed.data(), packed.size()) ||
      std::cin.peek() != std::char_traits<char>::eof()) {
    return 2;
  }

  try {
    // Match moe.cu exactly: int32 keys, int32 iota order, std::sort and a
    // lexicographic comparator without an additional equal-key tie breaker.
    const std::vector<int32_t> ids(packed.begin(), packed.end());
    std::vector<int32_t> keys = ids;
    for (size_t row = 0; row < rows; ++row) {
      int32_t* key = keys.data() + row * topk;
      std::sort(key, key + topk);
      if (key[topk - 1] >= experts ||
          std::adjacent_find(key, key + topk) != key + topk) {
        throw std::invalid_argument("invalid router expert row");
      }
    }
    std::vector<int32_t> order(rows);
    std::iota(order.begin(), order.end(), 0);
    if (rows > 1) {
      std::sort(order.begin(), order.end(), [&](int a, int b) {
        const int32_t* ka = keys.data() + static_cast<size_t>(a) * topk;
        const int32_t* kb = keys.data() + static_cast<size_t>(b) * topk;
        return std::lexicographical_compare(ka, ka + topk, kb, kb + topk);
      });
    }

    Chunks chunks;
    bool applied = false;
    bool fallback = false;
    q4t::model::PartitionCounters counters;
    if (requested && rows > 1) {
      auto planned = q4t::model::PlanMoEPartition(
          experts, capacity, topk, false, ids, order);
      chunks = std::move(planned.chunks);
      fallback = planned.fallback;
      applied = !fallback;
      counters = planned.counters;
    } else {
      q4t::model::PartitionInput input;
      input.experts = experts;
      input.capacity = capacity;
      input.topk = topk;
      input.ids = std::move(packed);
      input.order.assign(order.begin(), order.end());
      // For unique router rows and no hot protection, this is equivalent
      // to moe.cu's flush-and-reprobe legacy path. No min-new work occurs.
      const auto legacy = q4t::model::OriginalPartition(input);
      chunks.reserve(legacy.size());
      for (const auto& chunk : legacy) {
        chunks.emplace_back(chunk.begin(), chunk.end());
      }
    }
    ValidateChunks(chunks, ids, experts, capacity, topk, rows);

    std::cout << "{\"schema\":1,\"token_order\":";
    WriteArray(order);
    std::cout << ",\"chunks\":[";
    for (size_t i = 0; i < chunks.size(); ++i) {
      if (i != 0) std::cout << ',';
      WriteArray(chunks[i]);
    }
    std::cout << "],\"partition_applied\":" << (applied ? "true" : "false")
              << ",\"fallback\":" << (fallback ? "true" : "false")
              << ",\"counters\":{\"csr_visits\":" << counters.csr_visits
              << ",\"reset_rows\":" << counters.reset_rows
              << ",\"bucket_adds\":" << counters.bucket_adds
              << ",\"bucket_removes\":" << counters.bucket_removes
              << ",\"bucket_summary_checks\":"
              << counters.bucket_summary_checks
              << ",\"bucket_word_clears\":" << counters.bucket_word_clears
              << ",\"selections\":" << counters.selections
              << ",\"work_budget\":" << counters.work_budget
              << ",\"work_used\":" << counters.work_used
              << ",\"metadata_payload_bytes\":"
              << counters.metadata_payload_bytes << "}}\n";
  } catch (const std::exception& error) {
    std::cerr << "GPU cache schedule rejected: " << error.what() << '\n';
    return 2;
  }
  return std::cout ? 0 : 3;
}
