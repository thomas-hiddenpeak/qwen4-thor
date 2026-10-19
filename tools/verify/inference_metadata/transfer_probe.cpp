// Bounded CUDA transfer-lifetime probe; intentionally reuses live source data
// in the negative control. No freed memory, model loading or kernel changes.
#include <cuda_runtime.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <stdexcept>
#include <thread>
#include <vector>

void Check(cudaError_t error) {
  if (error != cudaSuccess) throw std::runtime_error(cudaGetErrorString(error));
}
void CUDART_CB Delay(void*) {
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
}
int main() {
  try {
    int device = 0, pageable = 0, host_tables = 0;
    Check(cudaGetDevice(&device));
    Check(cudaDeviceGetAttribute(&pageable, cudaDevAttrPageableMemoryAccess,
                                device));
    Check(cudaDeviceGetAttribute(&host_tables,
        cudaDevAttrPageableMemoryAccessUsesHostPageTables, device));
    std::printf("{\"pageable_access\":%d,\"host_page_tables\":%d,\"cases\":[",
                pageable, host_tables);
    bool first = true;
    for (int count : {1, 4, 8192, 3 * 208896}) {
      for (bool pinned : {false, true}) {
        for (bool wait : {false, true}) {
          std::vector<int> storage(count), output(count);
          int* source = storage.data();
          if (pinned) Check(cudaMallocHost(&source, count * sizeof(int)));
          std::fill(source, source + count, 12345);
          int* destination = nullptr;
          cudaStream_t stream = nullptr;
          cudaEvent_t done = nullptr;
          Check(cudaMalloc(&destination, count * sizeof(int)));
          Check(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));
          Check(cudaEventCreateWithFlags(&done, cudaEventDisableTiming));
          Check(cudaLaunchHostFunc(stream, Delay, nullptr));
          const auto start = std::chrono::steady_clock::now();
          Check(cudaMemcpyAsync(destination, source, count * sizeof(int),
                                cudaMemcpyHostToDevice, stream));
          const auto elapsed = std::chrono::steady_clock::now() - start;
          Check(cudaEventRecord(done, stream));
          const auto query = cudaEventQuery(done);
          if (query != cudaSuccess && query != cudaErrorNotReady) Check(query);
          if (wait) Check(cudaEventSynchronize(done));
          std::fill(source, source + count, 54321);
          Check(cudaStreamSynchronize(stream));
          Check(cudaMemcpy(output.data(), destination, count * sizeof(int),
                           cudaMemcpyDeviceToHost));
          const auto original = std::count(output.begin(), output.end(), 12345);
          const auto overwritten = std::count(output.begin(), output.end(), 54321);
          if (wait && original != count)
            throw std::runtime_error("completion control mismatch");
          if (!first) std::printf(",");
          first = false;
          std::printf("{\"count\":%d,\"pinned\":%s,\"wait\":%s,"
                      "\"complete_before_reuse\":%s,\"copy_call_us\":%lld,"
                      "\"original\":%lld,\"overwritten\":%lld}", count,
                      pinned ? "true" : "false", wait ? "true" : "false",
                      query == cudaSuccess ? "true" : "false",
                      static_cast<long long>(std::chrono::duration_cast<
                          std::chrono::microseconds>(elapsed).count()),
                      static_cast<long long>(original),
                      static_cast<long long>(overwritten));
          Check(cudaEventDestroy(done));
          Check(cudaStreamDestroy(stream));
          Check(cudaFree(destination));
          if (pinned) Check(cudaFreeHost(source));
        }
      }
    }
    std::puts("],\"status\":\"PASS\"}");
  } catch (const std::exception& error) {
    std::fprintf(stderr, "FAIL %s\n", error.what());
    return 1;
  }
}
