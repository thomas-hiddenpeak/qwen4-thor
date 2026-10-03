// Offline-only min-new partition adapter. Compile as a standalone host tool.
// stdin little-endian: uint32 experts, capacity, topk, rows; rows*topk uint16
// expert IDs in router order; rows uint32 actual C++ lex-sort token indices.
// stdout JSON includes both partitions and operation counts. No GPU/model I/O.
#include "offload_partition.h"

#include <array>
#include <bit>
#include <chrono>
#include <iostream>

namespace {

template <typename T>
bool Read(T* data, size_t count) {
  return static_cast<bool>(std::cin.read(
      reinterpret_cast<char*>(data),
      static_cast<std::streamsize>(count * sizeof(T))));
}

void WriteChunks(const std::vector<std::vector<uint32_t>>& chunks) {
  std::cout << '[';
  for (size_t j = 0; j < chunks.size(); ++j) {
    if (j != 0) std::cout << ',';
    std::cout << '[';
    for (size_t i = 0; i < chunks[j].size(); ++i) {
      if (i != 0) std::cout << ',';
      std::cout << chunks[j][i];
    }
    std::cout << ']';
  }
  std::cout << ']';
}

}  // namespace

int main() {
  if constexpr (std::endian::native != std::endian::little) {
    std::cerr << "partition adapter requires little-endian host\n";
    return 2;
  }
  std::array<uint32_t, 4> header{};
  if (!Read(header.data(), header.size()) || header[0] == 0 ||
      header[0] > 65536 || header[1] == 0 || header[1] > header[0] ||
      header[2] == 0 || header[2] > 63 || header[2] > header[1] ||
      header[3] == 0 || header[3] > 8192) return 2;
  q4t::trace::PartitionInput input;
  input.experts = static_cast<int>(header[0]);
  input.capacity = static_cast<int>(header[1]);
  input.topk = static_cast<int>(header[2]);
  input.ids.resize(static_cast<size_t>(header[3]) * header[2]);
  input.order.resize(header[3]);
  if (!Read(input.ids.data(), input.ids.size()) ||
      !Read(input.order.data(), input.order.size()) ||
      std::cin.peek() != std::char_traits<char>::eof()) return 2;
  try {
    const auto start = std::chrono::steady_clock::now();
    const auto baseline = q4t::trace::OriginalPartition(input);
    const auto middle = std::chrono::steady_clock::now();
    const auto result = q4t::trace::MinNewPartition(
        input, q4t::trace::FrozenWorkBudget(input));
    const auto end = std::chrono::steady_clock::now();
    const auto micros = [](auto a, auto b) {
      return std::chrono::duration_cast<std::chrono::microseconds>(b - a)
          .count();
    };
    const auto& c = result.counters;
    std::cout << "{\"schema\":1,\"fallback\":"
              << (result.fallback ? "true" : "false")
              << ",\"baseline_us\":" << micros(start, middle)
              << ",\"candidate_us\":" << micros(middle, end)
              << ",\"counters\":{\"csr_visits\":" << c.csr_visits
              << ",\"reset_rows\":" << c.reset_rows
              << ",\"bucket_adds\":" << c.bucket_adds
              << ",\"bucket_removes\":" << c.bucket_removes
              << ",\"bucket_summary_checks\":" << c.bucket_summary_checks
              << ",\"bucket_word_clears\":" << c.bucket_word_clears
              << ",\"selections\":" << c.selections
              << ",\"work_budget\":" << c.work_budget
              << ",\"work_used\":" << c.work_used
              << ",\"metadata_payload_bytes\":" << c.metadata_payload_bytes
              << "},\"baseline_chunks\":";
    WriteChunks(baseline);
    std::cout << ",\"candidate_chunks\":";
    WriteChunks(result.chunks);
    std::cout << "}\n";
  } catch (const std::exception& error) {
    std::cerr << "partition rejected: " << error.what() << '\n';
    return 2;
  }
  return std::cout ? 0 : 3;
}
