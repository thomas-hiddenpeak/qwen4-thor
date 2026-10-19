#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <span>

#include "q4t/status.h"

namespace q4t::model {

// Owns CPU upload sources only. Caller still serializes GPU scratch consumers
// and keeps the stream alive until Drain. Not thread-safe or copyable.
class DecodeMetadataUpload {
 public:
  struct Targets {
    int32_t* ids;
    int* positions;
    int* slots;
    int* rope;
    int max_len;
    int max_slots;
  };

  DecodeMetadataUpload() = default;
  ~DecodeMetadataUpload();
  DecodeMetadataUpload(const DecodeMetadataUpload&) = delete;
  DecodeMetadataUpload& operator=(const DecodeMetadataUpload&) = delete;

  Status Init(int capacity);
  Status Upload(std::span<const int32_t> ids, std::span<const int> positions,
                std::span<const int> slots, std::span<const int> rope_delta,
                Targets targets, cudaStream_t stream);
  Status Drain();

 private:
  int capacity_ = 0;
  int* source_ = nullptr;
  cudaEvent_t done_ = nullptr;
  cudaStream_t stream_ = nullptr;
  bool pending_ = false;
  bool recorded_ = false;
  bool failed_ = false;
};

}  // namespace q4t::model
