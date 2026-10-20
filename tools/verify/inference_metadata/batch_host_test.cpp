// Deliberately defer H2D reads until a mock wait. No CUDA runtime is linked.
#include "q4t/model/metadata_upload_batch.h"

#include <cstring>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
struct Copy {
  void* destination;
  const void* source;
  size_t bytes;
  cudaStream_t stream;
};
std::vector<Copy> copies;
int copy_count = 0, fail_copy = 0, events = 0;
int event_waits = 0, stream_waits = 0;
bool fail_create = false, fail_record = false, fail_wait = false;
cudaStream_t recorded_stream = nullptr;
cudaEvent_t last_event = nullptr;
void Require(bool value) {
  if (!value) throw std::runtime_error("batch ownership invariant failed");
}
cudaError_t Wait(cudaStream_t stream) {
  if (fail_wait) return cudaErrorUnknown;
  for (const auto& copy : copies) {
    Require(copy.stream == stream);
    std::memcpy(copy.destination, copy.source, copy.bytes);
  }
  copies.clear();
  return cudaSuccess;
}
void Reset() {
  Require(copies.empty() && events == 0);
  copy_count = fail_copy = event_waits = stream_waits = 0;
  fail_create = fail_record = fail_wait = false;
}
}  // namespace

extern "C" {
cudaError_t __wrap_cudaEventCreateWithFlags(cudaEvent_t* event, unsigned flags) {
  Require(flags == cudaEventDisableTiming);
  if (fail_create) return cudaErrorMemoryAllocation;
  *event = reinterpret_cast<cudaEvent_t>(new int(0));
  last_event = *event;
  ++events;
  return cudaSuccess;
}
cudaError_t __wrap_cudaEventDestroy(cudaEvent_t event) {
  delete reinterpret_cast<int*>(event);
  --events;
  return cudaSuccess;
}
cudaError_t __wrap_cudaMemcpyAsync(void* destination, const void* source,
                                  size_t bytes, cudaMemcpyKind kind,
                                  cudaStream_t stream) {
  Require(kind == cudaMemcpyHostToDevice);
  ++copy_count;
  // Include the failing copy in queued work to exercise conservative draining.
  copies.push_back({destination, source, bytes, stream});
  return copy_count == fail_copy ? cudaErrorUnknown : cudaSuccess;
}
cudaError_t __wrap_cudaEventRecord(cudaEvent_t, cudaStream_t stream) {
  recorded_stream = stream;
  return fail_record ? cudaErrorUnknown : cudaSuccess;
}
cudaError_t __wrap_cudaEventSynchronize(cudaEvent_t) {
  ++event_waits;
  return Wait(recorded_stream);
}
cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t stream) {
  ++stream_waits;
  return Wait(stream);
}
const char* __wrap_cudaGetErrorString(cudaError_t) { return "injected error"; }
}

