// Weight loading orchestration: map tensor names to shards (index.json) and
// read tensors on demand from the mmap'd shard files.
//
// The model directory's model.safetensors.index.json holds:
//   { "metadata": { "total_size": N, ... },
//     "weight_map": { "<tensor name>": "<shard file>", ... } }
//
// WeightIndex parses the weight_map (name -> shard). WeightLoader opens shard
// files lazily (mmap, read-only) with a small LRU cache, and reads tensor
// bytes on demand (host or H2D). Tensors are addressed by their full
// checkpoint name (e.g. "model.language_model.layers.0.mlp.gate.weight").
#pragma once

#include <mutex>

#include <cuda_runtime.h>

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "q4t/io/safetensors.h"
#include "q4t/status.h"

namespace q4t {
namespace io {

// Parsed model.safetensors.index.json.
class WeightIndex {
 public:
  // Parse the index file.
  static Status Open(const std::string& path, WeightIndex** out);
  ~WeightIndex();
  WeightIndex(const WeightIndex&) = delete;
  WeightIndex& operator=(const WeightIndex&) = delete;

  // Shard file (basename) holding `name`; nullptr if absent.
  const std::string* ShardOf(const std::string& name) const;
  bool Has(const std::string& name) const;
  size_t num_tensors() const;
  size_t num_shards() const;
  uint64_t total_size() const;

  // (shard file, tensor names in that shard) pairs, in index order.
  std::vector<std::pair<std::string, std::vector<std::string>>> ShardGroups()
      const;

 private:
  struct Impl;
  explicit WeightIndex(Impl* impl);
  Impl* impl_;
};

// Reads tensors from the shards referenced by a WeightIndex. Shard files are
// resolved relative to `model_dir` and opened lazily (mmap). A small LRU cache
// keeps recently used shards open. Each read retains its shard independently
// of LRU eviction, without holding the cache lock during file reads. The index
// must outlive the loader, and callers must finish before destroying it.
class WeightLoader {
 public:
  static Status Create(const std::string& model_dir, const WeightIndex& index,
                       size_t max_open_shards, WeightLoader** out);
  ~WeightLoader();
  WeightLoader(const WeightLoader&) = delete;
  WeightLoader& operator=(const WeightLoader&) = delete;

  // Metadata for `name` (from the shard's header); nullptr if absent.
  // The returned immutable metadata belongs to the loader and remains valid
  // until its destruction, including across shard evictions and other calls.
  const TensorInfo* FindTensor(const std::string& name) const;
  // Shard file (basename) holding `name`; nullptr if absent (the
  // residency loader caches this per expert to skip re-resolution on each
  // shard-direct read).
  const std::string* ShardOf(const std::string& name) const;
  // Read `name`'s bytes into `dst` (>= byte_size).
  Status ReadTensor(const std::string& name, void* dst) const;
  // Read `length` bytes from the shard that holds tensor `name`, starting at
  // data-region offset `offset` (TensorInfo::data_start coordinates). Lets
  // the residency loader fetch several adjacent tensors with one pread.
  Status ReadRange(const std::string& name, uint64_t offset, size_t length,
                   void* dst) const;
  // Shard-direct variant of ReadRange for callers that resolved the shard
  // once and cache it (the residency loader caches it per expert).
  Status ReadRangeShard(const std::string& shard, uint64_t offset,
                        size_t length, void* dst) const;
  // Scatter variant of ReadRangeShard (see SafetensorsFile::ReadRangev):
  // one preadv copies a contiguous shard range into several buffers.
  Status ReadRangevShard(const std::string& shard, uint64_t offset,
                         size_t count, const void* const* dsts,
                         const size_t* lens) const;
  // Read `name`'s bytes into `dst`, then H2D-copy to `device_dst` on `stream`.
  Status ReadTensorToDevice(const std::string& name, void* dst,
                            void* device_dst, cudaStream_t stream) const;

  // Number of cached shard handles (for diagnostics). Evicted shards can
  // remain open until their in-progress readers finish.
  size_t open_shards() const;

  // Make shards whose name contains `shard_marker` keep their page-cache
  // pages across LRU eviction (applied to open shards and to every shard
  // opened afterwards). Empty marker: all shards. Tiered MoE residency sets
  // this for the expert shards so on-demand expert preads stay on the warm
  // page cache instead of falling back to NVMe after each shard eviction.
  void SetKeepPageCache(bool keep, const std::string& shard_marker);

 private:
  struct Impl;
  explicit WeightLoader(Impl* impl);
  Impl* impl_;
  // Guards impl_->open/impl_->lru (the shard LRU cache). Required for the
  // parallel MoE expert load, which reads from multiple shards concurrently.
  mutable std::mutex mu_;
};

}  // namespace io
}  // namespace q4t
