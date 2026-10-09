// Standalone real-CUDA test. Includes the exact production helper used by
// MtpForward; no model, fake runtime, copied helper or numerical oracle.
#include "q4t/mtp/position_copy.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr int kLeft = 0x13572468;
constexpr int kRight = 0x24681357;
constexpr int kUntouched = -123456789;
enum class Source { kPageable, kPinned, kDevice };

const char* Name(Source source) {
  switch (source) {
    case Source::kPageable: return "pageable";
    case Source::kPinned: return "pinned";
    case Source::kDevice: return "device";
  }
  return "invalid";
}

void Check(cudaError_t error, const char* operation) {
  if (error != cudaSuccess) {
    throw std::runtime_error(std::string(operation) + ": " +
                             cudaGetErrorString(error));
  }
}

void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

// This is an independent fixed byte-copy fixture, not a position calculation
// oracle. Large cases include both valid position boundaries and int extremes.
std::vector<int> Expected(int rows) {
  std::vector<int> values(static_cast<size_t>(rows) + 4, kUntouched);
  constexpr std::array<int, 7> kEdges = {
      0, 8191, 8192, 208895, -1, std::numeric_limits<int>::min(),
      std::numeric_limits<int>::max()};
  values[0] = values[rows + 3] = kLeft;
  values[1] = values[rows + 2] = kRight;
  for (int i = 0; i < rows; ++i) {
    values[i + 2] = i < static_cast<int>(kEdges.size())
                        ? kEdges[i]
                        : (1024 + 17 * i) % 208896;
  }
  return values;
}

// Own host sources as well as CUDA allocations. Every release, including an
// exception path, drains the selected stream BEFORE host fields are destroyed.
struct Resources {
  explicit Resources(int rows)
      : expected(Expected(rows)), pageable(expected),
        destination_seed(expected.size(), kUntouched),
        result(expected.size()), source_readback(expected.size()) {
    destination_seed[0] = destination_seed[rows + 3] = kLeft;
    destination_seed[1] = destination_seed[rows + 2] = kRight;
  }
  std::vector<int> expected;
  std::vector<int> pageable;
  std::vector<int> destination_seed;
  std::vector<int> result;
  std::vector<int> source_readback;
  int* pinned = nullptr;
  int* device_source = nullptr;
  int* device_destination = nullptr;
  cudaStream_t stream = nullptr;
  bool owns_stream = false;
  bool closed = false;

  bool Close() {
    if (closed) return true;
    closed = true;
    bool ok = true;
    const auto release = [&](cudaError_t error, const char* what) {
      if (error != cudaSuccess) {
        std::fprintf(stderr, "copy_cleanup operation=%s error=%s\n", what,
                     cudaGetErrorString(error));
        ok = false;
      }
    };
    release(cudaStreamSynchronize(stream), "drain");
    if (device_source) release(cudaFree(device_source), "free_source");
    if (device_destination) {
      release(cudaFree(device_destination), "free_destination");
    }
    if (pinned) release(cudaFreeHost(pinned), "free_pinned");
    if (owns_stream) release(cudaStreamDestroy(stream), "destroy_stream");
    return ok;
  }
  ~Resources() { Close(); }
};

