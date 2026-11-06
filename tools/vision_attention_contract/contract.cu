// Exact source kernels, independent parent/candidate output comparison.
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace {

#include "kernels.inc"

void Check(cudaError_t error) {
  if (error != cudaSuccess) {
    std::fprintf(stderr, "CUDA failure: %s\n", cudaGetErrorString(error));
    std::exit(2);
  }
}

uint16_t Bf16(float value) {
  const auto result = __float2bfloat16_rn(value);
  uint16_t bits;
  std::memcpy(&bits, &result, sizeof(bits));
  return bits;
}

uint32_t Next(uint32_t& state) {
  state ^= state << 13;
  state ^= state >> 17;
  state ^= state << 5;
  return state;
}

struct Buffer {
  uint16_t* data = nullptr;
  explicit Buffer(size_t bytes) {
    Check(cudaMalloc(reinterpret_cast<void**>(&data), bytes));
  }
  ~Buffer() { Check(cudaFree(data)); }
  Buffer(const Buffer&) = delete;
  Buffer& operator=(const Buffer&) = delete;
};

size_t RunCase(int spatial, int temporal, int pattern) {
  constexpr int kHeads = 16;
  constexpr int kHeadDim = 72;
  constexpr int kHidden = kHeads * kHeadDim;
  const int length = spatial * temporal;
  const size_t count = static_cast<size_t>(length) * kHidden;
  std::vector<uint16_t> input(count * 3), old(count), current(count);
  uint32_t state = 20261107U + spatial * 17U + temporal * 3U + pattern;
  for (int l = 0; l < length; ++l) {
    for (int part = 0; part < 3; ++part) {
      for (int d = 0; d < kHidden; ++d) {
        float value = (static_cast<int>(Next(state) % 4097) - 2048) / 1024.f;
        if (part < 2) {
          if (pattern == 1) value = (l + d) % 2 ? 0.f : -0.f;
          if (pattern == 2) value = part == 0 ? 1.e16f : -1.e16f;
          if (pattern == 3) value *= 100.f;
          if (pattern == 4) value *= 1.e-20f;
          if (pattern == 5) value = (d % 7 - 3) / 4.f;
        }
        auto bits = Bf16(value);
        if (pattern == 6 && part < 2 && d % 17 == 0) {
          const uint16_t special[] = {0x7fc1, 0x7f80, 0xff80};
          bits = special[(l + d) % 3];
        }
        input[(static_cast<size_t>(l) * 3 + part) * kHidden + d] = bits;
      }
    }
  }
  Buffer qkv(input.size() * sizeof(uint16_t));
  Buffer output(count * sizeof(uint16_t));
  Check(cudaMemcpy(qkv.data, input.data(), input.size() * sizeof(uint16_t),
                   cudaMemcpyHostToDevice));
  const dim3 grid(length, kHeads);
  const float scale = 1.f / std::sqrt(static_cast<float>(kHeadDim));
  ParentAttentionKernel<<<grid, kBlock, spatial * sizeof(float)>>>(
      qkv.data, output.data, length, temporal, spatial, kHeads, kHeadDim,
      kHidden, scale);
  Check(cudaPeekAtLastError());
  Check(cudaDeviceSynchronize());
  Check(cudaMemcpy(old.data(), output.data, count * sizeof(uint16_t),
                   cudaMemcpyDeviceToHost));
  Check(cudaMemset(output.data, 0xa5, count * sizeof(uint16_t)));
  CandidateAttentionKernel<<<grid, kBlock, spatial * sizeof(float)>>>(
      qkv.data, output.data, length, temporal, spatial, kHeads, kHeadDim,
      kHidden, scale);
  Check(cudaPeekAtLastError());
  Check(cudaDeviceSynchronize());
  Check(cudaMemcpy(current.data(), output.data, count * sizeof(uint16_t),
                   cudaMemcpyDeviceToHost));
  size_t mismatches = 0, nonfinite = 0;
  for (size_t i = 0; i < count; ++i) {
    mismatches += old[i] != current[i];
    nonfinite += (current[i] & 0x7f80) == 0x7f80;
  }
  std::printf("{\"spatial\":%d,\"temporal\":%d,\"pattern\":%d,"
              "\"elements\":%zu,\"mismatches\":%zu,\"nonfinite\":%zu}\n",
              spatial, temporal, pattern, count, mismatches, nonfinite);
  std::fflush(stdout);
  if (mismatches || (pattern != 2 && pattern != 6 && nonfinite)) {
    std::exit(1);
  }
  return count;
}

}  // namespace

int main() {
  cudaDeviceProp device;
  Check(cudaGetDeviceProperties(&device, 0));
  if (device.major != 11 || device.minor != 0 || !device.integrated) return 2;
  cudaFuncAttributes parent, candidate;
  Check(cudaFuncGetAttributes(&parent, ParentAttentionKernel));
  Check(cudaFuncGetAttributes(&candidate, CandidateAttentionKernel));
  std::printf("{\"parent_registers\":%d,\"candidate_registers\":%d,"
              "\"parent_shared\":%zu,\"candidate_shared\":%zu,"
              "\"parent_local\":%zu,\"candidate_local\":%zu}\n",
              parent.numRegs, candidate.numRegs, parent.sharedSizeBytes,
              candidate.sharedSizeBytes, parent.localSizeBytes,
              candidate.localSizeBytes);
  if (parent.sharedSizeBytes != 1024 || candidate.sharedSizeBytes != 1024 ||
      parent.localSizeBytes || candidate.localSizeBytes) return 2;
  size_t elements = 0;
  for (int spatial : {1, 2, 3, 31, 32, 33, 63, 64, 65, 127, 128, 129,
                      255, 256, 257, 384, 1024, 2640, 8192}) {
    elements += RunCase(spatial, 1, 0);
  }
  elements += RunCase(384, 4, 0);
  for (int pattern = 1; pattern <= 6; ++pattern) {
    elements += RunCase(257, 1, pattern);
  }
  Check(cudaDeviceSynchronize());
  std::printf("{\"passed\":true,\"cases\":26,\"bf16_elements\":%zu}\n",
              elements);
  return 0;
}
