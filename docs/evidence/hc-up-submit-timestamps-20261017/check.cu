#include <cuda_runtime.h>
#include <cupti.h>
#include <cupti_activity.h>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>

std::atomic<int> records{0};
std::atomic<int> invalid{0};
std::atomic<size_t> dropped{0};
void Check(CUptiResult result) {
  if (result != CUPTI_SUCCESS) {
    const char* message = nullptr;
    cuptiGetResultString(result, &message);
    std::fprintf(stderr, "CUPTI: %s\n", message);
    std::exit(1);
  }
}
void CheckCuda(cudaError_t result) {
  if (result != cudaSuccess) {
    std::fprintf(stderr, "CUDA: %s\n", cudaGetErrorString(result));
    std::exit(1);
  }
}
void CUPTIAPI Request(uint8_t** buffer, size_t* size, size_t* maximum) {
  *size = 1024 * 1024;
  *maximum = 0;
  if (posix_memalign(reinterpret_cast<void**>(buffer), 8, *size)) std::exit(1);
}
void CUPTIAPI Complete(CUcontext context, uint32_t stream, uint8_t* buffer,
                       size_t, size_t valid) {
  CUpti_Activity* record = nullptr;
  while (true) {
    auto result = cuptiActivityGetNextRecord(buffer, valid, &record);
    if (result == CUPTI_ERROR_MAX_LIMIT_REACHED) break;
    Check(result);
    if (record->kind != CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL) continue;
    auto* kernel = reinterpret_cast<CUpti_ActivityKernel12*>(record);
    if (!std::strstr(kernel->name, "TimestampProbe")) continue;
    ++records;
    bool ordered = kernel->queued != CUPTI_TIMESTAMP_UNKNOWN &&
                   kernel->submitted != CUPTI_TIMESTAMP_UNKNOWN &&
                   kernel->queued <= kernel->submitted &&
                   kernel->submitted <= kernel->start &&
                   kernel->start < kernel->end;
    if (!ordered) ++invalid;
    std::printf("{\"queued\":%llu,\"submitted\":%llu,\"start\":%llu,"
                "\"end\":%llu,\"ordered\":%s}\n",
                static_cast<unsigned long long>(kernel->queued),
                static_cast<unsigned long long>(kernel->submitted),
                static_cast<unsigned long long>(kernel->start),
                static_cast<unsigned long long>(kernel->end),
                ordered ? "true" : "false");
  }
  size_t count = 0;
  Check(cuptiActivityGetNumDroppedRecords(context, stream, &count));
  dropped += count;
  std::free(buffer);
}
__global__ void TimestampProbe(int* value) { ++*value; }
int main() {
  Check(cuptiActivityEnableLatencyTimestamps(1));
  Check(cuptiActivityRegisterCallbacks(Request, Complete));
  Check(cuptiActivityEnable(CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL));
  int* value = nullptr;
  CheckCuda(cudaMalloc(&value, sizeof(int)));
  CheckCuda(cudaMemset(value, 0, sizeof(int)));
  for (int i = 0; i < 3; ++i) {
    TimestampProbe<<<1, 1>>>(value);
    CheckCuda(cudaGetLastError());
    CheckCuda(cudaDeviceSynchronize());
  }
  int host = 0;
  CheckCuda(cudaMemcpy(&host, value, sizeof(int), cudaMemcpyDeviceToHost));
  CheckCuda(cudaFree(value));
  Check(cuptiActivityFlushAll(0));
  std::printf("{\"records\":%d,\"invalid\":%d,\"dropped\":%zu,\"value\":%d}\n",
              records.load(), invalid.load(), dropped.load(), host);
  return records == 3 && invalid == 0 && dropped == 0 && host == 3 ? 0 : 1;
}
