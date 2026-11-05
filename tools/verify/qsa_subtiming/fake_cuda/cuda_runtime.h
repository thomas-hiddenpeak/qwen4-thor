#pragma once

// CPU-only fault injection for the standalone observer contract executable.
// This include directory must never be used by an engine build.
namespace fake_cuda {
struct Event {
  bool recorded = false;
};
inline int live = 0;
inline int creates = 0;
inline int records = 0;
inline int unrecorded_elapsed = 0;
inline int fail_create = -1;
inline bool fail_record = false;
inline bool fail_sync = false;
inline bool fail_elapsed = false;
inline void Reset() {
  creates = records = unrecorded_elapsed = 0;
  fail_create = -1;
  fail_record = fail_sync = fail_elapsed = false;
}
}  // namespace fake_cuda

using cudaEvent_t = fake_cuda::Event*;
using cudaStream_t = void*;
enum cudaError_t { cudaSuccess, cudaErrorUnknown };
inline cudaError_t cudaEventCreate(cudaEvent_t* event) {
  if (++fake_cuda::creates == fake_cuda::fail_create) return cudaErrorUnknown;
  *event = new fake_cuda::Event;
  ++fake_cuda::live;
  return cudaSuccess;
}
inline cudaError_t cudaEventDestroy(cudaEvent_t event) {
  delete event;
  --fake_cuda::live;
  return cudaSuccess;
}
inline cudaError_t cudaEventRecord(cudaEvent_t event, cudaStream_t) {
  ++fake_cuda::records;
  if (fake_cuda::fail_record) return cudaErrorUnknown;
  event->recorded = true;
  return cudaSuccess;
}
inline cudaError_t cudaEventSynchronize(cudaEvent_t event) {
  return fake_cuda::fail_sync || !event->recorded ? cudaErrorUnknown
                                                : cudaSuccess;
}
inline cudaError_t cudaEventElapsedTime(float* ms, cudaEvent_t a,
                                       cudaEvent_t b) {
  if (!a->recorded || !b->recorded) {
    ++fake_cuda::unrecorded_elapsed;
    return cudaErrorUnknown;
  }
  *ms = 1.f;
  return fake_cuda::fail_elapsed ? cudaErrorUnknown : cudaSuccess;
}
inline const char* cudaGetErrorString(cudaError_t) { return "injected error"; }
inline cudaError_t cudaGetLastError() { return cudaSuccess; }
