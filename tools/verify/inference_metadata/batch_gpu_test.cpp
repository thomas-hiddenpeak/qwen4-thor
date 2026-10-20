#include "q4t/model/metadata_upload_batch.h"

#include <array>
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <thread>

namespace {
void Check(cudaError_t error) {
  if (error != cudaSuccess) throw std::runtime_error(cudaGetErrorString(error));
}
void Check(q4t::Status status) {
  if (!status.ok()) throw std::runtime_error(status.message());
}
void Delay(void*) {
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
}
}  // namespace

int main() {
  try {
    cudaStream_t other = nullptr;
    Check(cudaStreamCreateWithFlags(&other, cudaStreamNonBlocking));
    std::array<int*, 4> target{};
    for (auto& pointer : target) {
      Check(cudaMalloc(&pointer, 16 * sizeof(int)));
      Check(cudaMemset(pointer, 0xff, 16 * sizeof(int)));
    }
    Check(cudaDeviceSynchronize());
    const int counts[] = {8, 4, 3, 2};
    {
      q4t::model::MetadataUploadBatch batch;
      for (int step = 0; step < 4; ++step) {
        const cudaStream_t stream = step < 2 ? nullptr : other;
        Check(batch.Begin(stream));
        Check(cudaLaunchHostFunc(stream, Delay, nullptr));
        std::array<int, 8> source;
        for (int i = 0; i < 8; ++i) source[i] = step * 100 + i;
        if (batch.Copy(nullptr, source.data(), sizeof(int)).ok())
          throw std::runtime_error("invalid destination accepted");
        Check(batch.Copy(target[step], source.data(), counts[step] * sizeof(int)));
        Check(batch.Copy(target[step] + 8, source.data(),
                         counts[step] * sizeof(int)));
        source.fill(-77);
        if (step != 0) Check(batch.Finish());
      }
      // Last batch still owns its upload sources when this scope ends.
    }
    int checked = 0;
    for (int step = 0; step < 4; ++step) {
      std::array<int, 16> result;
      Check(cudaMemcpy(result.data(), target[step], sizeof(result),
                       cudaMemcpyDeviceToHost));
      for (int i = 0; i < 16; ++i) {
        const int local = i % 8;
        const int expected = local < counts[step] ? step * 100 + local : -1;
        if (result[i] != expected) throw std::runtime_error("source mismatch");
        ++checked;
      }
      Check(cudaFree(target[step]));
    }
    Check(cudaStreamDestroy(other));
    std::cout << "PASS 4 batches, 8 H2D, " << checked << " exact ints\n";
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
