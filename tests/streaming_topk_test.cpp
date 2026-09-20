// Run only after the real HTTP quality and five-length performance gate.
// Check both the mathematical top-k scores and the old network's exact IDs:
// a generic sort alone cannot specify equal-score ordering for this model.
#include "q4t/model/streaming_topk.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <bit>
#include <cstdint>
#include <cstdio>
#include <vector>

namespace {

constexpr int kChunk = 2048;
constexpr int kTopk = 512;
constexpr int kChunks = 25;
constexpr float kInvalid = -1e30f;

struct Entry {
  float value;
  int index;
};

// Compatibility oracle: the original shared-memory sorting network, serial
// on the CPU. Stages contain disjoint pairs, so thread scheduling is absent.
void OriginalNetwork(std::vector<Entry>* entries) {
  const int size = static_cast<int>(entries->size());
  for (int k = 2; k <= size; k *= 2) {
    for (int j = k / 2; j > 0; j /= 2) {
      for (int i = 0; i < size; ++i) {
        if ((i & (2 * j - 1)) >= j) continue;
        Entry& a = (*entries)[i];
        Entry& b = (*entries)[i + j];
        const bool ascending = (i & (2 * k - 1)) < k;
        if (ascending ? a.value > b.value : a.value < b.value) {
          std::swap(a, b);
        }
      }
    }
  }
}

float Score(int row, int global_index) {
  switch (row % 8) {
    case 0:  // Distinct scores in a permuted order, including negatives.
      return static_cast<float>((global_index * 40503) & 65535) - 32768.f;
    case 1:
      return 100000.f - static_cast<float>(global_index);
    case 2:  // Many ties, including the top-k boundary.
      return static_cast<float>(global_index % 7);
    case 3:  // Equal numeric scores with different bit representations.
      return (global_index & 1) ? -0.f : 0.f;
    case 4:
      return kInvalid;
    case 5:  // Partial visible chunk beyond the former 8192-token limit.
      return global_index < 9001 ? global_index % 17 : kInvalid;
    case 6:  // All winners appear only in the final candidate chunk.
      return global_index >= 24 * kChunk && global_index < 24 * kChunk + kTopk
                 ? static_cast<float>(global_index)
                 : kInvalid;
    default:
      return global_index % 31 < 3 ? kInvalid
                                   : static_cast<float>(global_index % 257);
  }
}

struct Buffers {
  float* logits = nullptr;
  float* values = nullptr;
  int* indices = nullptr;
  ~Buffers() {
    cudaFree(logits);
    cudaFree(values);
    cudaFree(indices);
  }
};

bool CheckRows(int rows) {
  Buffers device;
  const size_t count = static_cast<size_t>(rows) * kTopk;
  std::vector<float> logits(static_cast<size_t>(rows) * kChunk);
  std::vector<float> expected_values(count, kInvalid);
  std::vector<int> expected_indices(count, -1);
  std::vector<float> actual_values(count);
  std::vector<int> actual_indices(count);
  Q4T_CHECK(cudaMalloc(&device.logits, logits.size() * sizeof(float)) ==
            cudaSuccess);
  Q4T_CHECK(cudaMalloc(&device.values, count * sizeof(float)) == cudaSuccess);
  Q4T_CHECK(cudaMalloc(&device.indices, count * sizeof(int)) == cudaSuccess);
  Q4T_CHECK(cudaMemcpy(device.values, expected_values.data(),
                       count * sizeof(float), cudaMemcpyHostToDevice) ==
            cudaSuccess);
  Q4T_CHECK(cudaMemcpy(device.indices, expected_indices.data(),
                       count * sizeof(int), cudaMemcpyHostToDevice) ==
            cudaSuccess);
  for (int chunk = 0; chunk < kChunks; ++chunk) {
    for (int row = 0; row < rows; ++row) {
      std::vector<Entry> local(kChunk);
      std::vector<Entry> merged(2 * kTopk);
      std::vector<float> mathematical;
      mathematical.reserve(kChunk + kTopk);
      for (int i = 0; i < kChunk; ++i) {
        const int index = chunk * kChunk + i;
        const float score = Score(row, index);
        logits[row * kChunk + i] = score;
        local[i] = {score, index};
        mathematical.push_back(score);
      }
      OriginalNetwork(&local);
      for (int i = 0; i < kTopk; ++i) {
        const int offset = row * kTopk + i;
        merged[i] = {expected_values[offset], expected_indices[offset]};
        merged[kTopk + i] = local[kChunk - kTopk + i];
        mathematical.push_back(expected_values[offset]);
      }
      OriginalNetwork(&merged);
      std::sort(mathematical.begin(), mathematical.end());
      for (int i = 0; i < kTopk; ++i) {
        const int offset = row * kTopk + i;
        const Entry entry = merged[kTopk + i];
        Q4T_CHECK(entry.value == mathematical[kChunk + i]);
        expected_values[offset] = entry.value;
        expected_indices[offset] = entry.index;
      }
    }
    Q4T_CHECK(cudaMemcpy(device.logits, logits.data(),
                         logits.size() * sizeof(float),
                         cudaMemcpyHostToDevice) == cudaSuccess);
    q4t::model::MergeStreamingTopk(device.logits, chunk * kChunk, rows,
                                  device.values, device.indices, nullptr);
    Q4T_CHECK(cudaGetLastError() == cudaSuccess);
    Q4T_CHECK(cudaMemcpy(actual_values.data(), device.values,
                         count * sizeof(float), cudaMemcpyDeviceToHost) ==
              cudaSuccess);
    Q4T_CHECK(cudaMemcpy(actual_indices.data(), device.indices,
                         count * sizeof(int), cudaMemcpyDeviceToHost) ==
              cudaSuccess);
    for (size_t i = 0; i < count; ++i) {
      if (actual_indices[i] != expected_indices[i] ||
          std::bit_cast<uint32_t>(actual_values[i]) !=
              std::bit_cast<uint32_t>(expected_values[i])) {
        std::printf("  mismatch: rows=%d chunk=%d row=%zu rank=%zu\n", rows,
                    chunk, i / kTopk, i % kTopk);
        return false;
      }
    }
  }
  std::printf("  rows=%d: 25 chunks, all top-512 scores/IDs bit-exact\n", rows);
  return true;
}

}  // namespace

Q4T_TEST(streaming_topk_exact) {
  int devices = 0;
  Q4T_CHECK(cudaGetDeviceCount(&devices) == cudaSuccess && devices > 0);
  for (int rows : {1, 3, 17}) Q4T_CHECK(CheckRows(rows));
  return true;
}