int main() {
  using q4t::model::MetadataUploadBatch;
  try {
    int cases = 0;
    int source = 17, first = 0, second = 0;
    const auto stream = reinterpret_cast<cudaStream_t>(uintptr_t{1});
    // 1: source independence and invalid calls do not enqueue work.
    Reset();
    {
      MetadataUploadBatch batch;
      Require(!batch.Copy(&first, &source, sizeof(int)).ok());
      Require(batch.Begin(nullptr).ok());
      Require(!batch.Copy(nullptr, &source, sizeof(int)).ok());
      Require(!batch.Copy(&first, nullptr, sizeof(int)).ok());
      Require(!batch.Copy(&first, &source, 0).ok());
      Require(copy_count == 0);
      Require(batch.Copy(&first, &source, sizeof(int)).ok());
      source = 99;
      Require(batch.Finish().ok());
      Require(!batch.Copy(&first, &source, sizeof(int)).ok());
      Require(batch.Drain().ok() && first == 17);
    }
    ++cases;
    // 2: vector growth and repeated caller buffer reuse cannot change sources.
    Reset();
    {
      MetadataUploadBatch batch;
      Require(batch.Begin(nullptr).ok());
      std::vector<int> output(64, -1);
      for (int i = 0; i < 64; ++i) {
        source = i;
        Require(batch.Copy(&output[i], &source, sizeof(int)).ok());
      }
      source = -1;
      Require(batch.Finish().ok() && batch.Drain().ok());
      for (int i = 0; i < 64; ++i) Require(output[i] == i);
    }
    ++cases;
    // 3: Begin waits the old stream before reuse, with a larger next block.
    Reset();
    {
      MetadataUploadBatch batch;
      source = 23;
      Require(batch.Begin(nullptr).ok());
      Require(batch.Copy(&first, &source, sizeof(int)).ok());
      Require(batch.Finish().ok());
      Require(batch.Begin(stream).ok() && first == 23 && event_waits == 1);
      int pair[] = {31, 47}, output[2]{};
      Require(batch.Copy(output, pair, sizeof(pair)).ok());
      pair[0] = 99;
      Require(batch.Finish().ok());
      Require(batch.Begin(stream).ok() && output[0] == 31 && output[1] == 47);
      Require(batch.Finish().ok() && event_waits == 2);
    }
    ++cases;
    // 4: caller's early return without Finish still keeps queued data alive.
    Reset();
    {
      MetadataUploadBatch batch;
      source = 41;
      Require(batch.Begin(stream).ok());
      Require(batch.Copy(&first, &source, sizeof(int)).ok());
      source = 99;
      Require(batch.Begin(nullptr).ok() && first == 41 && stream_waits == 1);
    }
    ++cases;
    // 5: event creation failure is terminal and submits no copies.
    Reset();
    {
      MetadataUploadBatch batch;
      fail_create = true;
      Require(!batch.Begin(nullptr).ok());
      fail_create = false;
      Require(!batch.Begin(nullptr).ok() && events == 0 && copy_count == 0);
    }
    ++cases;
    // 6: partial submission error drains all possibly submitted sources.
    Reset();
    {
      MetadataUploadBatch batch;
      source = 53;
      Require(batch.Begin(stream).ok());
      Require(batch.Copy(&first, &source, sizeof(int)).ok());
      fail_copy = 2;
      Require(!batch.Copy(&second, &source, sizeof(int)).ok());
      Require(copies.empty() && first == 53 && second == 53);
      Require(!batch.Begin(nullptr).ok() && copy_count == 2);
    }
    ++cases;
    // 7: record failure must use stream draining, not an unrecorded event.
    Reset();
    {
      MetadataUploadBatch batch;
      Require(batch.Begin(stream).ok());
      Require(batch.Copy(&first, &source, sizeof(int)).ok());
      fail_record = true;
      Require(!batch.Finish().ok());
      Require(copies.empty() && event_waits == 0 && stream_waits == 1);
    }
    ++cases;
    // 8: failed completion prevents Begin from changing the queued source.
    Reset();
    {
      MetadataUploadBatch batch;
      source = 61;
      Require(batch.Begin(stream).ok());
      Require(batch.Copy(&first, &source, sizeof(int)).ok());
      Require(batch.Finish().ok());
      fail_wait = true;
      Require(!batch.Begin(nullptr).ok());
      source = 99;
      Require(!batch.Copy(&first, &source, sizeof(int)).ok());
      Require(copy_count == 1);
      fail_wait = false;
      Require(!batch.Drain().ok() && first == 61 && copies.empty());
      Require(!batch.Begin(nullptr).ok());
    }
    ++cases;
    // 9: destruction of an unfinished batch drains before freeing its sources.
    Reset();
    {
      MetadataUploadBatch batch;
      source = 71;
      Require(batch.Begin(stream).ok());
      Require(batch.Copy(&first, &source, sizeof(int)).ok());
      source = 99;
    }
    Require(first == 71 && copies.empty() && stream_waits == 1);
    ++cases;
    // 10: failed destructor drain intentionally leaves source/event alive.
    Reset();
    {
      MetadataUploadBatch batch;
      source = 83;
      Require(batch.Begin(stream).ok());
      Require(batch.Copy(&first, &source, sizeof(int)).ok());
      Require(batch.Finish().ok());
      source = 99;
      fail_wait = true;
    }
    Require(copies.size() == 1 && events == 1);
    const auto* retained = static_cast<const uint8_t*>(copies[0].source);
    fail_wait = false;
    Require(Wait(stream) == cudaSuccess && first == 83);
    delete[] retained;  // Test-only simulated context recovery.
    __wrap_cudaEventDestroy(last_event);
    ++cases;
    Reset();
    std::cout << "PASS " << cases << " metadata batch host cases\n";
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
  return 0;
}
