// A disposable, bounded-size device probe. Guardian never links this target.
#include <cuda_runtime.h>

#include <array>
#include <cstdio>

namespace {
__global__ void Transform(int* values) {
  const int index = threadIdx.x;
  values[index] = values[index] * 3 + 7;
}

bool Check(cudaError_t result, const char* stage) {
  if (result == cudaSuccess) return true;
  std::printf(
      "{\"version\":1,\"ok\":false,\"cuda_code\":%d,"
      "\"stage\":\"%s\"}\n",
      static_cast<int>(result), stage);
  std::fflush(stdout);
  return false;
}
}  // namespace

int main() {
  constexpr int size = 256;
  std::array<int, size> values{};
  for (int index = 0; index < size; ++index) values[index] = index;
  int* device = nullptr;
  if (!Check(cudaSetDevice(0), "device") ||
      !Check(cudaMalloc(&device, sizeof(values)), "allocate") ||
      !Check(cudaMemcpy(device, values.data(), sizeof(values),
                        cudaMemcpyHostToDevice),
             "upload"))
    return 1;
  Transform<<<1, size>>>(device);
  if (!Check(cudaGetLastError(), "launch") ||
      !Check(cudaDeviceSynchronize(), "complete") ||
      !Check(cudaMemcpy(values.data(), device, sizeof(values),
                        cudaMemcpyDeviceToHost),
             "download") ||
      !Check(cudaFree(device), "free"))
    return 1;
  for (int index = 0; index < size; ++index) {
    if (values[index] != index * 3 + 7) {
      std::puts(
          "{\"version\":1,\"ok\":false,\"cuda_code\":0,"
          "\"stage\":\"numerical\"}");
      return 2;
    }
  }
  std::puts(
      "{\"version\":1,\"ok\":true,\"cuda_code\":0,"
      "\"stage\":\"complete\"}");
  return 0;
}
