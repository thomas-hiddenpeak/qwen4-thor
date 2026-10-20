#pragma once

#include <cuda_runtime.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

#include "q4t/status.h"

namespace q4t::model {

// Owns pageable H2D sources until completion, including unfinished batches.
// GPU consumers must still be serialized; the stream must outlive Drain().
// Not thread-safe. Finish() covers uploads only, not later model kernels.
class MetadataUploadBatch {
 public:
  MetadataUploadBatch() = default;
  ~MetadataUploadBatch();
  MetadataUploadBatch(const MetadataUploadBatch&) = delete;
  MetadataUploadBatch& operator=(const MetadataUploadBatch&) = delete;

  Status Begin(cudaStream_t stream);
  Status Copy(void* destination, const void* source, size_t bytes);
  Status Finish();
  Status Drain();

 private:
  struct Block {
    std::unique_ptr<uint8_t[]> data;
    size_t capacity = 0;
  };
  Status Fail(const char* action, cudaError_t error);

  std::vector<Block> blocks_;
  size_t used_ = 0;
  cudaStream_t stream_ = nullptr;
  cudaEvent_t done_ = nullptr;
  bool open_ = false;
  bool pending_ = false;
  bool recorded_ = false;
  bool failed_ = false;
};

}  // namespace q4t::model
