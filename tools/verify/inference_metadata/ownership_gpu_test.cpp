#include "q4t/model/decode_metadata_upload.h"
#include <algorithm>
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <vector>

void Check(cudaError_t e) {
  if (e != cudaSuccess) throw std::runtime_error(cudaGetErrorString(e));
}
void Check(const q4t::Status& s) {
  if (!s.ok()) throw std::runtime_error(s.message());
}
void CUDART_CB Delay(void*) {
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
}
int main() {
  using q4t::model::DecodeMetadataUpload;
  try {
    cudaStream_t streams[2]{};
    for (auto& stream : streams)
      Check(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));
    int* device = nullptr;
    constexpr int stride = 54;  // three 2-element arrays + [2,3,8] RoPE.
    Check(cudaMalloc(&device, 4 * stride * sizeof(int)));
    Check(cudaMemset(device, 0xff, 4 * stride * sizeof(int)));
    std::vector<int> expected(4 * stride, -1);
    {
      DecodeMetadataUpload upload;
      Check(upload.Init(2));
      for (int step = 0; step < 4; ++step) {
        const int rows = step % 2 == 0 ? 2 : 1;
        int ids[2] = {17 + step, 23 + step};
        int pos[2] = {step, 7 - step};
        int slots[2] = {1, 0};
        int delta[2] = {-2, 5};
        int* dst = device + step * stride;
        DecodeMetadataUpload::Targets targets{dst, dst + 2, dst + 4,
                                              dst + 6, 8, 2};
        const auto stream = streams[step < 2 ? 0 : 1];
        if (step == 0) {
          const int invalid[2] = {-1, 0};
          if (upload.Upload(ids, pos, invalid, delta, targets, stream).ok())
            throw std::runtime_error("accepted invalid slot");
          int guard[stride];
          Check(cudaMemcpy(guard, dst, sizeof(guard), cudaMemcpyDeviceToHost));
          if (!std::all_of(guard, guard + stride, [](int v) { return v == -1; }))
            throw std::runtime_error("invalid upload mutated target");
        }
        Check(cudaLaunchHostFunc(stream, Delay, nullptr));
        Check(upload.Upload({ids, size_t(rows)}, {pos, size_t(rows)},
                            {slots, size_t(rows)}, delta, targets, stream));
        for (int row = 0; row < rows; ++row) {
          const int base = step * stride;
          expected[base + row] = ids[row];
          expected[base + 2 + row] = pos[row];
          expected[base + 4 + row] = slots[row];
          for (int axis = 0; axis < 3; ++axis)
            expected[base + 6 + (slots[row] * 3 + axis) * 8 + pos[row]] =
                pos[row] + delta[slots[row]];
        }
        // Original arrays are reused immediately while DMA is delayed.
        std::fill(ids, ids + 2, 999);
        std::fill(pos, pos + 2, 999);
        std::fill(slots, slots + 2, 999);
        std::fill(delta, delta + 2, 999);
      }
      // Last transfer must finish through destructor, without explicit Drain.
    }
    std::vector<int> actual(expected.size());
    Check(cudaMemcpy(actual.data(), device, actual.size() * sizeof(int),
                     cudaMemcpyDeviceToHost));
    if (actual != expected) throw std::runtime_error("snapshot mismatch");
    Check(cudaFree(device));
    for (auto stream : streams) Check(cudaStreamDestroy(stream));
    std::cout << "PASS four uploads, 216 exact values, invalid no-mutation\n";
  } catch (const std::exception& e) {
    std::cerr << "FAIL " << e.what() << '\n'; return 1;
  }
}
