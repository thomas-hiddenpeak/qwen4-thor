#pragma once

#include <cuda_runtime.h>

#include <cstdio>
#include <memory>
#include <utility>

#include "q4t/status.h"

namespace q4t::io {

// Host transfer storage ownership. Mark before a possibly queued transfer,
// then Drain before reading or mutating the storage. The stream must outlive
// this object. Device storage remains caller-owned. Failed completion retains
// the host storage;
// it does not make a failed CUDA context recoverable.
template <class Storage>
class PendingHostStorage {
 public:
  template <class... Args>
  explicit PendingHostStorage(cudaStream_t stream, Args&&... args)
      : storage_(std::make_unique<Storage>(std::forward<Args>(args)...)),
        stream_(stream) {}
  ~PendingHostStorage() {
    const Status status = Drain();
    if (pending_) {
      (void)storage_.release();
      std::fprintf(stderr,
                   "host transfer storage retained after failed drain: %s\n",
                   status.message().c_str());
    }
  }
  PendingHostStorage(const PendingHostStorage&) = delete;
  PendingHostStorage& operator=(const PendingHostStorage&) = delete;

  Storage& Get() { return *storage_; }
  void MarkPending() { pending_ = true; }
  Status Drain() {
    if (!pending_) return Status();
    const cudaError_t error = cudaStreamSynchronize(stream_);
    if (error != cudaSuccess)
      return Status::Fail(std::string("host transfer storage completion: ") +
                          cudaGetErrorString(error));
    pending_ = false;
    return Status();
  }

 private:
  std::unique_ptr<Storage> storage_;
  cudaStream_t stream_;
  bool pending_ = false;
};

}  // namespace q4t::io