bool RunCase(Source source, bool nonblocking, int rows) {
  Resources memory(rows);
  bool passed = false;
  cudaMemoryType observed_type = cudaMemoryTypeUnregistered;
  try {
    if (nonblocking) {
      Check(cudaStreamCreateWithFlags(&memory.stream, cudaStreamNonBlocking),
            "create_nonblocking_stream");
      memory.owns_stream = true;
    }
    const size_t bytes = memory.expected.size() * sizeof(int);
    Check(cudaMalloc(reinterpret_cast<void**>(&memory.device_destination),
                     bytes), "allocate_destination");
    const int* source_base = memory.pageable.data();
    if (source == Source::kPinned) {
      Check(cudaMallocHost(reinterpret_cast<void**>(&memory.pinned), bytes),
            "allocate_pinned_source");
      std::copy(memory.expected.begin(), memory.expected.end(), memory.pinned);
      source_base = memory.pinned;
    } else if (source == Source::kDevice) {
      Check(cudaMalloc(reinterpret_cast<void**>(&memory.device_source), bytes),
            "allocate_device_source");
      // Both source preparation and destination guard preparation precede the
      // production copy on the selected stream, with no intermediate wait.
      Check(cudaMemcpyAsync(memory.device_source, memory.pageable.data(), bytes,
                            cudaMemcpyHostToDevice, memory.stream),
            "enqueue_source_predecessor");
      source_base = memory.device_source;
    }
    Check(cudaMemcpyAsync(memory.device_destination,
                          memory.destination_seed.data(), bytes,
                          cudaMemcpyHostToDevice, memory.stream),
          "enqueue_destination_predecessor");
    cudaPointerAttributes attributes{};
    Check(cudaPointerGetAttributes(&attributes, source_base + 2),
          "source_pointer_attributes");
    observed_type = attributes.type;
    const cudaMemoryType expected_type = source == Source::kPageable
        ? cudaMemoryTypeUnregistered
        : source == Source::kPinned ? cudaMemoryTypeHost : cudaMemoryTypeDevice;
    Require(observed_type == expected_type, "wrong actual source memory kind");
    Check(q4t::mtp::detail::CopyPositionsForForward(
              memory.device_destination + 2, source_base + 2, rows,
              memory.stream), "production_position_copy");
    Check(cudaMemcpyAsync(memory.result.data(), memory.device_destination,
                          bytes, cudaMemcpyDeviceToHost, memory.stream),
          "enqueue_destination_readback");
    if (source == Source::kDevice) {
      Check(cudaMemcpyAsync(memory.source_readback.data(), memory.device_source,
                            bytes, cudaMemcpyDeviceToHost, memory.stream),
            "enqueue_source_readback");
    }
    Check(cudaStreamSynchronize(memory.stream), "checked_completion");
    Require(memory.result == memory.expected,
            "copied integers or destination guards differ");
    if (source == Source::kDevice) {
      Require(memory.source_readback == memory.expected,
              "device source or source guards changed");
    } else {
      Require(std::equal(memory.expected.begin(), memory.expected.end(),
                         source_base), "host source or source guards changed");
    }
    passed = true;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "copy_failure source=%s stream=%s rows=%d reason=%s\n",
                 Name(source), nonblocking ? "nonblocking" : "default", rows,
                 error.what());
  }
  passed = memory.Close() && passed;
  std::printf("copy_case source=%s stream=%s rows=%d pointer_type=%d "
              "guards_per_side=2 same_stream_predecessor=1 passed=%d\n",
              Name(source), nonblocking ? "nonblocking" : "default", rows,
              static_cast<int>(observed_type), passed);
  return passed;
}

}  // namespace

int main() {
  try {
    int device = -1;
    cudaDeviceProp properties{};
    Check(cudaGetDevice(&device), "get_device");
    Check(cudaGetDeviceProperties(&properties, device), "device_properties");
    Require(properties.unifiedAddressing != 0, "UVA is required, not skipped");
    std::printf("copy_environment device=%d name=%s major=%d minor=%d uva=%d\n",
                device, properties.name, properties.major, properties.minor,
                properties.unifiedAddressing);
    int total = 0;
    int failures = 0;
    for (Source source : {Source::kPageable, Source::kPinned, Source::kDevice}) {
      for (bool nonblocking : {false, true}) {
        for (int rows : {1, 4, 8192}) {
          ++total;
          if (!RunCase(source, nonblocking, rows)) ++failures;
        }
      }
    }
    std::printf("copy_summary cases=%d failures=%d passed=%d\n", total,
                failures, total == 18 && failures == 0);
    return total == 18 && failures == 0 ? 0 : 1;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "copy_environment_failure reason=%s\n", error.what());
    return 1;
  }
}
