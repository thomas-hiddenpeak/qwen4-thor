#include "q4t/model/metadata_upload_batch.h"

#include <cstdio>
#include <cstring>
#include <new>
#include <string>

namespace q4t::model {

MetadataUploadBatch::~MetadataUploadBatch() {
  const Status status = Drain();
  if (pending_) {
    // The device may still read these sources. Reclamation requires context
    // recovery outside this object's contract; do not free in-flight memory.
    for (auto& block : blocks_) (void)block.data.release();
    std::fprintf(stderr, "metadata batch retained after failed drain: %s\n",
                 status.message().c_str());
    return;
  }
  if (done_) cudaEventDestroy(done_);
}

Status MetadataUploadBatch::Drain() {
  open_ = false;
  if (pending_) {
    const auto error = recorded_ ? cudaEventSynchronize(done_)
                                 : cudaStreamSynchronize(stream_);
    if (error != cudaSuccess) {
      failed_ = true;
      return Status::Fail(std::string("metadata batch completion: ") +
                          cudaGetErrorString(error));
    }
    pending_ = false;
    recorded_ = false;
  }
  return failed_ ? Status::Fail("metadata batch previously failed") : Status();
}

Status MetadataUploadBatch::Fail(const char* action, cudaError_t error) {
  failed_ = true;
  const Status status = Drain();
  if (pending_) return status;
  return Status::Fail(std::string("metadata batch ") + action + ": " +
                      cudaGetErrorString(error));
}

Status MetadataUploadBatch::Begin(cudaStream_t stream) {
  Status status = Drain();
  if (!status.ok()) return status;
  if (!done_) {
    const auto error = cudaEventCreateWithFlags(&done_, cudaEventDisableTiming);
    if (error != cudaSuccess) return Fail("event create", error);
  }
  stream_ = stream;
  used_ = 0;
  open_ = true;
  return Status();
}

Status MetadataUploadBatch::Copy(void* destination, const void* source,
                                 size_t bytes) {
  if (!open_ || failed_ || !destination || !source || bytes == 0)
    return Status::Fail("metadata batch invalid copy");
  try {
    if (used_ == blocks_.size()) blocks_.emplace_back();
    Block& block = blocks_[used_];
    if (block.capacity < bytes) {
      auto data = std::make_unique<uint8_t[]>(bytes);
      block.data = std::move(data);
      block.capacity = bytes;
    }
    std::memcpy(block.data.get(), source, bytes);
    // Even a failed API call is handled conservatively as possibly submitted.
    pending_ = true;
    const auto error = cudaMemcpyAsync(destination, block.data.get(), bytes,
                                       cudaMemcpyHostToDevice, stream_);
    ++used_;
    if (error != cudaSuccess) return Fail("copy", error);
  } catch (const std::bad_alloc&) {
    return Fail("source allocation", cudaErrorMemoryAllocation);
  }
  return Status();
}

Status MetadataUploadBatch::Finish() {
  if (!open_ || failed_)
    return Status::Fail("metadata batch is not open");
  open_ = false;
  if (!pending_) return Status();
  const auto error = cudaEventRecord(done_, stream_);
  if (error != cudaSuccess) return Fail("event record", error);
  recorded_ = true;
  return Status();
}

}  // namespace q4t::model
