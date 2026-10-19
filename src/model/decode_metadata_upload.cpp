#include "q4t/model/decode_metadata_upload.h"

#include <algorithm>
#include <cstdio>
#include <limits>
#include <string>

namespace q4t::model {
namespace {
Status Error(const char* action, cudaError_t error) {
  return Status::Fail(std::string("decode metadata ") + action + ": " +
                      cudaGetErrorString(error));
}
}  // namespace

DecodeMetadataUpload::~DecodeMetadataUpload() {
  const Status status = Drain();
  if (pending_) {
    // Do not release possibly in-flight CPU data when completion is unknown.
    std::fprintf(stderr, "decode metadata retained after failed drain: %s\n",
                 status.message().c_str());
    return;
  }
  if (done_) cudaEventDestroy(done_);
  if (source_) cudaFreeHost(source_);
}

Status DecodeMetadataUpload::Init(int capacity) {
  static_assert(sizeof(int) == sizeof(int32_t));
  if (source_ || capacity <= 0)
    return Status::Fail("decode metadata invalid initialization");
  auto error = cudaMallocHost(&source_, size_t(capacity) * 4 * sizeof(int));
  if (error != cudaSuccess) return Error("allocate", error);
  error = cudaEventCreateWithFlags(&done_, cudaEventDisableTiming);
  if (error != cudaSuccess) {
    cudaFreeHost(source_);
    source_ = nullptr;
    return Error("event create", error);
  }
  capacity_ = capacity;
  return Status();
}

Status DecodeMetadataUpload::Drain() {
  if (pending_) {
    const auto error = recorded_ ? cudaEventSynchronize(done_)
                                 : cudaStreamSynchronize(stream_);
    if (error != cudaSuccess) {
      failed_ = true;
      return Error("completion", error);
    }
    pending_ = false;
    recorded_ = false;
  }
  return failed_ ? Status::Fail("decode metadata previous upload failed")
                 : Status();
}

Status DecodeMetadataUpload::Upload(
    std::span<const int32_t> ids, std::span<const int> positions,
    std::span<const int> slots, std::span<const int> rope_delta,
    Targets targets, cudaStream_t stream) {
  const size_t rows = ids.size();
  if (!source_ || rows == 0 || rows > size_t(capacity_) ||
      positions.size() != rows || slots.size() != rows || !targets.ids ||
      !targets.positions || !targets.slots || !targets.rope ||
      targets.max_len <= 0 || targets.max_slots <= 0 ||
      rope_delta.size() < size_t(targets.max_slots))
    return Status::Fail("decode metadata invalid layout");
  for (size_t row = 0; row < rows; ++row) {
    if (positions[row] < 0 || positions[row] >= targets.max_len ||
        slots[row] < 0 || slots[row] >= targets.max_slots)
      return Status::Fail("decode metadata invalid position or slot");
    const int64_t rope = int64_t(positions[row]) + rope_delta[slots[row]];
    if (rope < std::numeric_limits<int>::min() ||
        rope > std::numeric_limits<int>::max())
      return Status::Fail("decode metadata rope overflow");
  }
  Status status = Drain();
  if (!status.ok()) return status;
  int* positions_host = source_ + capacity_;
  int* slots_host = source_ + 2 * size_t(capacity_);
  int* rope_host = source_ + 3 * size_t(capacity_);
  std::copy(ids.begin(), ids.end(), source_);
  std::copy(positions.begin(), positions.end(), positions_host);
  std::copy(slots.begin(), slots.end(), slots_host);
  for (size_t row = 0; row < rows; ++row)
    rope_host[row] = positions[row] + rope_delta[slots[row]];

  stream_ = stream;
  pending_ = true;
  const auto copy = [&](int* dst, const int* src, size_t count) {
    return cudaMemcpyAsync(dst, src, count * sizeof(int),
                           cudaMemcpyHostToDevice, stream);
  };
  const auto fail = [&](cudaError_t error) {
    failed_ = true;
    const Status drained = Drain();
    if (pending_) return drained;
    return Error("upload", error);
  };
  auto error = copy(targets.ids, source_, rows);
  if (error != cudaSuccess) return fail(error);
  error = copy(targets.positions, positions_host, rows);
  if (error != cudaSuccess) return fail(error);
  error = copy(targets.slots, slots_host, rows);
  if (error != cudaSuccess) return fail(error);
  for (size_t row = 0; row < rows; ++row) {
    for (int axis = 0; axis < 3; ++axis) {
      int* dst = targets.rope +
          (size_t(slots[row]) * 3 + axis) * targets.max_len + positions[row];
      error = copy(dst, rope_host + row, 1);
      if (error != cudaSuccess) return fail(error);
    }
  }
  error = cudaEventRecord(done_, stream);
  if (error != cudaSuccess) return fail(error);
  recorded_ = true;
  return Status();
}

}  // namespace q4t::model
