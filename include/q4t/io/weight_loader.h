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
// keeps recently used shards open.
class WeightLoader {
 public:
  static Status Create(const std::string& model_dir, const WeightIndex& index,
                       size_t max_open_shards, WeightLoader** out);
  ~WeightLoader();
  WeightLoader(const WeightLoader&) = delete;
  WeightLoader& operator=(const WeightLoader&) = delete;

  // Metadata for `name` (from the shard's header); nullptr if absent.
  const TensorInfo* FindTensor(const std::string& name) const;
  // Read `name`'s bytes into `dst` (>= byte_size).
  Status ReadTensor(const std::string& name, void* dst) const;
  // Read `name`'s bytes into `dst`, then H2D-copy to `device_dst` on `stream`.
  Status ReadTensorToDevice(const std::string& name, void* dst,
                            void* device_dst, cudaStream_t stream) const;

  // Number of shards currently open (for diagnostics).
  size_t open_shards() const;

 private:
  struct Impl;
  explicit WeightLoader(Impl* impl);
  Impl* impl_;
};

}  // namespace io
}  // namespace q4t
